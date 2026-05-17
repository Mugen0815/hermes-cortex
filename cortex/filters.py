"""Search filter spec for hermes-cortex hybrid search — Phase 3 / Slice 2.

This module defines the ``SearchFilters`` dataclass that the public
``HybridSearcher.search()`` API accepts and the helpers that translate it
into:

  1. A Chroma ``where`` dict for scalar (numeric / exact / range) filters
     applied at the vector channel.
  2. A pure-Python predicate for membership filters (tags / wikilinks)
     applied as a post-fetch step against ``chunks.jsonl`` arrays.

Filter semantics (locked-in convention):

  * **AND across fields.** Two different filter fields combine with AND —
    e.g. ``type=["fact"]`` AND ``importance_min=4`` matches chunks that are
    facts *and* importance >= 4.
  * **OR within a single list field.** A list value means *any-of*:
    ``type=["fact", "decision"]`` matches chunks whose type is fact OR
    decision.
  * **``*_any`` = at least one match.** ``tags_any=["jarvis", "memory"]``
    matches a chunk that has *jarvis* OR *memory* in its tags array.
  * **``*_all`` = all must match.** ``tags_all=["jarvis", "memory"]``
    matches only chunks that have *both* tags.
  * **Date strings must be ISO ``YYYY-MM-DD``.** Both ``modified_after``
    / ``modified_before`` and any future date filters are validated at
    ``SearchFilters.validate()`` time. Time-of-day is ignored — date-level
    granularity is what the index stores in ``modified_date``.

The ``*_flat`` strings written to Chroma metadata (``tags_flat``,
``wikilinks_flat``) are debug/transport only and **must not** be used as
filter inputs — array filtering goes through the JSONL side. Documented in
``cortex.embedder.chunk_metadata_for_chroma``.

This module is intentionally side-effect free and has no Chroma dependency:
the where-builder returns a plain dict, the predicates take plain chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Strict ISO date: YYYY-MM-DD. Calendar correctness is verified separately.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_iso_date(s: str) -> bool:
    """True iff ``s`` matches ``YYYY-MM-DD`` and is a real calendar date."""
    if not isinstance(s, str) or not _ISO_DATE_RE.match(s):
        return False
    from datetime import date as _date
    try:
        y, m, d = (int(x) for x in s.split("-"))
        _date(y, m, d)
    except (ValueError, TypeError):
        return False
    return True


@dataclass
class SearchFilters:
    """Declarative pre-fusion filter spec.

    All fields default to "no filter". ``None`` / empty list = unconstrained.

    Scalar / exact / range (Chroma side):
      type, status, domain, project        — list[str], OR within field.
      importance_min, importance_max       — float, inclusive.
      confidence_min, confidence_max       — float, inclusive.
      modified_after, modified_before      — ISO date string, inclusive.

    Membership (post-fetch, against chunks.jsonl arrays):
      tags_any, tags_all                   — at-least-one / all-of.
      wikilinks_any, wikilinks_all         — at-least-one / all-of.

    Folder filter (chunk["folder"]):
      folders                              — list[str], OR within field.
    """

    # --- scalar / exact ---
    type: Optional[list[str]] = None
    status: Optional[list[str]] = None
    domain: Optional[list[str]] = None
    project: Optional[list[str]] = None
    folders: Optional[list[str]] = None

    # --- numeric ranges ---
    importance_min: Optional[float] = None
    importance_max: Optional[float] = None
    confidence_min: Optional[float] = None
    confidence_max: Optional[float] = None

    # --- date ranges (ISO YYYY-MM-DD, inclusive) ---
    modified_after: Optional[str] = None
    modified_before: Optional[str] = None

    # --- membership (arrays in chunks.jsonl) ---
    tags_any: Optional[list[str]] = None
    tags_all: Optional[list[str]] = None
    wikilinks_any: Optional[list[str]] = None
    wikilinks_all: Optional[list[str]] = None

    # --- internal: validation cache ---
    _validated: bool = field(default=False, repr=False)

    # ---- Validation -------------------------------------------------------

    def validate(self) -> None:
        """Raise ``ValueError`` on malformed input.

        Idempotent — second call is a no-op.
        """
        if self._validated:
            return
        errors: list[str] = []

        # Date strings.
        for name in ("modified_after", "modified_before"):
            v = getattr(self, name)
            if v is not None and not _is_iso_date(v):
                errors.append(f"{name}={v!r} is not a valid ISO date (YYYY-MM-DD)")
        if (
            self.modified_after
            and self.modified_before
            and self.modified_after > self.modified_before
        ):
            errors.append(
                f"modified_after ({self.modified_after}) is after "
                f"modified_before ({self.modified_before})"
            )

        # Numeric ranges.
        for lo_name, hi_name in (
            ("importance_min", "importance_max"),
            ("confidence_min", "confidence_max"),
        ):
            lo = getattr(self, lo_name)
            hi = getattr(self, hi_name)
            if lo is not None and hi is not None and lo > hi:
                errors.append(
                    f"{lo_name} ({lo}) > {hi_name} ({hi})"
                )

        # List-typed fields must be list[str] when set.
        for name in (
            "type", "status", "domain", "project", "folders",
            "tags_any", "tags_all", "wikilinks_any", "wikilinks_all",
        ):
            v = getattr(self, name)
            if v is None:
                continue
            if not isinstance(v, list):
                errors.append(f"{name} must be a list (got {type(v).__name__})")
                continue
            if not all(isinstance(x, str) and x for x in v):
                errors.append(f"{name} must contain non-empty strings")

        if errors:
            raise ValueError(
                "Invalid SearchFilters:\n  - " + "\n  - ".join(errors)
            )
        self._validated = True

    # ---- Introspection ----------------------------------------------------

    def is_empty(self) -> bool:
        """True iff no filter is set (search behaves as Slice 1)."""
        for name in (
            "type", "status", "domain", "project", "folders",
            "tags_any", "tags_all", "wikilinks_any", "wikilinks_all",
            "importance_min", "importance_max",
            "confidence_min", "confidence_max",
            "modified_after", "modified_before",
        ):
            v = getattr(self, name)
            if v is None:
                continue
            if isinstance(v, list) and not v:
                continue
            return False
        return True


# ---- Chroma where-builder --------------------------------------------------


def build_chroma_where(f: SearchFilters) -> dict[str, Any]:
    """Translate scalar / numeric / date / folder filters into a Chroma ``where``.

    Membership filters (tags / wikilinks) are deliberately excluded — those
    are applied post-fetch against the JSONL arrays because Chroma's string
    metadata does not support array contains semantics.

    Returns ``{}`` when no scalar filters are set (caller passes no ``where``).

    Combination semantics:
      * Different fields  → AND  (Chroma ``$and``).
      * Same field with multiple values → OR (Chroma ``$in``).
      * Numeric ranges → ``$gte`` / ``$lte``.
      * Date ranges on ``modified_date`` use lexicographic compare on
        ISO strings (valid because YYYY-MM-DD sorts chronologically).
    """
    f.validate()
    clauses: list[dict[str, Any]] = []

    def _list_clause(field_name: str, values: Optional[list[str]]) -> None:
        if not values:
            return
        if len(values) == 1:
            clauses.append({field_name: values[0]})
        else:
            clauses.append({field_name: {"$in": list(values)}})

    _list_clause("type", f.type)
    _list_clause("status", f.status)
    _list_clause("domain", f.domain)
    _list_clause("project", f.project)
    _list_clause("folder", f.folders)

    if f.importance_min is not None:
        clauses.append({"importance": {"$gte": float(f.importance_min)}})
    if f.importance_max is not None:
        clauses.append({"importance": {"$lte": float(f.importance_max)}})
    if f.confidence_min is not None:
        clauses.append({"confidence": {"$gte": float(f.confidence_min)}})
    if f.confidence_max is not None:
        clauses.append({"confidence": {"$lte": float(f.confidence_max)}})

    if f.modified_after:
        clauses.append({"modified_date": {"$gte": f.modified_after}})
    if f.modified_before:
        clauses.append({"modified_date": {"$lte": f.modified_before}})

    if not clauses:
        return {}
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


# ---- Pure-Python predicate (for BM25 + post-fetch tag/wikilink) -----------


def chunk_matches(chunk: dict[str, Any], f: SearchFilters) -> bool:
    """Return True iff ``chunk`` satisfies every filter in ``f``.

    Used to:
      * Filter the BM25 candidate pool (filter-then-score; see
        ``HybridSearcher._bm25_candidates``).
      * Apply tag/wikilink membership to the *vector* candidate pool
        post-fetch (those can't go through Chroma ``where``).

    Missing metadata semantics:
      * For *exact* fields (type/status/domain/project/folder) a missing
        value never matches a non-empty filter list.
      * For *numeric ranges* a missing value is treated as a non-match
        (cannot prove the bound holds).
      * For *date ranges* a missing ``modified_date`` is treated as a
        non-match.
      * For *membership* arrays a missing/empty array means the chunk
        has no tags/wikilinks at all and therefore matches no ``*_any``
        or ``*_all`` filter.
    """
    if f.is_empty():
        return True
    f.validate()

    fm = chunk.get("fm_normalized") or {}
    raw_fm = chunk.get("frontmatter") or {}

    def _scalar(name: str) -> str:
        v = fm.get(name)
        if v is None or v == "":
            v = raw_fm.get(name)
        return str(v) if v is not None else ""

    # exact / OR-within-list
    if f.type and _scalar("type") not in f.type:
        return False
    if f.status and _scalar("status") not in f.status:
        return False
    if f.domain and _scalar("domain") not in f.domain:
        return False
    if f.project and _scalar("project") not in f.project:
        return False
    if f.folders and str(chunk.get("folder", "")) not in f.folders:
        return False

    # numeric
    def _numeric(name: str) -> Optional[float]:
        v = fm.get(name)
        if v is None:
            v = raw_fm.get(name)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if f.importance_min is not None:
        v = _numeric("importance")
        if v is None or v < f.importance_min:
            return False
    if f.importance_max is not None:
        v = _numeric("importance")
        if v is None or v > f.importance_max:
            return False
    if f.confidence_min is not None:
        v = _numeric("confidence")
        if v is None or v < f.confidence_min:
            return False
    if f.confidence_max is not None:
        v = _numeric("confidence")
        if v is None or v > f.confidence_max:
            return False

    # dates (lexicographic compare on ISO YYYY-MM-DD)
    md = str(chunk.get("modified_date") or "")
    if f.modified_after and (not md or md < f.modified_after):
        return False
    if f.modified_before and (not md or md > f.modified_before):
        return False

    # membership (arrays in chunks.jsonl — never the *_flat strings)
    tags = set(chunk.get("tags") or [])
    wikilinks = set(chunk.get("wikilinks") or [])
    if f.tags_any and not (tags & set(f.tags_any)):
        return False
    if f.tags_all and not set(f.tags_all).issubset(tags):
        return False
    if f.wikilinks_any and not (wikilinks & set(f.wikilinks_any)):
        return False
    if f.wikilinks_all and not set(f.wikilinks_all).issubset(wikilinks):
        return False

    return True
