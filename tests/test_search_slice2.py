"""Integration tests for HybridSearcher Slice-2 features:
filter-aware BM25, Chroma where-builder propagation, and combined boosts
across the full search() pipeline.
"""

from __future__ import annotations

import json
from datetime import date
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
from cortex.filters import SearchFilters
from cortex.search import HybridSearcher


# ---- Fixtures (shape-compatible with tests/test_search.py) -----------------


def make_config(tmp_path: Path, **search_overrides) -> Config:
    sc_kwargs = dict(
        top_k=5,
        bm25_weight=0.5,
        vector_weight=0.5,
        rrf_k=60,
        fetch_multiplier=5,
    )
    sc_kwargs.update(search_overrides)
    return Config(
        vault=VaultConfig(path=tmp_path / "vault"),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(model="test-model", device="cpu"),
        search=SearchConfig(**sc_kwargs),
        context_builder=ContextBuilderConfig(),
    )


def make_chunk(
    cid: str,
    text: str,
    *,
    type_: str = "fact",
    folder: str = "10_facts",
    importance: int | None = 3,
    modified_date: str = "2026-04-15",
    last_verified: str = "",
    tags: list[str] | None = None,
    wikilinks: list[str] | None = None,
    heading_path: list[str] | None = None,
) -> dict[str, Any]:
    raw_fm: dict[str, Any] = {"type": type_}
    if importance is not None:
        raw_fm["importance"] = importance
    fm_norm = dict(raw_fm)
    fm_norm.setdefault("status", "active")
    fm_norm.setdefault("confidence", 0.5)
    fm_norm["last_verified"] = last_verified
    if importance is not None:
        fm_norm["importance"] = float(importance)
    return {
        "id": cid,
        "file": cid.split("#", 1)[0],
        "folder": folder,
        "heading": heading_path[-1] if heading_path else None,
        "heading_path": heading_path or [],
        "text": text,
        "tags": tags or [],
        "wikilinks": wikilinks or [],
        "frontmatter": raw_fm,
        "fm_normalized": fm_norm,
        "modified": f"{modified_date}T00:00:00",
        "modified_date": modified_date,
        "content_hash": f"hash-{cid}",
        "char_len": len(text),
        "token_estimate": max(1, len(text) // 4),
    }


def write_chunks(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c) + "\n")


class FakeCollection:
    def __init__(self) -> None:
        self.vector_results: list[tuple[str, float]] = []
        self.query_calls: list[dict[str, Any]] = []

    def query(self, query_embeddings, n_results, include=None, where=None, **kw):
        self.query_calls.append(
            {"n_results": n_results, "include": include, "where": where}
        )
        # Honor the where filter trivially: tests can rig vector_results to
        # already match what they expect Chroma to return.
        ids = [cid for cid, _ in self.vector_results[:n_results]]
        dists = [d for _, d in self.vector_results[:n_results]]
        return {"ids": [ids], "distances": [dists]}


@pytest.fixture
def fake_chroma_and_st(monkeypatch):
    fake_collection = FakeCollection()
    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )
    fake_st = MagicMock()
    fake_model = MagicMock()
    import numpy as np
    fake_model.encode.side_effect = lambda texts, **kw: np.array(
        [[0.1, 0.2, 0.3, 0.4]] * len(texts)
    )
    fake_model.get_sentence_embedding_dimension.return_value = 4
    fake_st.SentenceTransformer.return_value = fake_model
    monkeypatch.setitem(__import__("sys").modules, "chromadb", fake_chromadb)
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)
    return fake_collection


# ---- BM25 filter-then-truncate guarantee -----------------------------------


