"""Tests for cortex.plugin — Phase 5 tool API."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import plugin_runtime
from cortex import plugin
from cortex.plugin import (
    CortexToolError,
    reset_cache,
    vault_build_context,
    vault_read_note,
    vault_search,
)


# ---- Fixtures --------------------------------------------------------------


CONFIG_TEMPLATE = """\
vault:
  path: {vault}
index:
  chunks_path: {chunks}
  chroma_path: {chroma}
  collection: test-coll
embeddings:
  model: test-model
  device: cpu
"""


SAMPLE_NOTE = """\
---
type: fact
status: active
tags: [memory, jarvis]
confidence: high
importance: high
stability: stable
---

# Foo

Body about memory.

## Section A

Detail under A. Links to [[Bar]].
"""


SECOND_NOTE = """\
---
type: decision
status: active
tags: [decisions]
confidence: medium
importance: medium
stability: provisional
---

# Bar

A different note.
"""


@pytest.fixture(autouse=True)
def _clear_plugin_cache():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def stubbed_chroma(monkeypatch):
    """Patch chromadb + sentence_transformers so the searcher works
    without a real backend (vault_search/build_context both touch them).
    """
    fake_collection = MagicMock()
    fake_collection.query.return_value = {"ids": [[]], "distances": [[]]}
    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = (
        fake_collection
    )
    monkeypatch.setitem(__import__("sys").modules, "chromadb", fake_chromadb)
    fake_st = MagicMock()
    fake_model = MagicMock()
    import numpy as np
    fake_model.encode.side_effect = lambda texts, **kw: np.array(
        [[0.1, 0.2, 0.3, 0.4]] * len(texts)
    )
    fake_model.get_sentence_embedding_dimension.return_value = 4
    fake_st.SentenceTransformer.return_value = fake_model
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)
    return fake_collection


def _setup_indexed(tmp_path: Path) -> Path:
    """Set up a tmp vault, run cortex.indexer over it, return cfg path."""
    vault = tmp_path / "vault"
    (vault / "10_facts").mkdir(parents=True)
    (vault / "10_facts" / "Foo.md").write_text(SAMPLE_NOTE)
    (vault / "20_decisions").mkdir()
    (vault / "20_decisions" / "Bar.md").write_text(SECOND_NOTE)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(
            vault=vault,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        )
    )
    from cortex.config import load_config
    from cortex.indexer import index_vault
    cfg = load_config(cfg_path)
    index_vault(cfg)
    return cfg_path


# ---- vault_search ----------------------------------------------------------


def test_vault_search_returns_envelope(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_search("memory", config_path=str(cfg_path))
    assert out["query"] == "memory"
    assert isinstance(out["count"], int)
    assert out["count"] >= 1
    first = out["results"][0]
    assert {"chunk_id", "file", "text", "scores", "ranks"} <= set(first)
    assert first["scores"]["final"] is not None


def test_vault_search_rejects_empty_query(tmp_path: Path) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="non-empty"):
        vault_search("", config_path=str(cfg_path))
    with pytest.raises(CortexToolError):
        vault_search("   ", config_path=str(cfg_path))


def test_vault_search_filter_passthrough(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_search(
        "memory",
        filters={"type": ["fact"]},
        config_path=str(cfg_path),
    )
    for r in out["results"]:
        assert r["fm_normalized"]["type"] == "fact"


def test_vault_search_unknown_filter_field_raises(tmp_path: Path) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="Unknown filter field"):
        vault_search(
            "memory",
            filters={"flavor": ["vanilla"]},
            config_path=str(cfg_path),
        )


def test_vault_search_invalid_filter_raises(tmp_path: Path) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="modified_after"):
        vault_search(
            "memory",
            filters={"modified_after": "garbage"},
            config_path=str(cfg_path),
        )


def test_vault_search_result_is_json_serializable(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_search("memory", config_path=str(cfg_path))
    json.dumps(out)  # must not raise


def test_hermes_tool_wrap_serializes_frontmatter_dates() -> None:
    out = plugin_runtime._wrap(lambda: {"frontmatter": {"created": date(2026, 5, 12)}})
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["frontmatter"]["created"] == "2026-05-12"


# ---- vault_read_note -------------------------------------------------------


def test_vault_read_note_returns_body_and_frontmatter(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_read_note("10_facts/Foo.md", config_path=str(cfg_path))
    assert out["exists"] is True
    assert "Body about memory" in out["content"]
    assert out["frontmatter"]["type"] == "fact"
    assert "memory" in out["tags"]
    assert "Bar" in out["wikilinks"]
    assert out["modified_date"]
    assert out["selected_heading_path"] is None


def test_vault_read_note_missing_file_returns_exists_false(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_read_note("10_facts/Nope.md", config_path=str(cfg_path))
    assert out["exists"] is False
    assert out["content"] == ""


def test_vault_read_note_path_outside_vault_raises(tmp_path: Path) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="outside the vault"):
        vault_read_note("/etc/passwd", config_path=str(cfg_path))


def test_vault_read_note_heading_slice(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_read_note(
        "10_facts/Foo.md",
        heading_path=["Section A"],
        config_path=str(cfg_path),
    )
    assert out["selected_heading_path"] == ["Section A"]
    assert "Detail under A" in out["content"]
    # Body of a different section must NOT be in this slice.
    assert "Body about memory" not in out["content"]


def test_vault_read_note_heading_miss_raises(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="No chunk"):
        vault_read_note(
            "10_facts/Foo.md",
            heading_path=["Nonexistent Heading"],
            config_path=str(cfg_path),
        )


def test_vault_read_note_accepts_absolute_path_under_vault(
    tmp_path: Path, stubbed_chroma
) -> None:
    cfg_path = _setup_indexed(tmp_path)
    abs_path = tmp_path / "vault" / "10_facts" / "Foo.md"
    out = vault_read_note(str(abs_path), config_path=str(cfg_path))
    assert out["exists"] is True
    assert out["file"] == "10_facts/Foo.md"  # normalized to relative


def test_vault_read_note_rejects_empty(tmp_path: Path) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="non-empty"):
        vault_read_note("", config_path=str(cfg_path))


# ---- vault_build_context ---------------------------------------------------


def test_vault_build_context_returns_markdown_envelope(
    tmp_path: Path, stubbed_chroma
) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_build_context("memory", config_path=str(cfg_path))
    assert out["query"] == "memory"
    assert "# Context" in out["text"]
    assert isinstance(out["chunks_included"], list)
    assert out["tokens_used"] <= out["tokens_budget"]


def test_vault_build_context_budget_override(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_build_context(
        "memory",
        budget=5,
        config_path=str(cfg_path),
    )
    assert out["tokens_budget"] == 5
    assert out["chunks_included"] == []


def test_vault_build_context_hermes_memory_override(
    tmp_path: Path, stubbed_chroma
) -> None:
    cfg_path = _setup_indexed(tmp_path)
    # MEMORY.md exists but include_hermes_memory defaults to True in the
    # ContextBuilderConfig — let's force it off via the per-call override.
    mem = tmp_path / "MEMORY.md"
    mem.write_text("memory blob\n")
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(
            vault=tmp_path / "vault",
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        )
        + f"hermes_memory:\n  memory_path: {mem}\n"
        + "context_builder:\n  include_hermes_memory: true\n"
    )
    reset_cache()
    out_on = vault_build_context("memory", config_path=str(cfg_path))
    out_off = vault_build_context(
        "memory",
        include_hermes_memory=False,
        config_path=str(cfg_path),
    )
    assert out_on["hermes_memory_included"] is True
    assert out_off["hermes_memory_included"] is False


def test_vault_build_context_rejects_empty_query(tmp_path: Path) -> None:
    cfg_path = _setup_indexed(tmp_path)
    with pytest.raises(CortexToolError, match="non-empty"):
        vault_build_context("", config_path=str(cfg_path))


def test_vault_build_context_passes_filters(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    out = vault_build_context(
        "memory",
        filters={"type": ["nonexistent"]},
        config_path=str(cfg_path),
    )
    # No matches, but the call should still succeed and yield an empty body.
    assert out["chunks_included"] == []


# ---- caching --------------------------------------------------------------


def test_resolve_state_caches_searcher(tmp_path: Path, stubbed_chroma) -> None:
    cfg_path = _setup_indexed(tmp_path)
    cfg1, s1 = plugin._resolve_state(str(cfg_path))
    cfg2, s2 = plugin._resolve_state(str(cfg_path))
    assert s1 is s2  # same instance reused


def test_resolve_state_invalidates_on_chunks_change(
    tmp_path: Path, stubbed_chroma
) -> None:
    cfg_path = _setup_indexed(tmp_path)
    cfg1, s1 = plugin._resolve_state(str(cfg_path))
    # Touch chunks.jsonl to advance mtime.
    chunks = tmp_path / "chunks.jsonl"
    import os
    new_mtime = chunks.stat().st_mtime + 5
    os.utime(chunks, (new_mtime, new_mtime))
    cfg2, s2 = plugin._resolve_state(str(cfg_path))
    assert s1 is not s2
