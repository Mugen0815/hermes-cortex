"""Tests for cortex.embedder.

Heavy dependencies (sentence-transformers, chromadb) are mocked so this
runs fast without GPUs/internet. We test the orchestration logic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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
from cortex.embedder import (
    ModelMismatchError,
    chunk_metadata_for_chroma,
    chunk_text_for_embedding,
    detect_device,
    embed_chunks,
    existing_ids_with_hash,
    index_hash_for_chunk,
    load_chunks,
)


# ---- Fixtures --------------------------------------------------------------


def make_config(tmp_path: Path, device: str = "cpu", model: str = "test-model") -> Config:
    return Config(
        vault=VaultConfig(path=tmp_path / "vault"),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(model=model, device=device),
        search=SearchConfig(),
        context_builder=ContextBuilderConfig(),
    )


def write_chunks_file(path: Path, chunks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c) + "\n")


SAMPLE_CHUNKS = [
    {
        "id": "10_facts/A.md#intro",
        "file": "10_facts/A.md",
        "folder": "10_facts",
        "heading": None,
        "heading_path": [],
        "text": "Intro text",
        "tags": ["memory"],
        "wikilinks": ["B"],
        "frontmatter": {"type": "fact"},
        "fm_normalized": {
            "type": "fact",
            "status": "active",
            "domain": "",
            "project": "",
            "stability": "stable",
            "tags": ["memory"],
            "confidence": 0.85,
            "importance": 5.0,
            "last_verified": "2026-04-27",
            "created": "",
            "related": [],
        },
        "modified": "2026-04-27T10:00:00",
        "modified_date": "2026-04-27",
        "content_hash": "hash-A",
        "char_len": 10,
        "token_estimate": 3,
    },
    {
        "id": "10_facts/A.md#details",
        "file": "10_facts/A.md",
        "folder": "10_facts",
        "heading": "Details",
        "heading_path": ["Details"],
        "text": "Detail body",
        "tags": ["memory"],
        "wikilinks": [],
        "frontmatter": {"type": "fact"},
        "fm_normalized": {
            "type": "fact",
            "status": "active",
            "domain": "",
            "project": "",
            "stability": "stable",
            "tags": ["memory"],
            "confidence": 0.85,
            "importance": 5.0,
            "last_verified": "2026-04-27",
            "created": "",
            "related": [],
        },
        "modified": "2026-04-27T10:00:00",
        "modified_date": "2026-04-27",
        "content_hash": "hash-A",
        "char_len": 11,
        "token_estimate": 3,
    },
]


# ---- detect_device --------------------------------------------------------


def test_detect_device_cpu_always() -> None:
    assert detect_device("cpu") == "cpu"


def test_detect_device_no_torch_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "torch":
            raise ImportError("no torch")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert detect_device("auto") == "cpu"
    assert detect_device("cuda") == "cpu"


def test_detect_device_auto_prefers_cuda() -> None:
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = True
    fake_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": fake_torch}):
        assert detect_device("auto") == "cuda"


def test_detect_device_auto_uses_mps_when_no_cuda() -> None:
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    fake_torch.backends.mps.is_available.return_value = True
    with patch.dict("sys.modules", {"torch": fake_torch}):
        assert detect_device("auto") == "mps"


def test_detect_device_auto_falls_back_to_cpu() -> None:
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    fake_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": fake_torch}):
        assert detect_device("auto") == "cpu"


def test_detect_device_explicit_cuda_unavailable_falls_back() -> None:
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False
    fake_torch.backends.mps.is_available.return_value = False
    with patch.dict("sys.modules", {"torch": fake_torch}):
        assert detect_device("cuda") == "cpu"


def test_detect_device_unknown_falls_back() -> None:
    assert detect_device("xpu") == "cpu"


# ---- chunk text + metadata --------------------------------------------------


def test_chunk_text_for_embedding_includes_heading_path() -> None:
    text = chunk_text_for_embedding(
        {"heading_path": ["Memory Model", "Lifecycle"], "text": "body here"}
    )
    assert text.startswith("Memory Model / Lifecycle")
    assert "body here" in text


def test_chunk_text_for_embedding_falls_back_to_heading() -> None:
    """Older chunks with only `heading` (no `heading_path`) still work."""
    text = chunk_text_for_embedding({"heading": "Alpha", "text": "body"})
    assert text == "Alpha\n\nbody"


def test_chunk_text_for_embedding_no_heading() -> None:
    text = chunk_text_for_embedding({"heading": None, "text": "intro"})
    assert text == "intro"


def test_chunk_metadata_numeric_fields_stay_numeric() -> None:
    md = chunk_metadata_for_chroma(SAMPLE_CHUNKS[0])
    assert isinstance(md["confidence"], float)
    assert isinstance(md["importance"], float)
    assert md["confidence"] == pytest.approx(0.85)
    assert md["importance"] == pytest.approx(5.0)


def test_chunk_metadata_flat_lists() -> None:
    """Tags and wikilinks become |sentinel|delimited| strings for Chroma."""
    md = chunk_metadata_for_chroma(SAMPLE_CHUNKS[0])
    assert md["tags_flat"] == "|memory|"
    assert md["wikilinks_flat"] == "|B|"


def test_chunk_metadata_flat_lists_multiple() -> None:
    """tags_flat / wikilinks_flat are debug/transport fields — NOT for Chroma filtering.

    The canonical membership check (has tag X? linked to Y?) runs in the
    Search layer against the ``tags``/``wikilinks`` arrays from chunks.jsonl.
    These flat strings exist only for human readability in Chroma Browse.

    We still assert their shape so the format is at least consistent for
    anyone who inspects the Chroma store manually.
    """
    chunk = dict(SAMPLE_CHUNKS[0])
    chunk["tags"] = ["foo", "bar", "baz"]
    chunk["wikilinks"] = ["Note A", "Note B"]
    md = chunk_metadata_for_chroma(chunk)
    # Shape contract: |value1|value2| (pipe-delimited sentinels)
    assert md["tags_flat"].startswith("|") and md["tags_flat"].endswith("|")
    assert md["wikilinks_flat"].startswith("|") and md["wikilinks_flat"].endswith("|")
    # All values present somewhere in the string (for manual inspection only)
    assert "foo" in md["tags_flat"]
    assert "Note A" in md["wikilinks_flat"]
    # Explicit statement of the contract: filtering against these is NOT supported
    # by Phase 3. Tag/wikilink membership is resolved via chunks.jsonl arrays.


def test_chunk_metadata_drops_empty_string_keys() -> None:
    chunk = {
        "id": "x",
        "file": "f",
        "folder": "",
        "frontmatter": {},
        "fm_normalized": {},
        "tags": [],
        "wikilinks": [],
        "text": "x",
    }
    md = chunk_metadata_for_chroma(chunk)
    assert "type" not in md       # empty string dropped
    assert "tags_flat" not in md  # empty list → empty string → dropped
    # numeric fields KEPT (they have defaults)
    assert "confidence" in md
    assert "importance" in md


def test_chunk_metadata_includes_heading_path_string() -> None:
    chunk = dict(SAMPLE_CHUNKS[1])
    chunk["heading_path"] = ["Top", "Sub"]
    md = chunk_metadata_for_chroma(chunk)
    assert md["heading_path"] == "Top / Sub"


def test_chunk_metadata_length_stats() -> None:
    md = chunk_metadata_for_chroma(SAMPLE_CHUNKS[0])
    assert md["char_len"] == 10
    assert md["token_estimate"] == 3


# ---- load_chunks ---------------------------------------------------------


def test_load_chunks_handles_missing_file(tmp_path: Path) -> None:
    assert load_chunks(tmp_path / "nope.jsonl") == []


def test_load_chunks_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text('{"id":"a"}\nNOT JSON\n{"id":"b"}\n')
    out = load_chunks(p)
    assert [c["id"] for c in out] == ["a", "b"]


# ---- embed_chunks orchestration ------------------------------------------


class FakeCollection:
    def __init__(self, dim: int = 4):
        self.store: dict[str, dict] = {}
        self.metadata: dict[str, Any] = {"hnsw:space": "cosine"}
        self._dim = dim

    def get(self, include=None):
        ids = list(self.store.keys())
        metas = [self.store[i]["meta"] for i in ids]
        return {"ids": ids, "metadatas": metas}

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, e, d, m in zip(ids, embeddings, documents, metadatas):
            self.store[i] = {"emb": e, "doc": d, "meta": m}

    def delete(self, ids):
        for i in ids:
            self.store.pop(i, None)

    def modify(self, metadata=None, **kw):
        if metadata is not None:
            self.metadata = dict(metadata)


@pytest.fixture
def patched_chroma_and_st(monkeypatch: pytest.MonkeyPatch):
    """Patch chromadb and sentence_transformers with in-memory fakes."""
    fake_collection = FakeCollection(dim=4)

    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = fake_collection

    fake_st = MagicMock()
    fake_model = MagicMock()
    import numpy as np

    fake_model.encode.side_effect = lambda texts, **kw: np.array(
        [[float(len(t)), 0.0, 0.0, 0.0] for t in texts]
    )
    fake_model.get_sentence_embedding_dimension.return_value = 4
    fake_st.SentenceTransformer.return_value = fake_model

    monkeypatch.setitem(__import__("sys").modules, "chromadb", fake_chromadb)
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)
    return fake_collection


def test_embed_chunks_first_run_embeds_all(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)

    report = embed_chunks(cfg)

    assert report.chunks_total == 2
    assert report.chunks_embedded == 2
    assert report.chunks_skipped_unchanged == 0
    assert report.device == "cpu"
    assert report.embedding_dim == 4
    assert len(patched_chroma_and_st.store) == 2

    # Collection metadata stamped with model + dim
    assert patched_chroma_and_st.metadata["embedding_model"] == "test-model"
    assert patched_chroma_and_st.metadata["embedding_dim"] == 4


def test_embed_chunks_second_run_skips_unchanged(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)

    report = embed_chunks(cfg)
    assert report.chunks_embedded == 0
    assert report.chunks_skipped_unchanged == 2


def test_embed_chunks_force_reembeds_all(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)

    report = embed_chunks(cfg, force=True)
    assert report.chunks_embedded == 2
    assert report.chunks_skipped_unchanged == 0


def test_embed_chunks_changed_hash_re_embeds(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)

    changed = [dict(c) for c in SAMPLE_CHUNKS]
    changed[0]["content_hash"] = "hash-A-NEW"
    write_chunks_file(cfg.index.chunks_path, changed)

    report = embed_chunks(cfg)
    assert report.chunks_embedded == 1
    assert report.chunks_skipped_unchanged == 1


def test_embed_chunks_removes_orphans(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)
    assert len(patched_chroma_and_st.store) == 2

    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS[:1])
    report = embed_chunks(cfg)
    assert report.chunks_removed == 1
    assert len(patched_chroma_and_st.store) == 1


def test_embed_chunks_no_chunks_file(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    report = embed_chunks(cfg)
    assert report.chunks_total == 0
    assert report.chunks_embedded == 0


def test_embed_chunks_refuses_model_mismatch(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    """Switching embedding model must raise ModelMismatchError, not silently corrupt."""
    cfg = make_config(tmp_path, device="cpu", model="model-A")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)
    assert patched_chroma_and_st.metadata["embedding_model"] == "model-A"

    # Switch model
    cfg2 = make_config(tmp_path, device="cpu", model="model-B")
    with pytest.raises(ModelMismatchError, match="model"):
        embed_chunks(cfg2)


def test_embed_chunks_refuses_dim_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Different embedding dim must raise, even if model name matches."""
    fake_collection = FakeCollection(dim=4)
    # Pre-stamp the collection as if it was built with dim=8.
    fake_collection.metadata = {
        "hnsw:space": "cosine",
        "embedding_model": "test-model",
        "embedding_dim": 8,
    }

    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = fake_collection

    fake_st = MagicMock()
    fake_model = MagicMock()
    import numpy as np

    fake_model.encode.side_effect = lambda texts, **kw: np.array(
        [[float(len(t)), 0.0, 0.0, 0.0] for t in texts]
    )
    fake_model.get_sentence_embedding_dimension.return_value = 4  # mismatch!
    fake_st.SentenceTransformer.return_value = fake_model

    monkeypatch.setitem(__import__("sys").modules, "chromadb", fake_chromadb)
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)

    cfg = make_config(tmp_path, device="cpu", model="test-model")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)

    with pytest.raises(ModelMismatchError, match="dim"):
        embed_chunks(cfg)


