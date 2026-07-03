"""End-to-end tests for the cortex CLI.

We invoke ``cli.main()`` directly with argv lists. Heavy deps (chromadb,
sentence-transformers) are mocked when needed.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
import yaml

from cortex.cli import main


CONFIG_TEMPLATE = dedent("""\
    vault:
      path: {vault}
    index:
      chunks_path: {chunks}
      chroma_path: {chroma}
      collection: test-coll
    embeddings:
      model: test-model
      device: cpu
""")


SAMPLE_NOTE = dedent("""\
    ---
    type: fact
    status: active
    tags: [memory]
    confidence: high
    importance: high
    stability: stable
    ---

    # Title

    ## Section

    body text
""")


def _setup(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "10_facts").mkdir(parents=True)
    (vault / "10_facts" / "A.md").write_text(SAMPLE_NOTE)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(
            vault=vault,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        )
    )
    return cfg_path


def _isolate_hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: []\n  disabled: []\nplatform_toolsets:\n  cli: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def test_cli_init_yes_uses_wiki_path_only_for_fresh_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_hermes_home(tmp_path, monkeypatch)
    wiki = tmp_path / "wiki-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    monkeypatch.setenv("WIKI_PATH", str(wiki))

    rc = main(["init", "--yes", "--config", str(cfg)])

    assert rc == 0
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["vault"]["path"] == str(wiki.resolve())
    assert (wiki / "SCHEMA.md").exists()
    assert (wiki / "raw" / "articles").is_dir()
    out = capsys.readouterr().out
    assert f"Vault path default: {wiki.resolve()} (source: WIKI_PATH)" in out


def test_cli_init_yes_existing_config_wins_over_wiki_path_and_runtime_uses_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_hermes_home(tmp_path, monkeypatch)
    configured = tmp_path / "configured-vault"
    wiki = tmp_path / "wiki-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    configured.mkdir()
    cfg.parent.mkdir()
    cfg.write_text(
        CONFIG_TEMPLATE.format(
            vault=configured,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        ),
        encoding="utf-8",
    )
    before = cfg.read_text(encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(wiki))

    rc = main(["init", "--yes", "--config", str(cfg)])
    assert rc == 0
    assert cfg.read_text(encoding="utf-8") == before
    assert (configured / "SCHEMA.md").exists()
    assert not wiki.exists()
    out = capsys.readouterr().out
    assert f"Vault path default: {configured.resolve()} (source: existing config)" in out
    assert "retained over WIKI_PATH" in out
    assert "planned vault.path" in out

    rc = main(["status", "--config", str(cfg)])
    assert rc == 0
    status_out = capsys.readouterr().out
    assert f"Vault:          {configured.resolve()} (ok)" in status_out
    assert str(wiki.resolve()) not in status_out

def test_config_show_legacy_context_label_depends_on_semantic_presence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy_cfg = _setup(tmp_path / "legacy")
    legacy_cfg.write_text(
        legacy_cfg.read_text()
        + """
hooks:
  context_injection:
    enabled: true
"""
    )
    rc = main(["config", "show", "--config", str(legacy_cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Legacy context:  True\n" in out
    assert "deprecated/ignored" not in out

    mixed_dir = tmp_path / "mixed"
    mixed_dir.mkdir()
    mixed_cfg = _setup(mixed_dir)
    mixed_cfg.write_text(
        mixed_cfg.read_text()
        + """
hooks:
  context_injection:
    enabled: true
  dynamic_context:
    enabled: false
"""
    )
    rc = main(["config", "show", "--config", str(mixed_cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Legacy context:  True (deprecated/ignored)" in out


def test_status_prints_hook_lifecycle_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _setup(tmp_path)
    cfg.write_text(
        cfg.read_text()
        + """
hooks:
  context_injection:
    enabled: true
  dynamic_context:
    enabled: true
    budget: 123
