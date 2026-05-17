"""Score boosts for hermes-cortex hybrid search — Phase 3 / Slice 2.

Both boosts are **multiplicative** and bounded by their respective config
knobs. They apply to the fused RRF score:

    final_score = rrf_score * (1 + recency_factor) * (1 + importance_factor)

with each factor in ``[0, max_boost]``.

Recency
-------
Half-life decay on the *age in days* of the chunk's source date::

    age_days = max(0, (now - source_date).days)
    recency_factor = recency_max_boost * exp(-ln(2) * age_days / half_life_days)

At ``age_days == half_life_days`` the factor equals ``recency_max_boost / 2``
(half-life property — the formula uses ``ln(2)`` precisely so that this
holds; ``exp(-age/HL)`` would only decay to ~36.8% at one half-life and is
the wrong formula).

Source date selection (in priority order):
  1. ``fm_normalized.last_verified`` if a valid ISO date.
  2. ``chunk.modified_date`` (or first 10 chars of ``chunk.modified``).
The fallback to ``modified_date`` exists because not every note has
``last_verified`` set — but a file mtime can mean "I touched the
formatting yesterday", so ``last_verified`` is preferred when present.

Missing both → recency_factor = 0 (neutral, no boost).

Importance
----------
Linear scaling on normalized importance::

    importance_factor = (importance - 1) / 4 * importance_max_boost

with ``importance ∈ [1, 5]`` clamped. Importance == 5 → full boost,
importance == 1 → zero boost (and importance == 3 → 0.5 * max_boost).

**Missing importance → factor = 0 (neutral).** We deliberately do *not*
use the schema default of 3.0 here. A missing/unparseable importance
value is "I don't know how important this is", not "this is medium-
importance" — phantom-boosting unsanitized metadata would be wrong.

Combined boost cap and quality factor
-------------------------------------
The raw recency * importance multiplier can still reach 1.56 with the
legacy per-factor defaults. Search applies a conservative combined cap
(``search.max_boost_multiplier``, default 1.20) so metadata is only a nudge.

After boost capping, obvious Links/Related/References chunks get a content
quality penalty (``search.link_chunk_penalty``, default 0.75) for normal
content queries. Explicit link/relation queries skip that penalty so those
chunks stay discoverable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from cortex.config import SearchConfig

# ln(2) — used in half-life formula; pulled out for clarity.
_LN2 = math.log(2.0)
_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
_LINK_SECTION_HEADINGS = {
    "link",
    "links",
    "related",
    "relations",
    "references",
    "reference",
    "see also",
    "verweise",
    "verwandt",
    "beziehungen",
    "referenzen",
    "links / related",
}
_EXPLICIT_LINK_QUERY_TERMS = {
    "link",
    "links",
    "wikilink",
    "wikilinks",
    "related",
    "relation",
    "relations",
    "reference",
    "references",
    "verweis",
    "verweise",
    "verknuepft",
    "verknüpft",
    "verbunden",
    "beziehung",
    "beziehungen",
}


@dataclass(frozen=True)
class BoostApplication:
    """Score-adjustment diagnostics for one fused hit.

    ``raw_boost_multiplier`` is recency * importance before the conservative
    cap. ``boost_multiplier`` is the capped multiplier actually used.
    ``quality_factor`` is currently the link/related chunk penalty and remains
    1.0 for normal content chunks or explicit relation/link queries.
    """

    final_score: float
    recency_factor: float
    importance_factor: float
    raw_boost_multiplier: float
    boost_multiplier: float
    boost_capped: bool
    quality_factor: float
    quality_reason: str


# ---- Date extraction -------------------------------------------------------


def _parse_iso_date(s: Any) -> Optional[date]:
    """Parse ISO date / datetime / date-prefix string. Returns None if unparseable."""
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    # Accept full datetime or pure date; both have YYYY-MM-DD as prefix.
    head = s[:10]
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def source_date_for_recency(chunk: dict[str, Any]) -> Optional[date]:
    """Pick the date used for the recency boost.

    Priority:
      1. ``fm_normalized.last_verified``
      2. ``chunk.modified_date``
      3. ``chunk.modified`` (first 10 chars)
    Returns ``None`` if none parse — boost stays neutral.
    """
    fm = chunk.get("fm_normalized") or {}
    d = _parse_iso_date(fm.get("last_verified"))
    if d:
        return d
    d = _parse_iso_date(chunk.get("modified_date"))
    if d:
        return d
    return _parse_iso_date(chunk.get("modified"))


# ---- Importance extraction -------------------------------------------------


def importance_value(chunk: dict[str, Any]) -> Optional[float]:
    """Extract a numeric importance in [1, 5], or None if missing/unparseable.

    Returning None signals "no boost". We do **not** fall back to 3.0 here
    even though that's the schema default in ``frontmatter.normalize`` —
    missing data should not phantom-boost.
    """
    fm = chunk.get("fm_normalized") or {}
    raw_fm = chunk.get("frontmatter") or {}
    # fm_normalized.importance is filled with the default 3.0 by the
    # frontmatter normalizer even when the source field is missing. To detect
    # *truly* missing data, prefer the raw frontmatter and only fall back to
    # the normalized value when the source explicitly carried importance.
    if "importance" in raw_fm and raw_fm.get("importance") not in (None, ""):
        try:
            v = float(raw_fm["importance"])
        except (TypeError, ValueError):
            return None
    elif "importance" in fm and fm.get("importance") is not None:
        # Only trust normalized importance if raw also carried *something* —
        # otherwise this is the schema default leaking in. The normalizer
        # warns on missing required fields, so fm_normalized may carry 3.0
        # without raw_fm having anything. Treat that as missing.
        return None
    else:
        return None
    # Clamp to [1, 5].
    if v < 1.0:
        v = 1.0
    elif v > 5.0:
        v = 5.0
    return v


# ---- Boost factors ---------------------------------------------------------


def recency_factor(
    chunk: dict[str, Any],
    cfg: SearchConfig,
    *,
    now: Optional[date] = None,
) -> float:
    """Return recency factor in ``[0, recency_max_boost]``.

    ``now`` is injectable for deterministic tests.
    """
    if not cfg.recency_boost or cfg.recency_max_boost <= 0:
        return 0.0
    src = source_date_for_recency(chunk)
    if src is None:
        return 0.0
    today = now or date.today()
    age_days = max(0, (today - src).days)
    decay = math.exp(-_LN2 * age_days / cfg.recency_half_life_days)
    return cfg.recency_max_boost * decay


def importance_factor(chunk: dict[str, Any], cfg: SearchConfig) -> float:
    """Return importance factor in ``[0, importance_max_boost]``."""
    if not cfg.importance_boost or cfg.importance_max_boost <= 0:
        return 0.0
    v = importance_value(chunk)
    if v is None:
        return 0.0
    return (v - 1.0) / 4.0 * cfg.importance_max_boost


def raw_boost_multiplier(recency_f: float, importance_f: float) -> float:
    """Return the uncapped combined boost multiplier."""
    return (1.0 + recency_f) * (1.0 + importance_f)


def capped_boost_multiplier(raw_multiplier: float, cfg: SearchConfig) -> tuple[float, bool]:
    """Cap the combined boost multiplier using ``cfg.max_boost_multiplier``."""
    cap = max(1.0, float(cfg.max_boost_multiplier))
    capped = min(raw_multiplier, cap)
    return capped, capped < raw_multiplier


def query_explicitly_asks_for_links(query: str | None) -> bool:
    """Return True when a query is explicitly about links/relations."""
    if not query:
        return False
    tokens = {t.lower() for t in re.findall(r"[\wäöüÄÖÜß]+", query, flags=re.UNICODE)}
    return bool(tokens & _EXPLICIT_LINK_QUERY_TERMS)


def chunk_quality_factor(
    chunk: dict[str, Any],
    cfg: SearchConfig,
    *,
    query: str | None = None,
) -> tuple[float, str]:
    """Return a content-quality multiplier and reason.

    Penalizes obvious Links/Related/References chunks for normal content
    queries. Explicit link/relation queries keep the factor neutral so graph
    and relation sections remain findable when the user asks for them.
    """
    if query_explicitly_asks_for_links(query):
        return 1.0, "explicit_link_query"

    penalty = float(cfg.link_chunk_penalty)
    if penalty >= 1.0:
        return 1.0, "neutral"

    headings = [str(h).strip().lower() for h in (chunk.get("heading_path") or []) if str(h).strip()]
    heading = str(chunk.get("heading") or "").strip().lower()
    if heading:
        headings.append(heading)
    for h in headings:
        h_norm = h.strip("# ")
        if h_norm in _LINK_SECTION_HEADINGS:
            return penalty, "link_heading"

    text = str(chunk.get("text") or "").strip()
    wikilink_matches = list(_WIKILINK_RE.finditer(text))
    wikilink_count = len(wikilink_matches)
    if not text or wikilink_count == 0:
        return 1.0, "neutral"

    # Only inline wikilinks in this chunk's text are a reliable signal for a
    # link-list shaped chunk. ``chunk["wikilinks"]`` can include note-level
    # links/related metadata; using it here penalizes normal short content
    # sections in otherwise linked notes.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullet_link_lines = [
        ln for ln in lines
        if ln.startswith(("- ", "* ")) and _WIKILINK_RE.search(ln)
    ]
    word_count = len(re.findall(r"[\wäöüÄÖÜß]+", text, flags=re.UNICODE))
    wikilink_chars = sum(len(m.group(0)) for m in wikilink_matches)
    density = wikilink_chars / max(1, len(text))

    if lines and len(bullet_link_lines) >= max(2, int(len(lines) * 0.6)):
        return penalty, "wikilink_bullet_list"
    if wikilink_count >= 2 and (word_count <= 40 or density >= 0.35):
        return penalty, "wikilink_dense_short_chunk"

    return 1.0, "neutral"


def apply_boosts_detailed(
    rrf_score: float,
    chunk: dict[str, Any],
    cfg: SearchConfig,
    *,
    now: Optional[date] = None,
    query: str | None = None,
) -> BoostApplication:
    """Apply capped boosts and chunk-quality factor with diagnostics."""
    rf = recency_factor(chunk, cfg, now=now)
    if_ = importance_factor(chunk, cfg)
    raw = raw_boost_multiplier(rf, if_)
    boost, boost_capped = capped_boost_multiplier(raw, cfg)
    qf, q_reason = chunk_quality_factor(chunk, cfg, query=query)
    final = rrf_score * boost * qf
    return BoostApplication(
        final_score=final,
        recency_factor=rf,
        importance_factor=if_,
        raw_boost_multiplier=raw,
        boost_multiplier=boost,
        boost_capped=boost_capped,
        quality_factor=qf,
        quality_reason=q_reason,
    )


def apply_boosts(
    rrf_score: float,
    chunk: dict[str, Any],
    cfg: SearchConfig,
    *,
    now: Optional[date] = None,
) -> tuple[float, float, float]:
    """Backward-compatible boost helper returning ``(final, recency, importance)``.

    The returned final score uses the capped boost multiplier. It does not
    apply link/related chunk quality penalties because this legacy helper has
    no query parameter; the search pipeline calls ``apply_boosts_detailed``.
    """
    applied = apply_boosts_detailed(rrf_score, chunk, cfg, now=now, query=None)
    return applied.final_score, applied.recency_factor, applied.importance_factor