def test_embed_chunks_reports_length_stats(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    cfg = make_config(tmp_path, device="cpu")
    big = dict(SAMPLE_CHUNKS[0])
    big["text"] = "x" * 5000
    big["char_len"] = 5000
    big["token_estimate"] = 1500  # above TOKEN_WARN_THRESHOLD
    big["id"] = "10_facts/A.md#big"
    write_chunks_file(cfg.index.chunks_path, [big])

    report = embed_chunks(cfg)
    assert report.max_chunk_chars == 5000
    assert report.max_chunk_tokens_est == 1500
    assert report.chunks_over_token_threshold == 1


# ---- index_hash_for_chunk -------------------------------------------------


def test_index_hash_stable_for_identical_chunk() -> None:
    """Same chunk → same hash (deterministic, reproducible across runs)."""
    a = dict(SAMPLE_CHUNKS[0])
    b = dict(SAMPLE_CHUNKS[0])
    assert index_hash_for_chunk(a) == index_hash_for_chunk(b)


def test_index_hash_changes_when_embedding_text_changes() -> None:
    """Edit to chunk text (which feeds chunk_text_for_embedding) → new hash."""
    a = dict(SAMPLE_CHUNKS[0])
    b = dict(SAMPLE_CHUNKS[0])
    b["text"] = b["text"] + " — appended sentence"
    assert index_hash_for_chunk(a) != index_hash_for_chunk(b)


def test_index_hash_changes_when_metadata_changes() -> None:
    """Confidence bump (Chroma metadata only, source text untouched) → new hash.

    This is the core motivation for index_hash: pure-metadata edits would
    silently slip past a content_hash-only check.
    """
    a = dict(SAMPLE_CHUNKS[0])
    b = dict(SAMPLE_CHUNKS[0])
    b["fm_normalized"] = dict(b["fm_normalized"])
    b["fm_normalized"]["confidence"] = 0.5  # was 0.85
    assert index_hash_for_chunk(a) != index_hash_for_chunk(b)


def test_index_hash_changes_when_heading_path_changes() -> None:
    """Renaming a heading shifts what gets embedded → new hash."""
    a = dict(SAMPLE_CHUNKS[1])
    b = dict(SAMPLE_CHUNKS[1])
    b["heading_path"] = ["Renamed"]
    b["heading"] = "Renamed"
    assert index_hash_for_chunk(a) != index_hash_for_chunk(b)


def test_index_hash_not_in_metadata_projection() -> None:
    """index_hash must NOT be a field returned by chunk_metadata_for_chroma —
    that would create a circular hash (hash depends on metadata which
    depends on hash). The caller stamps it on top during upsert."""
    md = chunk_metadata_for_chroma(SAMPLE_CHUNKS[0])
    assert "index_hash" not in md


# ---- Incremental skip via index_hash --------------------------------------


def test_embed_chunks_metadata_change_re_embeds(
    tmp_path: Path, patched_chroma_and_st: FakeCollection
) -> None:
    """Pure-metadata edit (same content_hash) MUST trigger re-embed."""
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)

    # Bump confidence; keep content_hash unchanged.
    edited = [dict(c) for c in SAMPLE_CHUNKS]
    edited[0]["fm_normalized"] = dict(edited[0]["fm_normalized"])
    edited[0]["fm_normalized"]["confidence"] = 0.10  # was 0.85
    assert edited[0]["content_hash"] == "hash-A"  # source unchanged
    write_chunks_file(cfg.index.chunks_path, edited)

    report = embed_chunks(cfg)
    assert report.chunks_embedded == 1
    assert report.chunks_skipped_unchanged == 1
    # And the new metadata is what landed in Chroma:
    stored = patched_chroma_and_st.store["10_facts/A.md#intro"]["meta"]
    assert stored["confidence"] == pytest.approx(0.10)


