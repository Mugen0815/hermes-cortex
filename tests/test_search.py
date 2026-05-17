"""Tests for cortex.search — Phase 3 Slice 1 (BM25 + Vector + RRF).

We mock out the Chroma vector channel so tests run without a real
sentence-transformers model. The BM25 channel runs against the real
``rank_bm25`` package (it's a project dependency, no network needed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from cortex.config import (
    Config,
    ContextBuilderConfig,
    EmbeddingsConfig,
    HermesMemoryConfig,
    IndexConfig,
    SearchConfig,
    VaultConfig,
)
from cortex.search import HybridSearcher, SearchResult, _BM25Index


# ---- Fixtures --------------------------------------------------------------


def make_config(
    tmp_path: Path,
    *,
    top_k: int = 5,
    fetch_multiplier: int = 5,
    bm25_weight: float = 0.5,
    vector_weight: float = 0.5,
    rrf_k: int = 60,
) -> Config:
    return Config(
        vault=VaultConfig(path=tmp_path / "vault"),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(model="test-model", device="cpu"),
        search=SearchConfig(
            top_k=top_k,
            bm25_weight=bm25_weight,
            vector_weight=vector_weight,
            rrf_k=rrf_k,
            fetch_multiplier=fetch_multiplier,
        ),
        context_builder=ContextBuilderConfig(),
    )


def make_chunk(
    cid: str,
    text: str,
    *,
    heading_path: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": cid,
        "file": cid.split("#", 1)[0],
        "folder": "10_facts",
        "heading": heading_path[-1] if heading_path else None,
        "heading_path": heading_path or [],
        "text": text,
        "tags": tags or [],
        "wikilinks": [],
        "frontmatter": {},
        "fm_normalized": {
            "type": "fact",
            "status": "active",
            "domain": "",
            "project": "",
            "stability": "stable",
            "tags": tags or [],
            "confidence": 0.5,
            "importance": 3.0,
            "last_verified": "",
            "created": "",
            "related": [],
        },
        "modified": "2026-01-01T00:00:00",
        "modified_date": "2026-01-01",
        "content_hash": f"hash-{cid}",
        "char_len": len(text),
        "token_estimate": max(1, len(text) // 4),
    }


SAMPLE_CHUNKS: list[dict[str, Any]] = [
    make_chunk("a.md#one", "memory model and lifecycle of caches",
               heading_path=["Memory Model"]),
    make_chunk("a.md#two", "deployment runbook for kubernetes clusters",
               heading_path=["Deploy"]),
    make_chunk("b.md#one", "embedding pipeline with chroma vector store",
               heading_path=["Embedding"]),
    make_chunk("b.md#two", "obsidian vault organization conventions",
               heading_path=["Vault"]),
    make_chunk("c.md#one", "memory and embedding hybrid retrieval",
               heading_path=["Memory Hybrid"]),
]


def write_chunks_file(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c) + "\n")


class FakeCollection:
    """Minimal Chroma collection stand-in — only `query()` is exercised here.

    ``vector_results`` is set per-test: a list of (chunk_id, distance)
    that the fake returns in order. We do NOT honor n_results truncation
    in the fake — tests that need pool-size behaviour set up the list to
    match.
    """

    def __init__(self) -> None:
        self.vector_results: list[tuple[str, float]] = []
        self.query_calls: list[dict[str, Any]] = []

    def query(self, query_embeddings, n_results, include=None, **kw):
        self.query_calls.append(
            {"n_results": n_results, "include": include}
        )
        ids = [cid for cid, _ in self.vector_results[:n_results]]
        dists = [d for _, d in self.vector_results[:n_results]]
        return {"ids": [ids], "distances": [dists]}


@pytest.fixture
def fake_chroma_and_st(monkeypatch: pytest.MonkeyPatch):
    """Patch chromadb + sentence_transformers with deterministic fakes.

    Returns the FakeCollection so tests can rig vector results per-call.
    """
    fake_collection = FakeCollection()

    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )

    fake_st = MagicMock()
    fake_model = MagicMock()
    import numpy as np

    # Embedding doesn't matter for the fake — Chroma never sees it because
    # FakeCollection returns canned results regardless. We still produce a
    # valid-shaped vector so the embedder code path doesn't blow up.
    fake_model.encode.side_effect = lambda texts, **kw: np.array(
        [[0.1, 0.2, 0.3, 0.4]] * len(texts)
    )
    fake_model.get_sentence_embedding_dimension.return_value = 4
    fake_st.SentenceTransformer.return_value = fake_model

    monkeypatch.setitem(__import__("sys").modules, "chromadb", fake_chromadb)
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)
    return fake_collection


# ---- _BM25Index ------------------------------------------------------------


def test_bm25_index_returns_matching_docs() -> None:
    idx = _BM25Index(SAMPLE_CHUNKS)
    hits = idx.query("memory", n=10)
    ids = [cid for cid, _ in hits]
    assert "a.md#one" in ids
    assert "c.md#one" in ids
    # A chunk with no "memory" token should not appear.
    assert "b.md#two" not in ids


def test_bm25_index_empty_query() -> None:
    idx = _BM25Index(SAMPLE_CHUNKS)
    assert idx.query("", n=10) == []
    assert idx.query("   ", n=10) == []
    # Punctuation-only query → no tokens after normalize.
    assert idx.query("!!!", n=10) == []


def test_bm25_index_handles_empty_corpus() -> None:
    idx = _BM25Index([])
    assert idx.query("anything", n=5) == []


def test_bm25_index_deterministic_tie_break() -> None:
    """Equal scores must tie-break on chunk_id ascending.

    We need a non-degenerate corpus (otherwise BM25's IDF collapses to 0
    when every doc contains the query terms). The three target docs share
    the matching content; the filler docs give IDF something to work with.
    """
    chunks = [
        make_chunk("z.md#x", "alpha beta"),
        make_chunk("a.md#x", "alpha beta"),
        make_chunk("m.md#x", "alpha beta"),
        make_chunk("filler-1.md#x", "completely unrelated content one"),
        make_chunk("filler-2.md#x", "completely unrelated content two"),
        make_chunk("filler-3.md#x", "completely unrelated content three"),
    ]
    idx = _BM25Index(chunks)
    hits = idx.query("alpha beta", n=10)
    target_ids = [cid for cid, _ in hits if cid.endswith(".md#x") and not cid.startswith("filler")]
    # The three "alpha beta" docs have identical scores → id-ascending order.
    assert target_ids == ["a.md#x", "m.md#x", "z.md#x"]


# ---- HybridSearcher: BM25-only and Vector-only ----------------------------


def test_search_bm25_only_hit_has_no_vector_score(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A chunk that BM25 finds but vector doesn't gets vector_score=None."""
    cfg = make_config(tmp_path, top_k=3)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = []  # vector channel returns nothing

    s = HybridSearcher(cfg)
    results = s.search("memory")

    assert results, "expected BM25 hits"
    for r in results:
        assert r.bm25_score is not None
        assert r.bm25_rank is not None and r.bm25_rank >= 1
        assert r.vector_score is None
        assert r.vector_rank is None
        # rrf_score is bm25-only contribution
        assert r.rrf_score == pytest.approx(
            cfg.search.bm25_weight / (cfg.search.rrf_k + r.bm25_rank)
        )