def test_bm25_finds_filtered_hit_buried_below_pool_size(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """The critical property: a filter-matching hit ranked outside the
    *unfiltered* top-N pool must still surface when filters are applied.

    We build a corpus where many irrelevant 'memory' chunks rank above the
    one fact-typed chunk we care about. With a tiny pool_size the
    fact-typed chunk would never make the unfiltered top-N — but with the
    filter applied first, it does.
    """
    cfg = make_config(tmp_path, top_k=1, fetch_multiplier=1)  # pool_size = 1
    chunks: list[dict[str, Any]] = []
    # 10 strong-matching note chunks: full query overlap, balanced length.
    for i in range(10):
        chunks.append(
            make_chunk(
                f"note{i}.md#0",
                "memory cache lifecycle " + " ".join(f"w{i}{j}" for j in range(20)),
                type_="note",
            )
        )
    # 1 weakly-matching fact chunk: shares only one query token, lots of noise.
    chunks.append(
        make_chunk(
            "fact.md#0",
            "lifecycle " + " ".join(f"x{j}" for j in range(40)),
            type_="fact",
        )
    )
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []  # vector empty → BM25 only

    s = HybridSearcher(cfg)

    # Without filter, fact.md is buried — top-1 is a note chunk.
    unfiltered = s.search("memory cache lifecycle", apply_boost=False)
    assert unfiltered, "expected at least one unfiltered hit"
    assert unfiltered[0].chunk["fm_normalized"]["type"] == "note", \
        f"unfiltered top-1 should be a note, got {unfiltered[0].chunk_id}"

    # With filter type=fact, fact.md MUST surface despite being weakly-ranked.
    filtered = s.search(
        "memory cache lifecycle",
        filters=SearchFilters(type=["fact"]),
        apply_boost=False,
    )
    assert filtered, "filter-matching chunk should surface"
    assert filtered[0].chunk_id == "fact.md#0"


# ---- Chroma where propagation ----------------------------------------------


def test_chroma_query_receives_where_when_filter_set(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path, [
        make_chunk("a.md#0", "memory", type_="fact"),
    ])
    fake_chroma_and_st.vector_results = [("a.md#0", 0.1)]
    s = HybridSearcher(cfg)
    s.search("memory", filters=SearchFilters(type=["fact"]))
    # Last query call should carry the where dict.
    assert fake_chroma_and_st.query_calls
    call = fake_chroma_and_st.query_calls[-1]
    assert call["where"] == {"type": "fact"}


def test_chroma_query_no_where_when_no_filter(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path, [make_chunk("a.md#0", "memory")])
    fake_chroma_and_st.vector_results = [("a.md#0", 0.1)]
    s = HybridSearcher(cfg)
    s.search("memory")
    assert fake_chroma_and_st.query_calls[-1]["where"] is None


# ---- Tag/wikilink post-fetch on vector channel -----------------------------


def test_membership_filter_drops_vector_hit_without_tag(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """Vector channel returns 2 hits; only one has the required tag.
    The tag filter must drop the other post-fetch (Chroma can't express it).
    """
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path, [
        make_chunk("a.md#0", "alpha", tags=["jarvis"]),
        make_chunk("b.md#0", "beta", tags=["other"]),
    ])
    fake_chroma_and_st.vector_results = [("a.md#0", 0.1), ("b.md#0", 0.2)]
    s = HybridSearcher(cfg)
    results = s.search(
        "alpha",
        filters=SearchFilters(tags_any=["jarvis"]),
        apply_boost=False,
    )
    ids = [r.chunk_id for r in results]
    assert "a.md#0" in ids
    assert "b.md#0" not in ids


# ---- Boost integration -----------------------------------------------------


def test_boost_changes_ranking_via_recency(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """Two equally-scoring chunks; the more recent one wins after boost."""
    cfg = make_config(
        tmp_path,
        top_k=2,
        recency_max_boost=0.5,
        recency_half_life_days=30,
        importance_boost=False,
    )
    write_chunks(cfg.index.chunks_path, [
        make_chunk("old.md#0", "memory", modified_date="2025-04-15"),
        make_chunk("new.md#0", "memory", modified_date="2026-04-15"),
    ])
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory", now=date(2026, 4, 15))
    assert [r.chunk_id for r in results] == ["new.md#0", "old.md#0"]


def test_apply_boost_false_disables_boost_path(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, recency_max_boost=0.5)
    write_chunks(cfg.index.chunks_path, [
        make_chunk("a.md#0", "memory", modified_date="2026-04-15"),
    ])
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    [r] = s.search("memory", apply_boost=False, now=date(2026, 4, 15))
    assert r.final_score == r.rrf_score
    assert r.debug["boost_multiplier"] == 1.0


def test_search_result_debug_carries_boost_factors(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, recency_max_boost=0.2, importance_max_boost=0.3)
    write_chunks(cfg.index.chunks_path, [
        make_chunk("a.md#0", "memory", importance=5, modified_date="2026-04-15"),
    ])
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    [r] = s.search("memory", now=date(2026, 4, 15))
    assert r.debug["recency_factor"] == pytest.approx(0.2)
    assert r.debug["importance_factor"] == pytest.approx(0.3)
    assert r.debug["raw_boost_multiplier"] == pytest.approx(1.2 * 1.3)
    assert r.debug["boost_multiplier"] == pytest.approx(1.2)
    assert r.debug["boost_capped"] is True
    assert r.debug["quality_factor"] == pytest.approx(1.0)


def test_link_only_chunk_penalty_downranks_content_query(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, top_k=2, recency_boost=False, importance_boost=False)
    write_chunks(cfg.index.chunks_path, [
        make_chunk(
            "content.md#0",
            "memory cache lifecycle implementation details and troubleshooting",
            heading_path=["Implementation"],
            wikilinks=[],
        ),
        make_chunk(
            "links.md#0",
            "- [[Memory Cache]]\n- [[Lifecycle]]",
            heading_path=["Related"],
            wikilinks=["Memory Cache", "Lifecycle"],
        ),
    ])
    fake_chroma_and_st.vector_results = [("links.md#0", 0.1), ("content.md#0", 0.2)]
    s = HybridSearcher(cfg)
    results = s.search("memory cache lifecycle")
    assert [r.chunk_id for r in results] == ["content.md#0", "links.md#0"]
    link = {r.chunk_id: r for r in results}["links.md#0"]
    assert link.debug["quality_factor"] == pytest.approx(0.75)
    assert link.debug["quality_reason"] == "link_heading"


def test_explicit_link_query_keeps_link_chunk_discoverable(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, top_k=2, recency_boost=False, importance_boost=False)
    write_chunks(cfg.index.chunks_path, [
        make_chunk(
            "content.md#0",
            "memory cache lifecycle implementation details",
            heading_path=["Implementation"],
        ),
        make_chunk(
            "links.md#0",
            "- [[Memory Cache]]\n- [[Lifecycle]]",
            heading_path=["Related"],
            wikilinks=["Memory Cache", "Lifecycle"],
        ),
    ])
    fake_chroma_and_st.vector_results = [("links.md#0", 0.1), ("content.md#0", 0.2)]
    s = HybridSearcher(cfg)
    results = s.search("related links memory cache lifecycle")
    by_id = {r.chunk_id: r for r in results}
    assert "links.md#0" in by_id
    assert by_id["links.md#0"].debug["quality_factor"] == pytest.approx(1.0)
    assert by_id["links.md#0"].debug["quality_reason"] == "explicit_link_query"


# ---- Filter validation surfaces at search() --------------------------------


def test_search_raises_on_invalid_filters(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path, [make_chunk("a.md#0", "memory")])
    s = HybridSearcher(cfg)
    with pytest.raises(ValueError, match="modified_after"):
        s.search("memory", filters=SearchFilters(modified_after="garbage"))


# ---- Combined: filters + boost together ------------------------------------


def test_filtered_results_still_get_boosted(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(
        tmp_path,
        top_k=2,
        importance_max_boost=0.5,
        recency_boost=False,
    )
    write_chunks(cfg.index.chunks_path, [
        make_chunk("a.md#0", "memory hybrid", type_="fact", importance=1),
        make_chunk("b.md#0", "memory hybrid", type_="fact", importance=5),
        make_chunk("c.md#0", "memory hybrid", type_="note", importance=5),
    ])
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search(
        "memory hybrid",
        filters=SearchFilters(type=["fact"]),
        now=date(2026, 4, 15),
    )
    ids = [r.chunk_id for r in results]
    # c.md (type=note) is filtered out; b.md ranks above a.md via importance.
    assert ids == ["b.md#0", "a.md#0"]
