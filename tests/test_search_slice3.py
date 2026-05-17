"""Integration tests for HybridSearcher Slice-3 features:
graph channel as 3rd RRF channel, wikilink_traversal, graph_weight knob.
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
from cortex.filters import SearchFilters
from cortex.search import HybridSearcher


def make_config(tmp_path: Path, **search_overrides) -> Config:
    sc_kwargs = dict(
        top_k=10,
        bm25_weight=0.5,
        vector_weight=0.5,
        rrf_k=60,
        fetch_multiplier=5,
        wikilink_traversal=1,
        graph_weight=0.2,
        recency_boost=False,
        importance_boost=False,
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
    file: str,
    *,
    type_: str = "fact",
    wikilinks: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": cid,
        "file": file,
        "folder": file.split("/", 1)[0] if "/" in file else "",
        "heading": None,
        "heading_path": [],
        "text": text,
        "tags": [],
        "wikilinks": wikilinks or [],
        "frontmatter": {"type": type_, "importance": 3},
        "fm_normalized": {
            "type": type_, "status": "active", "importance": 3.0,
            "confidence": 0.5, "last_verified": "",
        },
        "modified": "2026-04-15T00:00:00",
        "modified_date": "2026-04-15",
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
        self.query_calls.append({"n_results": n_results, "where": where})
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


# ---- Graph surfaces linked chunks not in BM25 -----------------------------


def test_graph_channel_pulls_in_linked_chunk(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """A chunk that doesn't match BM25 but is linked from a hit should
    surface via the graph channel.
    """
    cfg = make_config(tmp_path)
    chunks = [
        make_chunk("a.md#0", "memory hybrid retrieval", "10_facts/A.md",
                   wikilinks=["B"]),
        make_chunk("b.md#0", "completely unrelated text about plants",
                   "10_facts/B.md"),
    ]
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []  # vector empty
    s = HybridSearcher(cfg)
    results = s.search("memory hybrid")
    ids = [r.chunk_id for r in results]
    assert "a.md#0" in ids
    # b only enters via the graph channel.
    assert "b.md#0" in ids
    b_result = next(r for r in results if r.chunk_id == "b.md#0")
    assert b_result.bm25_rank is None
    assert b_result.vector_rank is None
    assert b_result.graph_rank == 1


def test_graph_disabled_when_weight_zero(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, graph_weight=0.0)
    chunks = [
        make_chunk("a.md#0", "memory", "10_facts/A.md", wikilinks=["B"]),
        make_chunk("b.md#0", "plants", "10_facts/B.md"),
    ]
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory")
    ids = [r.chunk_id for r in results]
    assert "b.md#0" not in ids


def test_graph_disabled_when_traversal_zero(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, wikilink_traversal=0)
    chunks = [
        make_chunk("a.md#0", "memory", "10_facts/A.md", wikilinks=["B"]),
        make_chunk("b.md#0", "plants", "10_facts/B.md"),
    ]
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory")
    ids = [r.chunk_id for r in results]
    assert "b.md#0" not in ids


# ---- Graph respects filters -----------------------------------------------


def test_graph_respects_type_filter(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    """Linked chunk of wrong type must not surface even via graph channel."""
    cfg = make_config(tmp_path)
    chunks = [
        make_chunk("a.md#0", "memory hybrid", "10_facts/A.md",
                   type_="fact", wikilinks=["B"]),
        make_chunk("b.md#0", "linked but wrong type", "10_facts/B.md",
                   type_="note"),
    ]
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory hybrid", filters=SearchFilters(type=["fact"]))
    ids = [r.chunk_id for r in results]
    assert "a.md#0" in ids
    assert "b.md#0" not in ids, "graph channel must respect filters"


# ---- Graph rank in SearchResult debug --------------------------------------


def test_search_result_carries_graph_rank(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path)
    chunks = [
        make_chunk("a.md#0", "memory", "10_facts/A.md", wikilinks=["B"]),
        make_chunk("b.md#0", "unrelated", "10_facts/B.md"),
    ]
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory")
    by_id = {r.chunk_id: r for r in results}
    # a is a BM25 hit, not via graph.
    assert by_id["a.md#0"].graph_rank is None
    # b is graph-only.
    assert by_id["b.md#0"].graph_rank == 1


# ---- Multi-hop -------------------------------------------------------------


def test_multi_hop_traversal(
    tmp_path: Path, fake_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, wikilink_traversal=2)
    chunks = [
        make_chunk("a.md#0", "memory", "10_facts/A.md", wikilinks=["B"]),
        make_chunk("b.md#0", "unrelated", "10_facts/B.md", wikilinks=["C"]),
        make_chunk("c.md#0", "two hops away", "10_facts/C.md"),
    ]
    write_chunks(cfg.index.chunks_path, chunks)
    fake_chroma_and_st.vector_results = []
    s = HybridSearcher(cfg)
    results = s.search("memory")
    ids = [r.chunk_id for r in results]
    assert "c.md#0" in ids
