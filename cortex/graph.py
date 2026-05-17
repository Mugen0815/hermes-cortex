"""Wikilink graph for hermes-cortex hybrid search — Phase 3 / Slice 3.

Builds a Title→[chunk_ids] map from chunks.jsonl and provides 1-hop
expansion as a *third RRF channel* in :class:`HybridSearcher`.

Pipeline shape::

    Top-N base hits (BM25 ∪ Vector after filters) ─┐
                                                   ├── union → RRF channel #3
    1-hop wikilink expansion ──────────────────────┘   (weight = graph_weight)

The graph channel is a peer of BM25 / Vector in the RRF formula::

    final_rrf = w_bm25/(k+r_bm25) + w_vec/(k+r_vec) + w_graph/(k+r_graph)

Boosts (recency, importance) apply to the fused result as before.

Resolution rules
----------------
A wikilink ``[[Foo]]`` resolves to every chunk whose source file's
basename (without ``.md``) equals ``Foo``. Matching is **case-insensitive**
and trims surrounding whitespace, so ``[[foo]]`` and ``[[Foo]]`` collide
intentionally — Obsidian behaves the same way.

When the same title exists in multiple folders (rare; a deliberate
duplicate is a vault smell) we resolve to **all** matching chunks. The
graph channel ranks them by ``chunk_id`` ascending — deterministic, but
ties are unavoidable: the upstream RRF rounds them out via the other
channels' rankings.

Filters apply *post-expansion*: linked chunks that don't satisfy the
caller's :class:`SearchFilters` are dropped before they enter the graph
channel. This keeps the contract "filtered search returns only filter-
matching chunks" — the graph cannot smuggle in rejected types.

Multi-hop (``wikilink_traversal`` > 1)
--------------------------------------
We breadth-first expand from the base seeds for ``wikilink_traversal``
steps, deduplicating titles along the way and never re-visiting a seed.
The graph channel ranks deeper hops *after* shallower ones (rank order:
hop-1 results first, then hop-2, …) so a 1-hop default keeps the
implementation honest.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

log = logging.getLogger("cortex.graph")


# ---- Title→chunk map -------------------------------------------------------


class WikilinkGraph:
    """Resolve wikilink targets to chunk_ids.

    Lazy-built once per :class:`HybridSearcher` instance from the loaded
    chunks list.
    """

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._title_to_chunks: dict[str, list[str]] = {}
        self._build(chunks)

    def _build(self, chunks: list[dict[str, Any]]) -> None:
        # Map: lowercased file-basename → list of chunk_ids in that file.
        for c in chunks:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            f = str(c.get("file") or "")
            if not f:
                continue
            stem = PurePosixPath(f).stem
            key = stem.strip().lower()
            if not key:
                continue
            self._title_to_chunks.setdefault(key, []).append(cid)
        # Stable ordering inside each title bucket.
        for key in self._title_to_chunks:
            self._title_to_chunks[key].sort()

    def resolve(self, title: str) -> list[str]:
        """Return all chunk_ids whose source file is named ``title``.

        Case-insensitive, whitespace-trimmed. Empty list when nothing
        resolves — a common state when a wikilink points at a not-yet-
        created note.
        """
        if not isinstance(title, str):
            return []
        key = title.strip().lower()
        if not key:
            return []
        return list(self._title_to_chunks.get(key, ()))

    def known_titles(self) -> int:
        """Number of distinct file-basename keys (for diagnostics)."""
        return len(self._title_to_chunks)


# ---- 1-hop / N-hop expansion ----------------------------------------------


def expand(
    seeds: Iterable[str],
    graph: WikilinkGraph,
    chunk_by_id: dict[str, dict[str, Any]],
    *,
    hops: int = 1,
) -> list[str]:
    """Expand a list of seed chunk_ids by their outgoing wikilinks.

    Returns a deduplicated, *seed-excluded* list of chunk_ids ordered by
    BFS hop and then ``chunk_id`` ascending within a hop. Seeds are
    explicitly removed from the result — they came from BM25/Vector and
    should not double-count via the graph channel.

    ``hops``: how many wikilink steps to traverse. ``0`` returns ``[]``.
    """
    if hops <= 0:
        return []
    seen_seeds = {s for s in seeds}
    out: list[str] = []
    out_set: set[str] = set()
    frontier: list[str] = sorted(seen_seeds)

    for _ in range(hops):
        next_frontier: list[str] = []
        for cid in frontier:
            chunk = chunk_by_id.get(cid)
            if not chunk:
                continue
            for target in chunk.get("wikilinks") or []:
                resolved = graph.resolve(target)
                for r_cid in resolved:
                    if r_cid in seen_seeds or r_cid in out_set:
                        continue
                    out.append(r_cid)
                    out_set.add(r_cid)
                    next_frontier.append(r_cid)
        if not next_frontier:
            break
        # Sort within the hop for determinism; deeper hops follow shallow.
        next_frontier.sort()
        frontier = next_frontier
    return out


def graph_candidates(
    seeds: Iterable[str],
    graph: WikilinkGraph,
    chunk_by_id: dict[str, dict[str, Any]],
    *,
    hops: int,
    filter_predicate: Optional[Any] = None,
    pool_size: Optional[int] = None,
) -> list[tuple[str, float]]:
    """Build the graph channel's ranked candidate list.

    Returns ``[(chunk_id, score), ...]`` sorted by graph rank (BFS order).
    The "score" is a synthetic, monotonically decreasing value (1.0 / rank)
    so the standard RRF wrapper that turns score lists into ranks works
    unchanged. The actual RRF contribution comes from the rank, not this
    score — see ``HybridSearcher._fuse``.

    ``filter_predicate``: optional callable ``chunk -> bool``. Linked
    chunks for which it returns ``False`` are dropped pre-rank. We apply
    it here (not in the caller) so we never count a filtered-out node
    against the graph pool size.

    ``pool_size``: cap on returned candidates. ``None`` = unlimited.
    """
    expanded = expand(seeds, graph, chunk_by_id, hops=hops)
    if filter_predicate is not None:
        expanded = [
            cid
            for cid in expanded
            if filter_predicate(chunk_by_id.get(cid, {}))
        ]
    if pool_size is not None:
        expanded = expanded[:pool_size]
    return [(cid, 1.0 / (i + 1)) for i, cid in enumerate(expanded)]