def test_embed_chunks_stamps_index_hash_in_metadata(
    tmp_path: Path, patched_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)
    for cid, entry in patched_chroma_and_st.store.items():
        assert "index_hash" in entry["meta"], f"{cid} missing index_hash"
        assert len(entry["meta"]["index_hash"]) == 64  # sha256 hex


# ---- force=True still cleans orphans --------------------------------------


def test_embed_chunks_force_still_removes_orphans(
    tmp_path: Path, patched_chroma_and_st: FakeCollection
) -> None:
    """force=True must NOT skip orphan cleanup."""
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)
    assert len(patched_chroma_and_st.store) == 2

    # Drop the second chunk and force-re-embed.
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS[:1])
    report = embed_chunks(cfg, force=True)

    assert report.chunks_embedded == 1
    assert report.chunks_skipped_unchanged == 0
    assert report.chunks_removed == 1
    assert "10_facts/A.md#details" not in patched_chroma_and_st.store


# ---- Orphans without any hash --------------------------------------------


def test_existing_ids_with_hash_returns_all_ids(
    patched_chroma_and_st: FakeCollection,
) -> None:
    """Even entries with no recorded hash show up in all_ids (orphan-eligible)."""
    coll = patched_chroma_and_st
    coll.store["legacy-no-hash"] = {"emb": [0, 0, 0, 0], "doc": "x", "meta": {}}
    coll.store["legacy-content-only"] = {
        "emb": [0, 0, 0, 0], "doc": "x",
        "meta": {"content_hash": "abc"},
    }
    coll.store["modern"] = {
        "emb": [0, 0, 0, 0], "doc": "x",
        "meta": {"index_hash": "deadbeef"},
    }

    all_ids, id_to_hash = existing_ids_with_hash(coll)
    assert all_ids == {"legacy-no-hash", "legacy-content-only", "modern"}
    # Hash mapping prefers index_hash, falls back to content_hash, skips empty.
    assert id_to_hash == {"legacy-content-only": "abc", "modern": "deadbeef"}


