"""Deterministic SessionDB recent-topic context for Hermes/Cortex hooks.

This module intentionally reads only SessionDB session metadata.  It does not
read transcript/message bodies, JSON session snapshots, or call an LLM.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from cortex.config import _expand, _hermes_home
from cortex.text import estimate_tokens

_GENERIC_TITLE_RE = re.compile(
    r"^(?:untitled|new chat|new session|chat|session|cli session|api request|hermes session|conversation)$",
    re.IGNORECASE,
)
_SERIES_SUFFIX_RE = re.compile(r"(?:\s*(?:#|no\.?\s*)\d+)+\s*$", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class RecentTopic:
    title: str
    count: int
    newest: float
    source_counts: dict[str, int]
    open_count: int = 0
    sample_session_id: str = ""


@dataclass
class RecentContextDiagnostics:
    source: str = "sessiondb"
    state_db_path: str = ""
    examined_sessions: int = 0
    filtered_counts: dict[str, int] = field(default_factory=dict)
    groups_produced: int = 0
    budget_used: int = 0
    skipped_reason: str = ""


@dataclass
class RecentContextResult:
    text: str = ""
    topics: list[RecentTopic] = field(default_factory=list)
    diagnostics: RecentContextDiagnostics = field(default_factory=RecentContextDiagnostics)
    query_hint: str = ""


def _root_hermes_home_from_profile_home(path: Path) -> Path | None:
    parts = path.resolve().parts
    if "profiles" not in parts:
        return None
    idx = parts.index("profiles")
    if idx == 0:
        return None
    return Path(*parts[:idx])


def resolve_state_db_path(configured: str | None = None) -> tuple[Path, str]:
    """Resolve Hermes SessionDB path without assuming profile ``Path.home()``.

    Resolution order:
    1. explicit recent_context.state_db_path;
    2. HERMES_STATE_DB / HERMES_SESSION_DB;
    3. profile-aware Hermes home if it actually contains state.db;
    4. root Hermes home derived from ``.../.hermes/profiles/<profile>``.
    """
    if configured and str(configured).strip():
        p = _expand(str(configured))
        if p is None:
            raise FileNotFoundError(f"invalid configured state_db_path: {configured!r}")
        return p, "config"

    for env_name in ("HERMES_STATE_DB", "HERMES_SESSION_DB"):
        env_val = os.environ.get(env_name)
        if env_val:
            p = _expand(env_val)
            if p is None:
                continue
            return p, f"env:{env_name}"

    hermes_home = _hermes_home()
    direct = hermes_home / "state.db"
    if direct.exists():
        return direct, "hermes_home"

    root_home = _root_hermes_home_from_profile_home(hermes_home)
    if root_home is not None:
        root_state = root_home / "state.db"
        if root_state.exists():
            return root_state, "root_hermes_home_from_profile"

    # Last deterministic candidate for diagnostics; caller will report missing.
    return direct, "hermes_home_missing"


def normalize_title(title: str) -> str:
    title = _WHITESPACE_RE.sub(" ", title.strip())
    title = _SERIES_SUFFIX_RE.sub("", title).strip()
    return _WHITESPACE_RE.sub(" ", title)


def is_noise_title(title: str) -> bool:
    normalized = normalize_title(title)
    if not normalized:
        return True
    if _GENERIC_TITLE_RE.match(normalized):
        return True
    if len(normalized) <= 2:
        return True
    return False


def _source_allowed(source: str, include_sources: Iterable[str], exclude_sources: Iterable[str]) -> bool:
    include = {s.strip() for s in include_sources if str(s).strip()}
    exclude = {s.strip() for s in exclude_sources if str(s).strip()}
    if include and source not in include:
        return False
    if source in exclude:
        return False
    return True


def _fmt_timestamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _render_topics(topics: list[RecentTopic], budget: int) -> tuple[str, int, bool]:
    if not topics or budget <= 0:
        return "", 0, bool(topics)
    lines = ["## Recent Hermes Topics", "", "SessionDB metadata only; no transcript/message bodies included."]
    used_topics = 0
    truncated = False
    for topic in topics:
        source_bits = ", ".join(f"{src}={count}" for src, count in sorted(topic.source_counts.items()))
        open_bit = f"; open={topic.open_count}" if topic.open_count else ""
        line = (
            f"- {topic.title} — {topic.count} session(s), newest {_fmt_timestamp(topic.newest)}, "
            f"sources: {source_bits}{open_bit}"
        )
        candidate = "\n".join(lines + [line])
        if estimate_tokens(candidate) > budget:
            truncated = True
            break
        lines.append(line)
        used_topics += 1
    if truncated:
        note = f"[recent_context truncated to budget={budget}; shown={used_topics}; total={len(topics)}]"
        candidate = "\n".join(lines + [note])
        if estimate_tokens(candidate) <= budget or used_topics == 0:
            lines.append(note)
    text = "\n".join(lines).strip()
    return text, estimate_tokens(text), truncated


def build_recent_context(config: Any) -> RecentContextResult:
    """Build deterministic recent-topic context from SessionDB metadata."""
    diagnostics = RecentContextDiagnostics(source="sessiondb")
    configured_path = getattr(config, "state_db_path", None)
    try:
        state_db, source = resolve_state_db_path(configured_path)
        diagnostics.state_db_path = str(state_db)
        diagnostics.source = f"sessiondb:{source}"
        if not state_db.exists():
            diagnostics.skipped_reason = f"state_db not found: {state_db}"
            return RecentContextResult(diagnostics=diagnostics)

        lookback_days = int(getattr(config, "lookback_days", 7))
        max_sessions = int(getattr(config, "max_sessions", 500))
        max_groups = int(getattr(config, "max_groups", 8))
        budget = int(getattr(config, "budget", 1000))
        include_sources = list(getattr(config, "include_sources", []) or [])
        exclude_sources = list(getattr(config, "exclude_sources", []) or [])

        cutoff = time.time() - max(1, lookback_days) * 86400
        rows: list[tuple[str, str, str, float, float | None, int, int]] = []
        with sqlite3.connect(f"file:{state_db}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            query = """
                select
                    id,
                    coalesce(title, '') as title,
                    coalesce(source, '') as source,
                    cast(started_at as real) as started_at,
                    cast(ended_at as real) as ended_at,
                    coalesce(message_count, 0) as message_count,
                    coalesce(tool_call_count, 0) as tool_call_count
                from sessions
                where coalesce(cast(ended_at as real), cast(started_at as real)) >= ?
                order by coalesce(cast(ended_at as real), cast(started_at as real)) desc, id asc
                limit ?
            """
            rows = [
                (
                    str(r["id"]),
                    str(r["title"] or ""),
                    str(r["source"] or ""),
                    float(r["started_at"] or 0),
                    float(r["ended_at"]) if r["ended_at"] is not None else None,
                    int(r["message_count"] or 0),
                    int(r["tool_call_count"] or 0),
                )
                for r in con.execute(query, (cutoff, max_sessions))
            ]
    except Exception as exc:
        diagnostics.skipped_reason = f"SessionDB query failed: {type(exc).__name__}: {exc}"
        return RecentContextResult(diagnostics=diagnostics)

    diagnostics.examined_sessions = len(rows)
    filtered: Counter[str] = Counter()
    grouped: dict[str, dict[str, Any]] = {}

    for session_id, raw_title, source, started_at, ended_at, _msg_count, _tool_count in rows:
        if not _source_allowed(source, include_sources, exclude_sources):
            filtered[f"source:{source or '(empty)'}"] += 1
            continue
        if is_noise_title(raw_title):
            filtered["title_noise"] += 1
            continue
        title = normalize_title(raw_title)
        key = title.casefold()
        ts = ended_at if ended_at is not None else started_at
        if key not in grouped:
            grouped[key] = {
                "title": title,
                "count": 0,
                "newest": ts,
                "source_counts": Counter(),
                "open_count": 0,
                "sample_session_id": session_id,
            }
        g = grouped[key]
        g["count"] += 1
        g["source_counts"][source or "unknown"] += 1
        if ended_at is None:
            g["open_count"] += 1
        if ts > g["newest"]:
            g["newest"] = ts
            g["sample_session_id"] = session_id

    topics = [
        RecentTopic(
            title=g["title"],
            count=int(g["count"]),
            newest=float(g["newest"]),
            source_counts=dict(g["source_counts"]),
            open_count=int(g["open_count"]),
            sample_session_id=str(g["sample_session_id"]),
        )
        for g in grouped.values()
    ]
    topics.sort(key=lambda t: (-t.newest, t.title.casefold()))
    all_topics = topics
    topics = topics[:max(0, max_groups)]
    if len(all_topics) > len(topics):
        filtered["max_groups_truncated"] = len(all_topics) - len(topics)

    text, budget_used, _truncated = _render_topics(topics, budget)
    diagnostics.filtered_counts = dict(sorted(filtered.items()))
    diagnostics.groups_produced = len(topics)
    diagnostics.budget_used = budget_used
    if not topics and not diagnostics.skipped_reason:
        diagnostics.skipped_reason = "no recent topics after filters"

    query_hint = ""
    if bool(getattr(config, "query_hint", False)) and topics:
        query_hint = " OR ".join(t.title for t in topics[: min(5, len(topics))])

    return RecentContextResult(text=text, topics=topics, diagnostics=diagnostics, query_hint=query_hint)


def render_diagnostics(diag: RecentContextDiagnostics) -> str:
    filtered = ", ".join(f"{k}={v}" for k, v in sorted(diag.filtered_counts.items())) or "none"
    skipped = f"; skipped={diag.skipped_reason}" if diag.skipped_reason else ""
    return (
        f"source={diag.source}; db={diag.state_db_path or '(unresolved)'}; "
        f"examined={diag.examined_sessions}; filtered={filtered}; "
        f"groups={diag.groups_produced}; budget_used={diag.budget_used}{skipped}"
    )
