"""Tool API for Hermes integration — Phase 5.

Three pure-Python functions, designed to be wrapped by a Hermes tool
registration shim (see ``plugins/hermes/cortex_vault.py``) or any other
agent integration:

  - ``vault_search``         — hybrid search → JSON-serializable hit list
  - ``vault_read_note``      — load a full note (frontmatter + body)
  - ``vault_build_context``  — search + context build → Markdown blob

All three:
  * accept a single optional ``config_path`` and load config lazily, so a
    long-lived agent process holds no Cortex state until first call;
  * return plain dicts (JSON-serializable) — caller decides framing;
  * raise ``CortexToolError`` on bad input or operational failure
    (Hermes adapters translate to JSON ``{"error": ...}`` envelopes);
  * accept the same filter shape that ``cortex search`` exposes — flat
    keyword args matching :class:`SearchFilters` field names, where list
    fields take Python lists (``["fact", "decision"]``).

Singleton-ness:
    Building a :class:`HybridSearcher` reads ``chunks.jsonl`` and opens
    Chroma. We don't want that on every call. The module keeps a small
    LRU cache of ``(config_path, file_mtime)`` → loaded searcher, so
    repeated calls within the same agent process reuse state. When
    ``config.yaml`` or ``chunks.jsonl`` change on disk the cache key
    rotates automatically.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from cortex.config import Config, load_config
from cortex.context import ContextBuilder
from cortex.filters import SearchFilters
from cortex.search import HybridSearcher

log = logging.getLogger("cortex.plugin")


class CortexToolError(Exception):
    """Tool-level failure surfaced to the agent.

    Hermes adapters translate this to ``{"error": str(e)}``.
    """


# ---- Config + searcher cache ----------------------------------------------


@lru_cache(maxsize=8)
def _cached_searcher(cfg_path_str: str, mtime_key: float) -> tuple[Config, HybridSearcher]:
    """Return ``(cfg, searcher)`` keyed by ``(config_path, max_mtime)``.

    ``mtime_key`` is the latest mtime of config.yaml or chunks.jsonl —
    when either changes on disk the cache key rotates and a fresh
    searcher is built. Bounded LRU keeps memory predictable when an
    agent juggles multiple profiles.
    """
    cfg_path = Path(cfg_path_str) if cfg_path_str else None
    cfg = load_config(cfg_path) if cfg_path else load_config()
    return cfg, HybridSearcher(cfg)


def _resolve_state(config_path: Optional[str]) -> tuple[Config, HybridSearcher]:
    cfg_path_str = str(Path(config_path).expanduser().resolve()) if config_path else ""
    # mtime key: config + chunks.jsonl (chroma path is opened by the searcher
    # on first use; mutations there don't require a rebuild of our caches).
    mtime = 0.0
    for p in (cfg_path_str, ""):
        try:
            if p:
                mtime = max(mtime, Path(p).stat().st_mtime)
        except OSError:
            pass
    # Best-effort: also key on chunks.jsonl mtime if discoverable cheaply.
    # We do this *after* a first config load, so we accept one cache miss
    # per fresh config_path. The trade-off is fine — alternative is to
    # parse YAML twice.
    cfg, searcher = _cached_searcher(cfg_path_str, mtime)
    try:
        chunks_mtime = cfg.index.chunks_path.stat().st_mtime
        if chunks_mtime > mtime:
            cfg, searcher = _cached_searcher(cfg_path_str, chunks_mtime)
    except OSError:
        pass
    return cfg, searcher


def reset_cache() -> None:
    """Drop the cached searchers. Useful in tests and after re-indexing."""
    _cached_searcher.cache_clear()


# ---- Filter construction ---------------------------------------------------


_FILTER_FIELDS = {
    "type", "status", "domain", "project", "folders",
    "importance_min", "importance_max",
    "confidence_min", "confidence_max",
    "modified_after", "modified_before",
    "tags_any", "tags_all",
    "wikilinks_any", "wikilinks_all",
}


def _build_filters(filters: Optional[dict[str, Any]]) -> SearchFilters:
    """Translate a flat filter dict into a validated :class:`SearchFilters`.

    Unknown keys raise :class:`CortexToolError` so agents get fast feedback
    rather than silently-ignored typos.
    """
    if not filters:
        return SearchFilters()
    extra = set(filters) - _FILTER_FIELDS
    if extra:
        raise CortexToolError(
            f"Unknown filter field(s): {sorted(extra)}. "
            f"Valid fields: {sorted(_FILTER_FIELDS)}"
        )
    f = SearchFilters(**{k: v for k, v in filters.items() if v is not None})
    try:
        f.validate()
    except ValueError as e:
        raise CortexToolError(str(e)) from e
    return f


# ---- Result shaping --------------------------------------------------------


def _result_to_dict(r: Any) -> dict[str, Any]:
    """Render a :class:`SearchResult` as a JSON-safe dict.

    We deliberately surface the channel ranks (bm25/vector/graph) and
    the boost debug so agents can reason about *why* a chunk surfaced.
    """
    chunk = r.chunk
    return {
        "chunk_id": r.chunk_id,
        "file": chunk.get("file"),
        "folder": chunk.get("folder"),
        "heading_path": chunk.get("heading_path") or [],
        "heading": chunk.get("heading"),
        "text": chunk.get("text") or "",
        "tags": chunk.get("tags") or [],
        "wikilinks": chunk.get("wikilinks") or [],
        "fm_normalized": chunk.get("fm_normalized") or {},
        "modified_date": chunk.get("modified_date"),
        "scores": {
            "final": r.final_score,
            "rrf": r.rrf_score,
            "bm25": r.bm25_score,
            "vector": r.vector_score,
        },
        "ranks": {
            "bm25": r.bm25_rank,
            "vector": r.vector_rank,
            "graph": r.graph_rank,
        },
        "debug": dict(r.debug or {}),
    }


# ---- Tool 1: vault_search --------------------------------------------------


def vault_search(
    query: str,
    *,
    top_k: Optional[int] = None,
    filters: Optional[dict[str, Any]] = None,
    apply_boost: Optional[bool] = None,
    config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Run hybrid search and return a JSON-friendly result envelope.

    Args:
        query: free-text query
        top_k: number of results (default: cfg.search.top_k)
        filters: dict matching :class:`SearchFilters` fields (e.g.
            ``{"type": ["fact"], "tags_any": ["jarvis"]}``)
        apply_boost: ``True``/``False`` forces; ``None`` honors config
        config_path: path to ``config.yaml`` (default: standard lookup)

    Returns:
        ``{"query": ..., "count": ..., "results": [...]}`` where each
        result is the dict from ``_result_to_dict``.
    """
    if not isinstance(query, str) or not query.strip():
        raise CortexToolError("query must be a non-empty string")
    cfg, searcher = _resolve_state(config_path)
    f = _build_filters(filters)
    try:
        results = searcher.search(
            query, top_k=top_k, filters=f, apply_boost=apply_boost
        )
    except ValueError as e:
        raise CortexToolError(str(e)) from e
    return {
        "query": query,
        "count": len(results),
        "results": [_result_to_dict(r) for r in results],
    }


