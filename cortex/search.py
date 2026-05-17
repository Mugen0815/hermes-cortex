"""Hybrid search for hermes-cortex — Phase 3 / Slice 1.

This slice implements the deterministic retrieval core:

    BM25 (over chunks.jsonl) + Vector (over Chroma) → RRF fusion → top_k

Slice 1 explicitly does **not** include:
  - metadata filters (numeric / exact match via Chroma `where`)
  - tag / wikilink membership filtering
  - recency / importance boosts
  - 1-hop wikilink graph expansion
  - the `cortex search` CLI

Those land in Slice 2 (filters + boosts) and Slice 3 (graph + CLI).

Architecture:

    HybridSearcher(cfg)              # cheap; loads nothing yet
        .search(query, top_k=...)    # first call lazy-loads:
                                     #   - chunks.jsonl  → in-memory BM25 index
                                     #   - chromadb client + collection
                                     # subsequent calls reuse both.

A single instance is meant to be kept alive across many queries (the
Phase-5 `vault_search` tool will hold one). chunks.jsonl is read **once**
per instance.

Determinism contract:
    Given the same chunks.jsonl + same Chroma collection + same query +
    same Config, ``search()`` returns the exact same ordered result list.
    Tie-breaks within the RRF fusion are resolved by chunk-id ascending.

Score semantics — IMPORTANT:
    - ``bm25_score``: raw BM25 score from ``rank_bm25.BM25Okapi.get_scores``.
      Higher is better. ``None`` if the chunk was not in the BM25 candidate
      pool (top ``top_k * fetch_multiplier`` BM25 hits).
    - ``vector_score``: **cosine similarity** in [-1, 1], higher is better.
      Chroma stores cosine *distance* (we set ``hnsw:space=cosine`` at
      collection-create time in the embedder), so we convert via
      ``similarity = 1 - distance``. ``None`` if not in the vector pool.
    - ``rrf_score``: weighted Reciprocal Rank Fusion score. Higher is better.
      Formula: ``Σ (channel_weight / (rrf_k + rank_in_channel))`` where
      ``rank_in_channel`` is 1-based and only counted if the chunk
      participated in that channel.
    - ``final_score``: alias for ``rrf_score`` in Slice 1; in Slice 2 this
      becomes ``rrf_score`` after applying recency/importance boosts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from cortex.boosts import apply_boosts_detailed
from cortex.config import Config
from cortex.filters import (
    SearchFilters,
    build_chroma_where,
    chunk_matches,
)
from cortex.graph import WikilinkGraph, graph_candidates
from cortex.text import tokenize_bm25

log = logging.getLogger("cortex.search")


# ---- Result type -----------------------------------------------------------


@dataclass
class SearchResult:
    """A fused-and-ranked retrieval hit.

    Both ``bm25_score`` and ``vector_score`` may be ``None`` when the chunk
    only participated in the *other* channel's candidate pool. ``rrf_score``
    is always populated.
    """

    chunk_id: str
    chunk: dict[str, Any]                 # full hydrated record from chunks.jsonl
    bm25_score: Optional[float]
    vector_score: Optional[float]         # cosine similarity (1 - chroma distance)
    bm25_rank: Optional[int]              # 1-based rank inside BM25 pool
    vector_rank: Optional[int]            # 1-based rank inside vector pool
    graph_rank: Optional[int] = None      # 1-based rank inside graph pool (Slice 3)
    rrf_score: float = 0.0
    final_score: float = 0.0

    # Extra debug info — useful when tracing why a chunk did/didn't surface.
    debug: dict[str, Any] = field(default_factory=dict)


# ---- BM25 index wrapper ----------------------------------------------------


class _BM25Index:
    """In-memory BM25 over the chunks. Lazy-built on first query.

    We tokenize using ``cortex.text.tokenize_bm25`` so the index and query
    side are normalized identically — that's the contract the Phase-2.5
    hardening locked in.
    """

    def __init__(self, chunks: list[dict[str, Any]]):
        self._chunks = chunks
        self._bm25 = None
        # Parallel arrays: i-th token list and i-th chunk id.
        self._chunk_ids: list[str] = []
        # Per-doc token set, used to filter true non-matches at query time
        # (independent of score sign — see query() docstring).
        self._token_sets: list[frozenset[str]] = []

    def _build(self) -> None:
        if self._bm25 is not None:
            return
        # Lazy import — keeps ``import cortex.search`` cheap and avoids
        # importing rank_bm25 in CLI paths that never search.
        from rank_bm25 import BM25Okapi

        tokenized: list[list[str]] = []
        for c in self._chunks:
            cid = str(c.get("id") or "")
            if not cid:
                continue
            # We embed the heading_path + text; mirror that here so the BM25
            # index sees the same surface as the vector channel.
            heading_path = c.get("heading_path") or []
            head = " / ".join(p for p in heading_path if p)
            body = c.get("text") or ""
            text = f"{head}\n\n{body}" if head else body
            tokens = tokenize_bm25(text)
            # rank_bm25 chokes on completely empty docs (zero-length corpus
            # rows can produce NaN scores). Inject a single sentinel token so
            # the row exists but never matches a real query.
            if not tokens:
                tokens = ["\x00empty"]
            tokenized.append(tokens)
            self._chunk_ids.append(cid)
            self._token_sets.append(frozenset(tokens))

        if not tokenized:
            # Empty corpus — keep _bm25 None and let query() short-circuit.
            log.info("BM25 index built from empty corpus")
            return

        self._bm25 = BM25Okapi(tokenized)

    def query(
        self,
        q: str,
        n: int,
        *,
        allowed_ids: Optional[set[str]] = None,
    ) -> list[tuple[str, float]]:
        """Return up to ``n`` (chunk_id, raw_bm25_score) pairs, score desc.

        Empty / whitespace / unsupported queries return ``[]``. Ties are
        broken by ``chunk_id`` ascending so output is deterministic.

        ``allowed_ids`` (Slice 2) constrains the candidate pool **before**
        truncation: BM25 is scored over the full corpus (cheap — vectorized
        over a few hundred-to-few-thousand docs) and then we keep only the
        top ``n`` rows whose chunk_id is in ``allowed_ids``. This is the
        "filter-then-truncate" guarantee — a chunk that satisfies the
        filter but ranks #80 in BM25 will still appear when ``n=50`` if the
        first 79 rows are filter-rejects. No overfetch loop needed.
        ``allowed_ids=None`` (default) means "no filter".

        **Scope of the token-overlap filter: BM25 channel only.**

        Match filter: we keep docs that share at least one token with the
        query, regardless of score sign. This handles the small-corpus
        edge case where ``rank_bm25``'s IDF can produce zero or negative
        scores for matching docs (when df > N/2). A score-based filter
        would silently eat those hits.

        This filter is strictly internal to ``_BM25Index`` and has no
        effect whatsoever on the vector channel. Vector candidates are
        collected independently in ``HybridSearcher._vector_candidates()``
        and bypass this class entirely. A chunk that Chroma returns as a
        semantic hit — even if it shares zero tokens with the query — will
        still appear in ``_fuse()`` and therefore in the final results.
        """
        self._build()
        if self._bm25 is None or n <= 0:
            return []
        tokens = tokenize_bm25(q)
        if not tokens:
            return []
        query_token_set = frozenset(tokens)
        scores = self._bm25.get_scores(tokens)
        pairs: list[tuple[str, float]] = []
        for cid, sc, doc_tokens in zip(
            self._chunk_ids, (float(s) for s in scores), self._token_sets
        ):
            if not (query_token_set & doc_tokens):
                continue
            if allowed_ids is not None and cid not in allowed_ids:
                continue
            pairs.append((cid, sc))
        # Deterministic ordering: score desc, then chunk_id asc.
        pairs.sort(key=lambda p: (-p[1], p[0]))
        return pairs[:n]


# ---- Hybrid searcher -------------------------------------------------------


class HybridSearcher:
    """Bundle of BM25 + Vector retrieval, fused with weighted RRF.

    Lazy-loads chunks.jsonl and the Chroma collection on first ``search()``.
    Both stay loaded for the lifetime of the instance; pass the same
    ``HybridSearcher`` around if you query repeatedly.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._chunks: Optional[list[dict[str, Any]]] = None
        self._chunk_by_id: dict[str, dict[str, Any]] = {}
        self._bm25: Optional[_BM25Index] = None
        self._collection = None
        self._graph: Optional[WikilinkGraph] = None
        self._loaded = False
        # Test/diagnostic counters.
        self._load_calls = 0

    # ---- Lazy loading -----------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_calls += 1
        self._chunks = self._load_chunks()
        self._chunk_by_id = {
            str(c["id"]): c for c in self._chunks if c.get("id")
        }
        self._bm25 = _BM25Index(self._chunks)
        self._graph = WikilinkGraph(self._chunks)
        self._collection = self._open_collection()
        self._loaded = True

    def _load_chunks(self) -> list[dict[str, Any]]:
        path = self.cfg.index.chunks_path
        if not path.exists():
            log.warning("chunks.jsonl not found at %s; BM25 will be empty", path)
            return []
        out: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as e:
                    log.warning("Skipping malformed chunks.jsonl line: %s", e)
        return out

    def _open_collection(self):
        # Imported lazily so a missing chromadb dep doesn't blow up at import.
        try:
            import chromadb
        except ImportError:
            log.warning("chromadb not installed; vector search disabled")
            return None
        try:
            client = chromadb.PersistentClient(path=str(self.cfg.index.chroma_path))
            return client.get_or_create_collection(name=self.cfg.index.collection)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not open Chroma collection: %s", e)
            return None

    # ---- Channels ---------------------------------------------------------

    def _bm25_candidates(
        self,
        query: str,
        pool_size: int,
        allowed_ids: Optional[set[str]] = None,
    ) -> list[tuple[str, float]]:
        assert self._bm25 is not None
        return self._bm25.query(query, pool_size, allowed_ids=allowed_ids)

    def _vector_candidates(
        self,
        query: str,
        pool_size: int,
        where: Optional[dict[str, Any]] = None,
    ) -> list[tuple[str, float]]:
        """Return ``(chunk_id, cosine_similarity)`` pairs, similarity desc.

        ``where`` is a Chroma-native filter dict (built by
        ``cortex.filters.build_chroma_where``). When passed, Chroma applies
        the filter pre-search, so the returned pool is already
        scalar-filtered. Membership filters (tags / wikilinks) cannot be
        expressed in Chroma metadata and are applied post-fetch by the
        caller via ``cortex.filters.chunk_matches``.

        We embed the query with the same model that built the collection.
        That model is recorded as collection-level metadata, so we *could*
        verify it here — but the embed-time guard already raises on
        mismatch, and re-checking on every query is wasteful.
        """
        if self._collection is None or pool_size <= 0:
            return []
        if not query or not query.strip():
            return []

        # Embed query lazily. We import Embedder here (not at module top)
        # so search.py is cheap to import.
        from cortex.embedder import Embedder, detect_device

        device = detect_device(self.cfg.embeddings.device)
        embedder = Embedder(self.cfg.embeddings.model, device)
        try:
            qvec = embedder.encode([query])[0]
        except Exception as e:  # noqa: BLE001
            log.warning("Query embedding failed: %s", e)
            return []

        try:
            kwargs: dict[str, Any] = {
                "query_embeddings": [qvec],
                "n_results": pool_size,
                "include": ["distances"],
            }
            if where:
                kwargs["where"] = where
            res = self._collection.query(**kwargs)
        except Exception as e:  # noqa: BLE001
            log.warning("Chroma query failed: %s", e)
            return []

        ids_batches = res.get("ids") or []
        dist_batches = res.get("distances") or []
        if not ids_batches or not dist_batches:
            return []
        ids = ids_batches[0]
        dists = dist_batches[0]

        # Convert cosine distance → cosine similarity = 1 - distance.
        pairs = [(str(cid), 1.0 - float(d)) for cid, d in zip(ids, dists)]
        pairs.sort(key=lambda p: (-p[1], p[0]))
        return pairs

    # ---- Fusion -----------------------------------------------------------

    @staticmethod
    def _ranked_map(pairs: list[tuple[str, float]]) -> dict[str, tuple[int, float]]:
        """``[(id, score), …]`` (already sorted desc) → ``{id: (rank_1based, score)}``."""
        return {cid: (i + 1, sc) for i, (cid, sc) in enumerate(pairs)}

    def _fuse(
        self,
        bm25: list[tuple[str, float]],
        vector: list[tuple[str, float]],
        graph: Optional[list[tuple[str, float]]] = None,
        *,
        apply_boost: bool = True,
        now: Optional["date"] = None,  # type: ignore[name-defined]
        query: str | None = None,
    ) -> list[SearchResult]:
        bm25_map = self._ranked_map(bm25)
        vec_map = self._ranked_map(vector)
        graph_map = self._ranked_map(graph or [])
        rrf_k = self.cfg.search.rrf_k
        w_bm25 = self.cfg.search.bm25_weight
        w_vec = self.cfg.search.vector_weight
        w_graph = self.cfg.search.graph_weight

        all_ids = set(bm25_map) | set(vec_map) | set(graph_map)
        results: list[SearchResult] = []
        for cid in all_ids:
            br = bm25_map.get(cid)
            vr = vec_map.get(cid)
            gr = graph_map.get(cid)
            score = 0.0
            if br is not None:
                score += w_bm25 / (rrf_k + br[0])
            if vr is not None:
                score += w_vec / (rrf_k + vr[0])
            if gr is not None:
                score += w_graph / (rrf_k + gr[0])
            chunk = self._chunk_by_id.get(cid, {"id": cid})
            debug: dict[str, Any] = {}
            if apply_boost:
                applied = apply_boosts_detailed(
                    score, chunk, self.cfg.search, now=now, query=query
                )
                final = applied.final_score
                debug["recency_factor"] = applied.recency_factor
                debug["importance_factor"] = applied.importance_factor
                debug["raw_boost_multiplier"] = applied.raw_boost_multiplier
                debug["boost_multiplier"] = applied.boost_multiplier
                debug["boost_capped"] = applied.boost_capped
                debug["quality_factor"] = applied.quality_factor
                debug["quality_reason"] = applied.quality_reason
            else:
                final = score
                debug["recency_factor"] = 0.0
                debug["importance_factor"] = 0.0
                debug["raw_boost_multiplier"] = 1.0
                debug["boost_multiplier"] = 1.0
                debug["boost_capped"] = False
                debug["quality_factor"] = 1.0
                debug["quality_reason"] = "boost_disabled"
            results.append(
                SearchResult(
                    chunk_id=cid,
                    chunk=chunk,
                    bm25_score=br[1] if br is not None else None,
                    vector_score=vr[1] if vr is not None else None,
                    bm25_rank=br[0] if br is not None else None,
                    vector_rank=vr[0] if vr is not None else None,
                    graph_rank=gr[0] if gr is not None else None,
                    rrf_score=score,
                    final_score=final,
                    debug=debug,
                )
            )
        # Deterministic order: final_score desc, then chunk_id asc.
        results.sort(key=lambda r: (-r.final_score, r.chunk_id))
        return results

    # ---- Public API -------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        filters: Optional[SearchFilters] = None,
        apply_boost: Optional[bool] = None,
        now: Optional["date"] = None,  # type: ignore[name-defined]
    ) -> list[SearchResult]:
        """Run hybrid retrieval.

        Empty/whitespace queries return ``[]`` without touching either channel.

        Filter behavior (Slice 2):
          * Scalar / numeric / date filters → Chroma ``where`` (vector channel
            pre-filter) + pure-Python predicate (BM25 channel pre-score).
          * Tag / wikilink membership → post-fetch on both channels (Chroma
            metadata cannot express array contains).
          * BM25 uses *filter-then-truncate*: the full corpus is scored,
            filter is applied, then the top ``pool_size`` survivors are
            kept. A relevant hit ranked outside the unfiltered top-pool but
            inside the filtered top-pool will surface.

        ``apply_boost``: defaults to ``True`` if recency, importance, or the
        link/related chunk penalty is enabled in config. Pass ``False`` to
        disable all score adjustments for a single call (useful for debugging
        / A/B tests).

        ``now``: injectable "today" for deterministic recency tests. Defaults
        to ``date.today()`` inside ``cortex.boosts``.
        """
        self._ensure_loaded()
        if not query or not query.strip():
            return []
        k = int(top_k) if top_k is not None else self.cfg.search.top_k
        if k <= 0:
            return []

        # Normalize filters input.
        f = filters or SearchFilters()
        f.validate()

        # Determine boost flag.
        if apply_boost is None:
            apply_boost = (
                self.cfg.search.recency_boost
                or self.cfg.search.importance_boost
                or self.cfg.search.link_chunk_penalty < 1.0
            )

        pool_size = k * max(1, self.cfg.search.fetch_multiplier)

        # Build the BM25 allow-list (filter-then-score). When no filters are
        # set we pass None to skip the per-doc membership check entirely.
        allowed_ids: Optional[set[str]] = None
        if not f.is_empty():
            allowed_ids = {
                cid for cid, c in self._chunk_by_id.items() if chunk_matches(c, f)
            }

        bm25 = self._bm25_candidates(query, pool_size, allowed_ids=allowed_ids)

        # Vector channel: scalar/range/date filters via Chroma where; then
        # post-fetch predicate to enforce membership filters that Chroma
        # cannot express (and to defend against any drift between the two
        # representations).
        where = build_chroma_where(f) if not f.is_empty() else None
        vector = self._vector_candidates(query, pool_size, where=where)
        if not f.is_empty() and vector:
            vector = [
                (cid, sc)
                for (cid, sc) in vector
                if chunk_matches(self._chunk_by_id.get(cid, {}), f)
            ]

        # Graph channel (Slice 3): 1-hop wikilink expansion of the union of
        # base hits. Filters apply post-expansion so the graph cannot
        # surface filter-rejected chunks. Disabled when graph_weight=0 or
        # wikilink_traversal=0 — both spellings supported.
        hops = int(self.cfg.search.wikilink_traversal)
        w_graph = float(self.cfg.search.graph_weight)
        graph: list[tuple[str, float]] = []
        if hops > 0 and w_graph > 0 and self._graph is not None:
            seeds = {cid for cid, _ in bm25} | {cid for cid, _ in vector}
            if seeds:
                pred = (
                    (lambda c: chunk_matches(c, f))
                    if not f.is_empty()
                    else None
                )
                graph = graph_candidates(
                    seeds,
                    self._graph,
                    self._chunk_by_id,
                    hops=hops,
                    filter_predicate=pred,
                    pool_size=pool_size,
                )

        fused = self._fuse(
            bm25, vector, graph, apply_boost=apply_boost, now=now, query=query
        )
        return fused[:k]