def test_embed_chunks_removes_hashless_orphan(
    tmp_path: Path, patched_chroma_and_st: FakeCollection
) -> None:
    """A stale Chroma entry without index_hash/content_hash must still be cleaned."""
    cfg = make_config(tmp_path, device="cpu")
    # Pre-seed Chroma with a hashless legacy entry that's NOT in chunks.jsonl.
    patched_chroma_and_st.store["10_facts/legacy.md#stale"] = {
        "emb": [0.0, 0.0, 0.0, 0.0],
        "doc": "stale",
        "meta": {},
    }
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)

    report = embed_chunks(cfg)

    assert report.chunks_removed == 1
    assert "10_facts/legacy.md#stale" not in patched_chroma_and_st.store


# ---- Duplicate IDs in chunks.jsonl ---------------------------------------


def test_embed_chunks_duplicate_id_reported_not_overwritten(
    tmp_path: Path, patched_chroma_and_st: FakeCollection
) -> None:
    """Two chunks sharing an ID must produce a report error, not silent overwrite."""
    cfg = make_config(tmp_path, device="cpu")
    a = dict(SAMPLE_CHUNKS[0])
    b = dict(SAMPLE_CHUNKS[0])  # same id!
    b["text"] = "DIFFERENT BODY"
    b["content_hash"] = "different-hash"
    write_chunks_file(cfg.index.chunks_path, [a, b])

    report = embed_chunks(cfg)

    # First chunk embedded, second flagged as duplicate.
    assert report.chunks_embedded == 1
    dup_errors = [e for e in report.errors if "duplicate" in e[1]]
    assert len(dup_errors) == 1
    assert dup_errors[0][0] == a["id"]
    # The store contains the FIRST chunk's body, not silently the second's.
    assert patched_chroma_and_st.store[a["id"]]["doc"] != "DIFFERENT BODY"