def test_search_vector_only_hit_has_no_bm25_score(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A chunk that only the vector channel returns gets bm25_score=None."""
    cfg = make_config(tmp_path, top_k=3)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    # Query token won't match BM25 (none of our chunks contain "xenon").
    # But we rig the vector channel to return b.md#two anyway.
    fake_chroma_and_st.vector_results = [("b.md#two", 0.2), ("b.md#one", 0.5)]

    s = HybridSearcher(cfg)
    results = s.search("xenon")

    assert results
    for r in results:
        assert r.bm25_score is None
        assert r.bm25_rank is None
        assert r.vector_score is not None
        assert r.vector_rank is not None and r.vector_rank >= 1


def test_search_vector_semantic_hit_without_token_overlap(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """The BM25 token-overlap filter is BM25-internal and MUST NOT block
    vector-only semantic hits.

    We set up a chunk whose text shares zero tokens with the query.
    BM25 will rightfully return nothing for that chunk. But the vector
    channel (fake Chroma) returns it as the top semantic hit. The fused
    result must still contain the chunk — proving that the token-overlap
    filter in _BM25Index does not act as a global gate on the search.
    """
    cfg = make_config(tmp_path, top_k=5)
    # Chunk text has NO overlap with query "memory cache".
    no_overlap = make_chunk(
        "semantic.md#hit",
        "completely different words: photosynthesis and molecular biology",
        heading_path=["Semantic Hit"],
    )
    other = make_chunk("other.md#one", "memory cache lifecycle", heading_path=["Cache"])
    write_chunks_file(cfg.index.chunks_path, [no_overlap, other])

    # Vector channel returns the semantically-matched no-overlap chunk first.
    fake_chroma_and_st.vector_results = [
        ("semantic.md#hit", 0.05),  # distance 0.05 → similarity 0.95
        ("other.md#one", 0.40),
    ]

    s = HybridSearcher(cfg)
    results = s.search("memory cache")
    by_id = {r.chunk_id: r for r in results}

    # "other.md#one" matches BM25 and vector → appears.
    assert "other.md#one" in by_id

    # "semantic.md#hit" shares zero tokens with "memory cache" → BM25 gives
    # it nothing. But the vector channel returned it → it MUST appear.
    assert "semantic.md#hit" in by_id, (
        "Vector-only semantic hit without token overlap was filtered out — "
        "the token-overlap filter must be BM25-internal only."
    )
    sem = by_id["semantic.md#hit"]
    assert sem.bm25_score is None, "no BM25 match expected for no-overlap chunk"
    assert sem.vector_score is not None
    assert sem.vector_score == pytest.approx(1.0 - 0.05)  # 1 - chroma_distance


# ---- HybridSearcher: cross-channel chunks rank higher ---------------------


def test_search_chunk_in_both_channels_gets_higher_rrf(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A chunk that appears in BOTH channels must outrank one that's only in one.

    We construct: c.md#one is in BM25 (matches "memory") AND vector.
    a.md#two is in vector only. b.md#two is in BM25 only (we'll force it).
    """
    cfg = make_config(tmp_path, top_k=10, bm25_weight=1.0, vector_weight=1.0)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    # Vector returns c.md#one at rank 1, a.md#two at rank 2.
    fake_chroma_and_st.vector_results = [("c.md#one", 0.1), ("a.md#two", 0.4)]

    s = HybridSearcher(cfg)
    results = s.search("memory")
    by_id = {r.chunk_id: r for r in results}

    # c.md#one: in both channels → should be ranked highest.
    assert "c.md#one" in by_id
    c_one = by_id["c.md#one"]
    assert c_one.bm25_score is not None and c_one.vector_score is not None

    # All single-channel chunks must have strictly lower RRF score.
    for r in results:
        if r.chunk_id == "c.md#one":
            continue
        assert r.rrf_score < c_one.rrf_score, (
            f"single-channel hit {r.chunk_id} should not outrank dual-channel c.md#one"
        )

    # And the result ordering puts c.md#one first.
    assert results[0].chunk_id == "c.md#one"


# ---- top_k truncation -----------------------------------------------------


def test_search_respects_top_k_argument(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, top_k=10)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = [
        ("a.md#one", 0.1), ("b.md#one", 0.2), ("c.md#one", 0.3)
    ]
    s = HybridSearcher(cfg)
    results = s.search("memory embedding", top_k=2)
    assert len(results) == 2


def test_search_uses_config_top_k_when_omitted(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, top_k=2)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = [
        ("a.md#one", 0.1), ("b.md#one", 0.2), ("c.md#one", 0.3)
    ]
    s = HybridSearcher(cfg)
    results = s.search("memory embedding")
    assert len(results) == 2


# ---- fetch_multiplier influences pool ------------------------------------


def test_search_fetch_multiplier_drives_vector_pool_size(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """``n_results`` passed to Chroma == top_k * fetch_multiplier."""
    cfg = make_config(tmp_path, top_k=4, fetch_multiplier=7)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = [("a.md#one", 0.1)]

    s = HybridSearcher(cfg)
    s.search("memory")

    assert fake_chroma_and_st.query_calls, "vector channel should have been queried"
    assert fake_chroma_and_st.query_calls[0]["n_results"] == 4 * 7


def test_search_fetch_multiplier_drives_bm25_pool_size(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A larger fetch_multiplier surfaces more BM25 candidates into the fusion.

    Pool size = top_k * fetch_multiplier. We hold top_k=2 constant on BOTH
    the config side and the .search() override, then vary fetch_multiplier.
    With multiplier=1 we get 2 BM25 candidates; with 10 we get 20. After
    the final top_k=2 truncation, both runs return ≤ 2 results — but we
    can probe pool sizing through the BM25 raw pool indirectly.

    Concrete check: when ``top_k`` (the cap) is the same but multiplier
    changes, the same final result count is acceptable; what we really
    need to verify is that more candidates entered the fusion. We do that
    by inspecting the BM25 channel directly.
    """
    chunks = [
        make_chunk(f"x.md#{i:02d}", f"memory cache lifecycle distinguishing-{i}")
        for i in range(15)
    ]
    write_chunks_file((tmp_path / "chunks.jsonl"), chunks)
    fake_chroma_and_st.vector_results = []  # isolate BM25 channel

    cfg_small = make_config(tmp_path, top_k=2, fetch_multiplier=1)
    s_small = HybridSearcher(cfg_small)
    s_small._ensure_loaded()
    pool_small = s_small._bm25_candidates(
        "memory", cfg_small.search.top_k * cfg_small.search.fetch_multiplier
    )

    cfg_big = make_config(tmp_path, top_k=2, fetch_multiplier=10)
    s_big = HybridSearcher(cfg_big)
    s_big._ensure_loaded()
    pool_big = s_big._bm25_candidates(
        "memory", cfg_big.search.top_k * cfg_big.search.fetch_multiplier
    )

    assert len(pool_small) == 2, len(pool_small)
    assert len(pool_big) == 15, len(pool_big)  # corpus has 15 docs, all match
    assert len(pool_big) > len(pool_small)


# ---- Lazy loading happens once -------------------------------------------


def test_searcher_loads_chunks_only_once(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = [("a.md#one", 0.1)]

    s = HybridSearcher(cfg)
    assert s._load_calls == 0  # constructor must not load anything

    s.search("memory")
    assert s._load_calls == 1

    s.search("embedding")
    s.search("vault")
    assert s._load_calls == 1, "subsequent searches must reuse loaded state"


def test_searcher_constructor_is_cheap(tmp_path: Path) -> None:
    """No side effects on construction — chunks.jsonl can even be missing.

    We don't load Chroma either; both happen at first .search().
    """
    cfg = make_config(tmp_path)
    # chunks.jsonl deliberately absent
    s = HybridSearcher(cfg)
    assert s._loaded is False
    assert s._chunks is None


# ---- Empty / nonsensical queries ----------------------------------------


def test_empty_query_returns_empty_list(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    s = HybridSearcher(cfg)
    assert s.search("") == []
    assert s.search("   ") == []


def test_punctuation_only_query_returns_empty_list(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    # Punctuation strips to nothing in BM25 normalize. Vector channel
    # *would* embed it, but we returned no candidates from the fake. So
    # the union is empty.
    assert s.search("!!!???") == []


def test_no_match_query_returns_empty_list(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A real query with no matches in either channel returns []."""
    cfg = make_config(tmp_path)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    # "xenophonic" doesn't appear in any chunk.
    assert s.search("xenophonic") == []


def test_searcher_works_without_chromadb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If chromadb is unavailable the searcher degrades to BM25-only."""
    cfg = make_config(tmp_path)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)

    # Force the chromadb import inside _open_collection to fail.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "chromadb":
            raise ImportError("simulated missing chromadb")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    s = HybridSearcher(cfg)
    results = s.search("memory")
    # BM25 channel still works — at least one chunk matches.
    assert results
    # All hits are BM25-only.
    for r in results:
        assert r.vector_score is None


# ---- Determinism ---------------------------------------------------------


def test_search_is_deterministic_across_calls(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """Same inputs → byte-identical result list across repeated calls."""
    cfg = make_config(tmp_path, top_k=5)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = [
        ("a.md#one", 0.1), ("c.md#one", 0.2), ("b.md#one", 0.3)
    ]
    s = HybridSearcher(cfg)
    r1 = s.search("memory embedding")
    r2 = s.search("memory embedding")
    r3 = s.search("memory embedding")
    assert [r.chunk_id for r in r1] == [r.chunk_id for r in r2] == [r.chunk_id for r in r3]
    assert [r.rrf_score for r in r1] == [r.rrf_score for r in r2]


def test_search_is_deterministic_across_instances(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A fresh HybridSearcher on the same data + query yields the same order."""
    cfg = make_config(tmp_path, top_k=5)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = [
        ("a.md#one", 0.1), ("c.md#one", 0.2), ("b.md#one", 0.3)
    ]
    r1 = HybridSearcher(cfg).search("memory embedding")
    fake_chroma_and_st.vector_results = [
        ("a.md#one", 0.1), ("c.md#one", 0.2), ("b.md#one", 0.3)
    ]
    r2 = HybridSearcher(cfg).search("memory embedding")
    assert [r.chunk_id for r in r1] == [r.chunk_id for r in r2]


# ---- SearchResult shape sanity -------------------------------------------


def test_search_result_carries_hydrated_chunk(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory", apply_boost=False)
    assert results
    for r in results:
        assert isinstance(r, SearchResult)
        assert r.chunk["id"] == r.chunk_id
        assert "text" in r.chunk
        assert "heading_path" in r.chunk
        # With apply_boost=False the final_score equals the raw RRF score.
        assert r.final_score == r.rrf_score