# ---- Tool 2: vault_read_note ----------------------------------------------


def vault_read_note(
    file: str,
    *,
    heading_path: Optional[list[str]] = None,
    config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Read a full note from the vault.

    Args:
        file: vault-relative posix path (e.g. ``"10_facts/Foo.md"``) or
            absolute path that lies under the vault root.
        heading_path: when provided, return only the section matching
            this exact heading_path (as recorded in chunks.jsonl). If
            no chunk matches, raises :class:`CortexToolError` rather
            than returning an empty body — agents should treat this as
            a hard miss.
        config_path: standard lookup if omitted.

    Returns:
        ``{"file": str, "exists": bool, "content": str, "frontmatter": dict,
        "tags": [...], "wikilinks": [...], "modified_date": str|None,
        "selected_heading_path": [...] | None}``

    The full ``content`` is the file body verbatim (after frontmatter
    stripping). When ``heading_path`` is given, ``content`` contains
    only the matching chunk's text and ``selected_heading_path`` echoes
    back the resolved path.
    """
    if not isinstance(file, str) or not file.strip():
        raise CortexToolError("file must be a non-empty string")
    cfg, searcher = _resolve_state(config_path)

    # Resolve to vault-relative posix.
    vault_root = cfg.vault.path.resolve()
    candidate = Path(file).expanduser()
    if not candidate.is_absolute():
        candidate = (vault_root / file).resolve()
    else:
        candidate = candidate.resolve()
    try:
        rel = candidate.relative_to(vault_root).as_posix()
    except ValueError:
        raise CortexToolError(
            f"file {file!r} is outside the vault root {vault_root}"
        )

    # If heading_path requested, look it up via the loaded chunks (cheap —
    # already in memory) instead of re-parsing the file.
    if heading_path is not None:
        searcher._ensure_loaded()
        match = next(
            (
                c for c in (searcher._chunks or [])
                if c.get("file") == rel
                and (c.get("heading_path") or []) == list(heading_path)
            ),
            None,
        )
        if match is None:
            raise CortexToolError(
                f"No chunk in {rel!r} with heading_path={heading_path!r}"
            )
        return {
            "file": rel,
            "exists": True,
            "content": match.get("text") or "",
            "frontmatter": match.get("frontmatter") or {},
            "tags": match.get("tags") or [],
            "wikilinks": match.get("wikilinks") or [],
            "modified_date": match.get("modified_date"),
            "selected_heading_path": list(heading_path),
        }

    # Whole-file path: read from disk so we get the original body.
    if not candidate.exists() or not candidate.is_file():
        return {
            "file": rel,
            "exists": False,
            "content": "",
            "frontmatter": {},
            "tags": [],
            "wikilinks": [],
            "modified_date": None,
            "selected_heading_path": None,
        }
    try:
        from cortex.indexer import parse_frontmatter
        raw = candidate.read_text(encoding="utf-8")
    except OSError as e:
        raise CortexToolError(f"Could not read {rel}: {e}") from e
    fm, body = parse_frontmatter(raw)

    # Pull tags / wikilinks from the indexed chunks if available — they're
    # already deduped and code-fence-stripped. Fallback to empty lists if
    # the file isn't indexed yet.
    searcher._ensure_loaded()
    file_chunks = [
        c for c in (searcher._chunks or [])
        if c.get("file") == rel
    ]
    tags: list[str] = []
    wikilinks: list[str] = []
    modified_date: Optional[str] = None
    if file_chunks:
        tags = list(file_chunks[0].get("tags") or [])
        modified_date = file_chunks[0].get("modified_date")
        seen: dict[str, None] = {}
        for c in file_chunks:
            for w in c.get("wikilinks") or []:
                if w not in seen:
                    seen[w] = None
        wikilinks = list(seen.keys())

    return {
        "file": rel,
        "exists": True,
        "content": body,
        "frontmatter": fm,
        "tags": tags,
        "wikilinks": wikilinks,
        "modified_date": modified_date,
        "selected_heading_path": None,
    }


# ---- Tool 3: vault_build_context ------------------------------------------


def vault_build_context(
    query: str,
    *,
    top_k: Optional[int] = None,
    budget: Optional[int] = None,
    filters: Optional[dict[str, Any]] = None,
    apply_boost: Optional[bool] = None,
    include_hermes_memory: Optional[bool] = None,
    config_path: Optional[str] = None,
) -> dict[str, Any]:
    """Search + build a Markdown context blob under a token budget.

    Wraps :class:`HybridSearcher` + :class:`ContextBuilder` in a single
    call. Returns a dict with both the rendered text and full
    diagnostics so the agent can decide whether to use it as-is or
    re-query with different parameters.

    Args:
        query: free-text query
        top_k: number of results to feed the builder (default cfg)
        budget: override token budget for this call (default: cfg.context_builder.token_budget)
        filters: same shape as ``vault_search``
        apply_boost: see ``vault_search``
        include_hermes_memory: per-call override of
            ``context_builder.include_hermes_memory``
        config_path: standard lookup if omitted

    Returns:
        ``{"text": str, "tokens_used": int, "tokens_budget": int,
        "chunks_included": [...], "chunks_skipped_oversize": [...],
        "hermes_memory_included": bool, "hermes_user_included": bool,
        "citation_count": int, "query": str}``
    """
    if not isinstance(query, str) or not query.strip():
        raise CortexToolError("query must be a non-empty string")
    cfg, searcher = _resolve_state(config_path)

    if include_hermes_memory is not None:
        cfg.context_builder.include_hermes_memory = bool(include_hermes_memory)

    f = _build_filters(filters)
    try:
        results = searcher.search(
            query, top_k=top_k, filters=f, apply_boost=apply_boost
        )
    except ValueError as e:
        raise CortexToolError(str(e)) from e

    builder = ContextBuilder(cfg)
    ctx = builder.build(results, budget_override=budget)

    return {
        "query": query,
        "text": ctx.text,
        "tokens_used": ctx.tokens_used,
        "tokens_budget": ctx.tokens_budget,
        "chunks_included": list(ctx.chunks_included),
        "chunks_skipped_oversize": list(ctx.chunks_skipped_oversize),
        "hermes_memory_included": ctx.hermes_memory_included,
        "hermes_user_included": ctx.hermes_user_included,
        "citation_count": ctx.citation_count,
    }
