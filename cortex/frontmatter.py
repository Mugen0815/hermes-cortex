"""Frontmatter normalization for hermes-cortex.

Single source of truth for how raw YAML frontmatter is coerced into the
canonical form used throughout indexing, embedding, and (Phase 3) search.

Decisions (locked):
- ``tags``: accepts list, comma-separated string, or single string. Always
  emitted as ``list[str]``. None → [].
- ``confidence`` / ``importance`` / ``stability``: numeric where possible.
  Strings like "high"/"medium"/"low" are mapped to a 0..1 / 1..5 scale so
  Phase-3 boost math has a real number to work with. Original string is
  preserved in ``frontmatter[<field>_raw]`` for citations.
- Dates (``last_verified``, ``created``, etc.): coerced to ISO ``YYYY-MM-DD``.
- Unknown enum values for ``type`` / ``status`` / ``stability`` are kept as-is;
  caller decides whether to warn.

This module is the ONLY place that knows about field-level semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


# ---- Enum vocabularies (informational; we warn but don't reject) ----------

KNOWN_TYPES = {"fact", "decision", "project", "runbook", "person", "map", "note"}
KNOWN_STATUSES = {"active", "draft", "archived", "deprecated", "stale", "superseded"}
KNOWN_STABILITY = {"stable", "evolving", "experimental"}

# String→numeric maps. Importance/confidence are stored as floats; stability
# stays categorical (it's a label, not a magnitude).
_CONFIDENCE_MAP = {
    "low": 0.25, "medium": 0.5, "med": 0.5, "high": 0.85, "verified": 1.0,
    "unknown": 0.5, "": 0.5,
}
_IMPORTANCE_MAP = {
    "low": 1.0, "medium": 3.0, "med": 3.0, "high": 5.0, "critical": 5.0,
    "": 3.0,
}


# ---- Result type -----------------------------------------------------------


@dataclass
class NormalizedFrontmatter:
    """Canonical frontmatter view consumed by indexer + embedder.

    ``raw`` holds the original parsed YAML (after JSON-safe coercion of
    dates/etc.) so callers can still cite human-friendly labels.
    """

    type: str = ""
    status: str = ""
    domain: str = ""
    project: str = ""
    stability: str = ""
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5     # 0..1
    importance: float = 3.0     # 1..5
    last_verified: str = ""     # ISO date or ""
    created: str = ""
    related: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # Issues found during normalization. Each is a short human-readable string.
    warnings: list[str] = field(default_factory=list)


# ---- Coercion helpers ------------------------------------------------------


def _to_iso_date(v: Any) -> str:
    """Best-effort ISO date string. Empty on failure."""
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    # Accept already-ISO; otherwise try fromisoformat for tolerance.
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except ValueError:
        # Try just the date portion.
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return ""


def _coerce_tags(v: Any) -> list[str]:
    """Accept list / CSV string / single string. Dedupe preserving order."""
    if v is None:
        return []
    items: list[str]
    if isinstance(v, list):
        items = [str(x).strip() for x in v if x is not None and str(x).strip()]
    elif isinstance(v, str):
        # Split on commas if any, else treat whole string as one tag.
        if "," in v:
            items = [s.strip() for s in v.split(",") if s.strip()]
        else:
            items = [v.strip()] if v.strip() else []
    else:
        items = [str(v).strip()]
    seen: dict[str, None] = {}
    for it in items:
        if it not in seen:
            seen[it] = None
    return list(seen.keys())


def _coerce_confidence(v: Any) -> tuple[float, list[str]]:
    """Return (value_in_0_1, warnings)."""
    warnings: list[str] = []
    if v is None or v == "":
        return 0.5, warnings
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if 0.0 <= f <= 1.0:
            return f, warnings
        if 0.0 <= f <= 5.0:
            return f / 5.0, warnings
        warnings.append(f"confidence {v!r} out of range; clamped to 0..1")
        return max(0.0, min(1.0, f)), warnings
    s = str(v).strip().lower()
    if s in _CONFIDENCE_MAP:
        return _CONFIDENCE_MAP[s], warnings
    # Try numeric string.
    try:
        return _coerce_confidence(float(s))
    except ValueError:
        warnings.append(f"unrecognized confidence {v!r}; using 0.5")
        return 0.5, warnings


def _coerce_importance(v: Any) -> tuple[float, list[str]]:
    """Return (value_in_1_5, warnings)."""
    warnings: list[str] = []
    if v is None or v == "":
        return 3.0, warnings
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        if 1.0 <= f <= 5.0:
            return f, warnings
        if 0.0 <= f <= 1.0:
            return 1.0 + 4.0 * f, warnings
        warnings.append(f"importance {v!r} out of range; clamped to 1..5")
        return max(1.0, min(5.0, f)), warnings
    s = str(v).strip().lower()
    if s in _IMPORTANCE_MAP:
        return _IMPORTANCE_MAP[s], warnings
    try:
        return _coerce_importance(float(s))
    except ValueError:
        warnings.append(f"unrecognized importance {v!r}; using 3.0")
        return 3.0, warnings


def _coerce_yaml_jsonsafe(value: Any) -> Any:
    """Make YAML values JSON-serializable (dates → str). Recursive."""
    if isinstance(value, (date, datetime)):
        return value.isoformat() if isinstance(value, date) else value.isoformat()
    if isinstance(value, list):
        return [_coerce_yaml_jsonsafe(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce_yaml_jsonsafe(v) for k, v in value.items()}
    return value


# ---- Public API ------------------------------------------------------------


def normalize(fm: dict[str, Any] | None) -> NormalizedFrontmatter:
    """Normalize a parsed YAML frontmatter dict into a canonical view.

    Always returns a NormalizedFrontmatter; missing fields fall back to
    sensible defaults. Callers should also check ``warnings``.
    """
    out = NormalizedFrontmatter()
    if not fm:
        out.raw = {}
        return out

    # Preserve a JSON-safe copy of the original.
    out.raw = _coerce_yaml_jsonsafe(fm)

    # ---- enums (warn on unknown, keep value) ----
    t = str(fm.get("type") or "").strip()
    if t and t not in KNOWN_TYPES:
        out.warnings.append(f"unknown type {t!r}")
    out.type = t

    st = str(fm.get("status") or "").strip()
    if st and st not in KNOWN_STATUSES:
        out.warnings.append(f"unknown status {st!r}")
    out.status = st

    sb = str(fm.get("stability") or "").strip()
    if sb and sb not in KNOWN_STABILITY:
        out.warnings.append(f"unknown stability {sb!r}")
    out.stability = sb

    out.domain = str(fm.get("domain") or "").strip()
    out.project = str(fm.get("project") or "").strip()

    # ---- tags ----
    tags_raw = fm.get("tags")
    out.tags = _coerce_tags(tags_raw)
    if tags_raw and not out.tags:
        out.warnings.append(f"could not parse tags from {tags_raw!r}")

    # ---- numeric signals ----
    conf, w = _coerce_confidence(fm.get("confidence"))
    out.confidence = conf
    out.warnings.extend(w)

    imp, w = _coerce_importance(fm.get("importance"))
    out.importance = imp
    out.warnings.extend(w)

    # ---- dates ----
    out.last_verified = _to_iso_date(fm.get("last_verified"))
    if fm.get("last_verified") and not out.last_verified:
        out.warnings.append(f"could not parse last_verified {fm['last_verified']!r}")

    out.created = _to_iso_date(fm.get("created"))
    if fm.get("created") and not out.created:
        out.warnings.append(f"could not parse created {fm['created']!r}")

    # ---- related (1-hop graph signal; raw wikilink strings allowed) ----
    rel = fm.get("related")
    if isinstance(rel, list):
        out.related = [str(r).strip() for r in rel if r is not None and str(r).strip()]
    elif isinstance(rel, str) and rel.strip():
        out.related = [rel.strip()]

    return out


REQUIRED_FIELDS = {"type", "status", "tags", "confidence", "importance", "stability"}


def missing_required(fm: dict[str, Any] | None) -> list[str]:
    """Return required-field names absent from raw frontmatter (sorted).

    We check for *presence* in the raw dict, not normalized values, because
    presence is what the user controls. An empty list with key ``tags: []``
    counts as present.
    """
    if not fm:
        return sorted(REQUIRED_FIELDS)
    return sorted(REQUIRED_FIELDS - set(fm.keys()))