def test_embed_chunks_missing_id_reported(
    tmp_path: Path, patched_chroma_and_st: FakeCollection
) -> None:
    cfg = make_config(tmp_path, device="cpu")
    bad = dict(SAMPLE_CHUNKS[0])
    bad["id"] = ""
    good = dict(SAMPLE_CHUNKS[1])
    write_chunks_file(cfg.index.chunks_path, [bad, good])

    report = embed_chunks(cfg)

    assert report.chunks_embedded == 1
    missing_errors = [e for e in report.errors if e[0] == "<missing id>"]
    assert len(missing_errors) == 1


# ---- P8 sidecar/reuse/warning hygiene ------------------------------------


def test_embed_chunks_writes_sidecar_manifest(tmp_path: Path, patched_chroma_and_st: FakeCollection) -> None:
    from cortex.embedder import embedding_manifest_path

    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)

    report = embed_chunks(cfg)
    manifest = json.loads(embedding_manifest_path(cfg).read_text())

    assert report.manifest_path == str(embedding_manifest_path(cfg))
    assert manifest["embedding_model"] == "test-model"
    assert manifest["embedding_dim"] == 4
    assert manifest["chunk_count"] == 2


def test_embed_chunks_unchanged_uses_manifest_without_model_load(
    tmp_path: Path,
    patched_chroma_and_st: FakeCollection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = make_config(tmp_path, device="cpu")
    write_chunks_file(cfg.index.chunks_path, SAMPLE_CHUNKS)
    embed_chunks(cfg)

    fake_st = __import__("sys").modules["sentence_transformers"]
    fake_st.SentenceTransformer.reset_mock()

    report = embed_chunks(cfg)

    assert report.chunks_embedded == 0
    assert report.chunks_skipped_unchanged == 2
    assert report.embedding_dim == 4
    assert "trusted sidecar manifest" in report.model_load_skipped_reason
    fake_st.SentenceTransformer.assert_not_called()


def test_embedder_prefers_supported_embedding_dimension_api(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 4
    fake_model.get_sentence_embedding_dimension.side_effect = AssertionError("deprecated API used")
    fake_model.encode.return_value = np.array([[1.0, 0.0, 0.0, 0.0]])
    fake_st = MagicMock()
    fake_st.SentenceTransformer.return_value = fake_model
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)

    from cortex.embedder import Embedder

    assert Embedder("model", "cpu").dim == 4


def test_same_process_embedder_reuses_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np
    import cortex.embedder as embedder_mod

    embedder_mod._MODEL_CACHE.clear()
    fake_model = MagicMock()
    fake_model.get_embedding_dimension.return_value = 4
    fake_model.encode.return_value = np.array([[1.0, 0.0, 0.0, 0.0]])
    fake_st = MagicMock()
    fake_st.SentenceTransformer.return_value = fake_model
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)

    first = embedder_mod.Embedder("model", "cpu")
    second = embedder_mod.Embedder("model", "cpu")

    assert first.dim == 4
    assert second.dim == 4
    assert first.reused is False
    assert second.reused is True
    assert fake_st.SentenceTransformer.call_count == 1


def test_hf_warning_filter_dedupes_child_logger_token_warning(caplog: pytest.LogCaptureFixture) -> None:
    import cortex.embedder as embedder_mod

    embedder_mod._HF_WARNING_FILTER_INSTALLED = False
    embedder_mod._HF_WARNING_SEEN.clear()
    logger = logging.getLogger("huggingface_hub.file_download")
    warning = "HF_TOKEN is not set; unauthenticated Hugging Face Hub requests may be rate limited"
    unrelated = "Cache directory is read-only; using temporary download location"

    with caplog.at_level("WARNING", logger="huggingface_hub"):
        embedder_mod._install_hf_warning_filter()
        embedder_mod._install_hf_warning_filter()
        assert sum(isinstance(f, embedder_mod._OnceHFWarningFilter) for f in logger.filters) == 1
        logger.warning(warning)
        logger.warning(warning)
        logger.warning(unrelated)

    messages = [r.message for r in caplog.records]
    assert messages.count(warning) == 1
    assert messages.count(unrelated) == 1
