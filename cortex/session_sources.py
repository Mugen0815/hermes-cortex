"""Deterministic Hermes session sources for Cortex nightly promotion.

The nightly cron prompt should not depend on legacy JSON snapshots being present.
This module prefers Hermes' SQLite SessionDB (``~/.hermes/state.db``) via a
read-only connection and falls back to legacy JSON/JSONL snapshots only when the
SQLite source is unavailable, incompatible, or has no eligible sessions.
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


_INCLUDED_MESSAGE_ROLES = {"system", "user", "assistant"}
_DEFAULT_SESSION_GLOBS = ["~/.hermes/sessions/*.jsonl", "~/.hermes/sessions/session_*.json"]


@dataclass
class SessionMessage:
    role: str
    content: str
    timestamp: float | None = None
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        if self.source:
            out["source"] = self.source
        if self.metadata:
            out["metadata"] = self.metadata
        return out


@dataclass
class SessionRecord:
    session_id: str
    source: str
    messages: list[SessionMessage]
    started_at: float | None = None
    last_message_ts: float | None = None
    path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def recency_ts(self) -> float:
        if self.last_message_ts is not None:
            return self.last_message_ts
        if self.started_at is not None:
            return self.started_at
        return 0.0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "source": self.source,
            "messages": [m.as_dict() for m in self.messages],
        }
        if self.started_at is not None:
            out["started_at"] = self.started_at
        if self.last_message_ts is not None:
            out["last_message_ts"] = self.last_message_ts
        if self.path:
            out["path"] = self.path
        if self.metadata:
            out["metadata"] = self.metadata
        return out


@dataclass
class SessionSourceDiagnostics:
    source_backend_primary: str = "state_db"
    state_db_path: str = ""
    state_db_schema_version: int | None = None
    state_db_readable: bool = False
    sessions_seen_by_backend: dict[str, int] = field(
        default_factory=lambda: {"state_db": 0, "legacy_json": 0, "legacy_jsonl": 0}
    )
    sessions_selected: int = 0
    sessions_selected_by_source: dict[str, int] = field(default_factory=dict)
    messages_scanned: int = 0
    fallback_used: bool = False
    fallback_reason: str = ""
    ignored_files: dict[str, int] = field(default_factory=lambda: {"request_dump": 0})
    lookback_cutoff: str = ""
    timezone: str = "UTC"
    errors: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=lambda: {"tool_or_non_chat_messages": 0})

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_backend_primary": self.source_backend_primary,
            "state_db_path": self.state_db_path,
            "state_db_schema_version": self.state_db_schema_version,
            "state_db_readable": self.state_db_readable,
            "sessions_seen_by_backend": dict(self.sessions_seen_by_backend),
            "sessions_selected": self.sessions_selected,
            "sessions_selected_by_source": dict(self.sessions_selected_by_source),
            "messages_scanned": self.messages_scanned,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "ignored_files": dict(self.ignored_files),
            "lookback_cutoff": self.lookback_cutoff,
            "timezone": self.timezone,
            "errors": list(self.errors),
            "skipped": dict(self.skipped),
        }


@dataclass
class SessionSourceResult:
    sessions: list[SessionRecord]
    diagnostics: SessionSourceDiagnostics

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": [s.as_dict() for s in self.sessions],
            "diagnostics": self.diagnostics.as_dict(),
        }


def collect_recent_sessions(
    *,
    lookback_days: int = 1,
    timezone: str = "Europe/Berlin",
    state_db_path: str | Path = "~/.hermes/state.db",
    session_globs: Iterable[str] | None = None,
    legacy_fallback_enabled: bool = True,
    now: datetime | None = None,
) -> SessionSourceResult:
    """Collect recent Hermes sessions with state.db as the primary backend."""

    tz = ZoneInfo(timezone)
    now_dt = now.astimezone(tz) if now else datetime.now(tz)
    cutoff_dt = now_dt - timedelta(days=lookback_days)
    cutoff_ts = cutoff_dt.timestamp()
    diagnostics = SessionSourceDiagnostics(
        state_db_path=str(Path(state_db_path).expanduser()),
        lookback_cutoff=cutoff_dt.isoformat(),
        timezone=timezone,
    )

    state_sessions, state_reason = _load_state_db_sessions(
        Path(state_db_path).expanduser(), cutoff_ts, diagnostics
    )
    if state_sessions:
        _finalize_diagnostics(diagnostics, state_sessions)
        return SessionSourceResult(state_sessions, diagnostics)

    if state_reason:
        diagnostics.fallback_reason = state_reason
    else:
        diagnostics.fallback_reason = "state_db_zero_eligible_sessions"

    if not legacy_fallback_enabled:
        _finalize_diagnostics(diagnostics, [])
        return SessionSourceResult([], diagnostics)

    diagnostics.fallback_used = True
    legacy_sessions = _load_legacy_sessions(
        session_globs or _DEFAULT_SESSION_GLOBS,
        cutoff_ts,
        diagnostics,
    )
    if legacy_sessions:
        diagnostics.source_backend_primary = "legacy_files"
    _finalize_diagnostics(diagnostics, legacy_sessions)
    return SessionSourceResult(legacy_sessions, diagnostics)


def _finalize_diagnostics(
    diagnostics: SessionSourceDiagnostics,
    sessions: list[SessionRecord],
) -> None:
    diagnostics.sessions_selected = len(sessions)
    by_source: dict[str, int] = {}
    for session in sessions:
        by_source[session.source] = by_source.get(session.source, 0) + 1
    diagnostics.sessions_selected_by_source = by_source


def _load_state_db_sessions(
    db_path: Path,
    cutoff_ts: float,
    diagnostics: SessionSourceDiagnostics,
) -> tuple[list[SessionRecord], str]:
    if not db_path.exists():
        return [], "state_db_missing"

    uri = f"file:{db_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            _verify_state_db_schema(conn)
            diagnostics.state_db_readable = True
            diagnostics.state_db_schema_version = _schema_version(conn)
            diagnostics.sessions_seen_by_backend["state_db"] = _count_rows(conn, "sessions")
            session_rows = conn.execute(
                """
                SELECT
                    s.id,
                    s.source,
                    s.started_at,
                    s.ended_at,
                    s.title,
                    s.parent_session_id,
                    s.end_reason,
                    COALESCE(MAX(m.timestamp), s.started_at) AS last_message_ts,
                    COUNT(m.id) AS message_count
                FROM sessions s
                LEFT JOIN messages m ON m.session_id = s.id
                GROUP BY s.id
                HAVING COALESCE(MAX(m.timestamp), s.started_at) >= ?
                ORDER BY COALESCE(MAX(m.timestamp), s.started_at) DESC, s.id ASC
                """,
                (cutoff_ts,),
            ).fetchall()
            if not session_rows:
                return [], "state_db_zero_eligible_sessions"

            sessions: list[SessionRecord] = []
            for row in session_rows:
                message_rows = conn.execute(
                    """
                    SELECT id, role, content, timestamp, tool_name, tool_call_id
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY timestamp ASC, id ASC
                    """,
                    (row["id"],),
                ).fetchall()
                diagnostics.messages_scanned += len(message_rows)
                messages = [_message_from_state_db(r, diagnostics) for r in message_rows]
                messages = [m for m in messages if m is not None]
                sessions.append(
                    SessionRecord(
                        session_id=str(row["id"]),
                        source="state_db",
                        started_at=_float_or_none(row["started_at"]),
                        last_message_ts=_float_or_none(row["last_message_ts"]),
                        messages=messages,
                        metadata={
                            "session_source": row["source"],
                            "title": row["title"],
                            "parent_session_id": row["parent_session_id"],
                            "end_reason": row["end_reason"],
                            "raw_message_count": row["message_count"],
                        },
                    )
                )
            return sessions, ""
    except (sqlite3.Error, OSError) as exc:
        diagnostics.errors.append(f"state_db: {exc}")
        return [], "state_db_unreadable_or_incompatible"


def _verify_state_db_schema(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
        ).fetchall()
    }
    missing = {"sessions", "messages"} - tables
    if missing:
        raise sqlite3.DatabaseError(f"missing required tables: {', '.join(sorted(missing))}")
    session_cols = _table_columns(conn, "sessions")
    message_cols = _table_columns(conn, "messages")
    for col in ("id", "started_at"):
        if col not in session_cols:
            raise sqlite3.DatabaseError(f"sessions missing column: {col}")
    for col in ("id", "session_id", "role", "content", "timestamp"):
        if col not in message_cols:
            raise sqlite3.DatabaseError(f"messages missing column: {col}")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(conn: sqlite3.Connection) -> int | None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_version" in tables:
        row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        return int(row[0]) if row and row[0] is not None else None
    if "state_meta" in tables:
        row = conn.execute("SELECT value FROM state_meta WHERE key='schema_version'").fetchone()
        if row and row[0] is not None:
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None
    return None


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def _message_from_state_db(
    row: sqlite3.Row,
    diagnostics: SessionSourceDiagnostics,
) -> SessionMessage | None:
    role = str(row["role"] or "unknown")
    content = row["content"]
    if role not in _INCLUDED_MESSAGE_ROLES:
        diagnostics.skipped["tool_or_non_chat_messages"] += 1
        return None
    if not isinstance(content, str) or not content.strip():
        diagnostics.skipped["tool_or_non_chat_messages"] += 1
        return None
    return SessionMessage(
        role=role,
        content=content,
        timestamp=_float_or_none(row["timestamp"]),
        source="state_db",
        metadata={
            "message_id": row["id"],
            "tool_name": row["tool_name"],
            "tool_call_id": row["tool_call_id"],
        },
    )


def _load_legacy_sessions(
    session_globs: Iterable[str],
    cutoff_ts: float,
    diagnostics: SessionSourceDiagnostics,
) -> list[SessionRecord]:
    paths = _expand_legacy_globs(session_globs)
    sessions: list[SessionRecord] = []
    for path in paths:
        if path.name.startswith("request_dump_") and path.suffix == ".json":
            diagnostics.ignored_files["request_dump"] = diagnostics.ignored_files.get("request_dump", 0) + 1
            continue
        try:
            rows, legacy_kind = load_legacy_session_rows(path)
        except (OSError, ValueError) as exc:
            diagnostics.errors.append(f"legacy:{path}: {exc}")
            continue
        diagnostics.sessions_seen_by_backend[legacy_kind] += 1
        diagnostics.messages_scanned += len(rows)
        recency = _legacy_recency(path, rows)
        if recency < cutoff_ts:
            continue
        messages = [_message_from_legacy(row, legacy_kind, diagnostics) for row in rows]
        messages = [m for m in messages if m is not None]
        sessions.append(
            SessionRecord(
                session_id=path.stem,
                source=legacy_kind,
                started_at=_legacy_started_at(path, rows),
                last_message_ts=recency,
                path=str(path),
                messages=messages,
            )
        )
    sessions.sort(key=lambda s: (-s.recency_ts(), s.session_id))
    return sessions


def _expand_legacy_globs(session_globs: Iterable[str]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in session_globs:
        for match in glob.glob(str(Path(pattern).expanduser())):
            paths.add(Path(match))
    return sorted(paths, key=lambda p: p.as_posix())


def load_legacy_session_rows(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return [], "legacy_jsonl" if path.suffix == ".jsonl" else "legacy_json"

    if path.suffix == ".jsonl":
        rows: list[dict[str, Any]] = []
        for line_number, raw_line in enumerate(raw.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL on line {line_number}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
        return rows, "legacy_jsonl"

    if raw.startswith("["):
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [r for r in parsed if isinstance(r, dict)], "legacy_json"
        raise ValueError("JSON array session did not contain a list")

    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            if isinstance(parsed.get("messages"), list):
                return [m for m in parsed["messages"] if isinstance(m, dict)], "legacy_json"
            return [parsed], "legacy_json"

    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL on line {line_number}: {exc}") from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows, "legacy_jsonl"


def _message_from_legacy(
    row: dict[str, Any],
    source: str,
    diagnostics: SessionSourceDiagnostics,
) -> SessionMessage | None:
    role = str(row.get("role") or "unknown")
    content = row.get("content")
    if role not in _INCLUDED_MESSAGE_ROLES:
        diagnostics.skipped["tool_or_non_chat_messages"] += 1
        return None
    if not isinstance(content, str) or not content.strip():
        diagnostics.skipped["tool_or_non_chat_messages"] += 1
        return None
    return SessionMessage(
        role=role,
        content=content,
        timestamp=_parse_timestamp(row.get("timestamp")),
        source=source,
    )


def _legacy_recency(path: Path, rows: list[dict[str, Any]]) -> float:
    timestamps = [_parse_timestamp(row.get("timestamp")) for row in rows]
    timestamps = [ts for ts in timestamps if ts is not None]
    if timestamps:
        return max(timestamps)
    started = _timestamp_from_stem(path.stem)
    if started is not None:
        return started
    return path.stat().st_mtime


def _legacy_started_at(path: Path, rows: list[dict[str, Any]]) -> float | None:
    timestamps = [_parse_timestamp(row.get("timestamp")) for row in rows]
    timestamps = [ts for ts in timestamps if ts is not None]
    if timestamps:
        return min(timestamps)
    return _timestamp_from_stem(path.stem)


def _timestamp_from_stem(stem: str) -> float | None:
    value = stem.removeprefix("session_")
    for fmt in ("%Y%m%d_%H%M%S_%f", "%Y%m%d_%H%M%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    return None


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect recent Hermes sessions for Cortex nightly promotion")
    parser.add_argument("--lookback-days", type=int, default=1)
    parser.add_argument("--timezone", default="Europe/Berlin")
    parser.add_argument("--state-db-path", default="~/.hermes/state.db")
    parser.add_argument("--session-glob", action="append", dest="session_globs", default=[])
    parser.add_argument("--no-legacy-fallback", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = collect_recent_sessions(
        lookback_days=args.lookback_days,
        timezone=args.timezone,
        state_db_path=args.state_db_path,
        session_globs=args.session_globs or _DEFAULT_SESSION_GLOBS,
        legacy_fallback_enabled=not args.no_legacy_fallback,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