"""
    )
    rc = main(["status", "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Hook lifecycle:" in out
    assert "Runtime mode: semantic" in out
    assert "dynamic_context" in out
    assert "legacy_context_injection" in out
    assert "legacy-ignored" in out
    assert "ignored because semantic hook blocks are present" in out


def test_cli_index_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _setup(tmp_path)
    rc = main(["index", "--config", str(cfg)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Indexed" in out
    assert (tmp_path / "chunks.jsonl").exists()


def test_cli_reset_requires_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _setup(tmp_path)
    rc = main(["reset", "--config", str(cfg)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Nothing to reset" in err


def test_cli_reset_chunks(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    chunks_file = tmp_path / "chunks.jsonl"
    assert chunks_file.exists()

    rc = main(["reset", "--config", str(cfg), "--chunks", "--yes"])
    assert rc == 0
    assert not chunks_file.exists()


def test_cli_reset_chroma(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir()
    (chroma_dir / "marker").write_text("x")

    rc = main(["reset", "--config", str(cfg), "--chroma", "--yes"])
    assert rc == 0
    assert not chroma_dir.exists()


def test_cli_reset_all(tmp_path: Path) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    chroma_dir = tmp_path / "chroma"
    chroma_dir.mkdir(exist_ok=True)
    (chroma_dir / "x").write_text("x")

    rc = main(["reset", "--config", str(cfg), "--all", "--yes"])
    assert rc == 0
    assert not (tmp_path / "chunks.jsonl").exists()
    assert not chroma_dir.exists()


def test_cli_embed_handles_model_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model mismatch should produce a non-zero exit and a clear error message."""
    from cortex import embedder as embedder_mod

    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])

    def boom(*a, **kw):
        raise embedder_mod.ModelMismatchError(
            "Embedding model mismatch: collection was built with 'old' but config says 'new'."
        )

    monkeypatch.setattr(embedder_mod, "embed_chunks", boom)
    rc = main(["embed", "--config", str(cfg)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "mismatch" in err.lower()


# ---- search subcommand ----------------------------------------------------


def _stub_chroma_and_st(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch chromadb + sentence_transformers with empty/canned fakes so
    ``cortex search`` can run BM25-only without a real backend.
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


def test_cli_search_text_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()  # discard index output
    _stub_chroma_and_st(monkeypatch)

    rc = main(["search", "body text", "--config", str(cfg), "--top-k", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Query: 'body text'" in out
    assert "10_facts/A.md" in out


def test_cli_search_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)

    rc = main(["search", "body", "--config", str(cfg), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)
    assert payload, "expected at least one hit"
    first = payload[0]
    assert {"chunk_id", "file", "final_score", "rrf_score"} <= set(first)


def test_cli_search_empty_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main(["search", "absolutelynomatchxyz", "--config", str(cfg)])
    assert rc == 0
    assert "(no results)" in capsys.readouterr().out


def test_cli_search_invalid_filter_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main([
        "search", "body", "--config", str(cfg),
        "--modified-after", "not-a-date",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "modified_after" in err


def test_cli_search_filter_csv_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--type fact,decision should pass a list to SearchFilters."""
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main([
        "search", "body", "--config", str(cfg),
        "--type", "fact,decision",
        "--top-k", "5",
    ])
    assert rc == 0  # the existing fact-typed chunk should match
    out = capsys.readouterr().out
    assert "10_facts/A.md" in out


# ---- context subcommand ---------------------------------------------------


def test_cli_context_markdown_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main(["context", "body", "--config", str(cfg)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "# Context" in captured.out
    assert "## Vault Hits" in captured.out
    assert "## Citations" in captured.out
    # Diagnostics on stderr.
    assert "[ctx] tokens=" in captured.err


def test_cli_context_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main(["context", "body", "--config", str(cfg), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert {
        "text", "tokens_used", "tokens_budget",
        "chunks_included", "chunks_skipped_oversize",
        "hermes_memory_included", "hermes_user_included", "citation_count",
    } <= set(payload)


def test_cli_context_budget_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main(["context", "body", "--config", str(cfg),
               "--budget", "5", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tokens_budget"] == 5
    # 5-token budget can't fit any real chunk → all skipped.
    assert payload["chunks_included"] == []


def test_cli_context_no_hermes_memory_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--no-hermes-memory must override config.context_builder.include_hermes_memory."""
    import json
    # Wire a config with hermes_memory enabled and a real MEMORY.md.
    vault = tmp_path / "vault"
    (vault / "10_facts").mkdir(parents=True)
    (vault / "10_facts" / "A.md").write_text(SAMPLE_NOTE)
    mem = tmp_path / "MEMORY.md"
    mem.write_text("# Memory\nstuff\n")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(
            vault=vault,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        )
        + f"hermes_memory:\n  memory_path: {mem}\n"
        + "context_builder:\n  include_hermes_memory: true\n"
    )
    main(["index", "--config", str(cfg_path)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)

    # Without --no-hermes-memory: MEMORY.md included.
    rc = main(["context", "body", "--config", str(cfg_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hermes_memory_included"] is True

    # With --no-hermes-memory: not included.
    rc = main(["context", "body", "--config", str(cfg_path),
               "--no-hermes-memory", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hermes_memory_included"] is False


def test_cli_context_invalid_filter_returns_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _setup(tmp_path)
    main(["index", "--config", str(cfg)])
    capsys.readouterr()
    _stub_chroma_and_st(monkeypatch)
    rc = main(["context", "body", "--config", str(cfg),
               "--modified-after", "garbage"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "modified_after" in err
