"""End-to-end tests for the cortex CLI.

We invoke ``cli.main()`` directly with argv lists. Heavy deps (chromadb,
sentence-transformers) are mocked when needed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock

import pytest
import yaml

from cortex.cli import main
from cortex.installer import InstallPlan


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


def _setup_wiki_health(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for rel in [
        "00_inbox",
        "10_facts",
        "20_decisions",
        "30_projects",
        "40_runbooks",
        "50_people",
        "60_maps",
        "80_templates",
        "raw/articles",
        "raw/papers",
        "raw/transcripts",
        "raw/assets",
    ]:
        (vault / rel).mkdir(parents=True, exist_ok=True)
    for rel in ["SCHEMA.md", "index.md", "log.md"]:
        (vault / rel).write_text(f"# {rel}\n", encoding="utf-8")
    (vault / "raw" / "README.md").write_text("# Raw\n", encoding="utf-8")

    raw_body = "# Source\n\nOriginal raw body.\n"
    raw_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    (vault / "raw" / "articles" / "source.md").write_text(
        f"---\nsource_url: https://example.invalid/source\ningested: 2026-07-03\nsha256: {raw_hash}\n---\n\n{raw_body}",
        encoding="utf-8",
    )

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        dedent(
            f"""\
            vault:
              path: {vault}
              include_folders: [10_facts, 20_decisions, 30_projects, 40_runbooks, 50_people, 60_maps]
              exclude_folders: [00_inbox, 80_templates, raw]
            index:
              chunks_path: {tmp_path / "chunks.jsonl"}
              chroma_path: {tmp_path / "chroma"}
              collection: test-coll
            embeddings:
              model: test-model
              device: cpu
            """
        ),
        encoding="utf-8",
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


def test_cli_init_yes_explicit_vault_mismatch_aborts_without_partial_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``init --yes --config existing --vault NEW`` where NEW != config vault.path
    must abort before seed writes; no partial seed in NEW.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    configured = tmp_path / "configured-vault"
    configured.mkdir()
    new_vault = tmp_path / "new-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        CONFIG_TEMPLATE.format(
            vault=configured,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        ),
        encoding="utf-8",
    )
    cfg_before = cfg.read_text(encoding="utf-8")

    rc = main(["init", "--yes", "--config", str(cfg), "--vault", str(new_vault)])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to seed" in err
    assert "points at" in err
    assert "--yes" in err
    assert not new_vault.exists()
    assert not (new_vault / "SCHEMA.md").exists()
    assert cfg.read_text(encoding="utf-8") == cfg_before


@pytest.mark.parametrize(
    "config_text",
    [
        "vault: [\n",
        "vault: {}\n",
    ],
    ids=["invalid-yaml", "missing-vault-path"],
)
def test_cli_init_yes_existing_config_without_readable_vault_path_aborts_before_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_text: str,
) -> None:
    """An existing config that cannot provide ``vault.path`` must not be
    preserved while a separate explicit Vault is seeded.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    new_vault = tmp_path / "new-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(config_text, encoding="utf-8")
    cfg_before = cfg.read_text(encoding="utf-8")

    rc = main(["init", "--yes", "--config", str(cfg), "--vault", str(new_vault)])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to seed" in err
    assert "readable vault.path" in err
    assert "--yes" in err
    assert not new_vault.exists()
    assert not (new_vault / "SCHEMA.md").exists()
    assert cfg.read_text(encoding="utf-8") == cfg_before


def test_init_help_describes_vault_selection_without_runtime_override(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["init", "--help"])
    assert excinfo.value.code == 0

    help_text = capsys.readouterr().out
    assert "Override vault path" not in help_text
    assert "Select the Vault path during init" in help_text


def test_cli_init_yes_explicit_vault_matching_config_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init --yes --config existing --vault MATCH`` where MATCH normalizes to
    config vault.path must succeed normally.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    configured = tmp_path / "configured-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        CONFIG_TEMPLATE.format(
            vault=configured,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        ),
        encoding="utf-8",
    )

    rc = main(["init", "--yes", "--config", str(cfg), "--vault", str(configured)])

    assert rc == 0
    assert (configured / "SCHEMA.md").exists()


@pytest.mark.parametrize(
    ("wiki_env_value", "expected_fragment"),
    [
        pytest.param(None, "WIKI_PATH is unset", id="unset"),
        pytest.param("SELF", "aligned;", id="aligned"),
        pytest.param("OTHER", "mismatch;", id="mismatch"),
    ],
)
def test_cli_init_dry_run_reports_llm_wiki_alignment_for_all_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wiki_env_value: str | None,
    expected_fragment: str,
) -> None:
    """``init --dry-run`` must print the LLM-Wiki alignment line for all three
    cases and write nothing to disk.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    if wiki_env_value == "SELF":
        monkeypatch.setenv("WIKI_PATH", str(vault))
    elif wiki_env_value == "OTHER":
        monkeypatch.setenv("WIKI_PATH", str(tmp_path / "other-wiki"))
    else:
        monkeypatch.delenv("WIKI_PATH", raising=False)

    rc = main(["init", "--yes", "--dry-run", "--vault", str(vault), "--config", str(cfg)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "LLM-Wiki alignment:" in out
    assert expected_fragment in out
    assert str(vault.resolve()) in out
    # dry-run must not write anything
    assert not cfg.exists()
    assert not vault.exists()


def test_cli_init_dry_run_alignment_does_not_mutate_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init --dry-run`` with mismatching WIKI_PATH must not mutate any file
    tree or mtimes under tmp_path.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    cfg.parent.mkdir()
    monkeypatch.setenv("WIKI_PATH", str(tmp_path / "other-wiki"))

    before = {
        p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    rc = main(["init", "--yes", "--dry-run", "--vault", str(vault), "--config", str(cfg)])

    assert rc == 0
    after = {
        p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_cli_init_yes_mismatch_refusal_still_aborts_before_seed_with_alignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The existing ``--yes`` mismatch refusal must still abort before seed
    writes even when the alignment diagnostic is present.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    configured = tmp_path / "configured-vault"
    configured.mkdir()
    new_vault = tmp_path / "new-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text(
        CONFIG_TEMPLATE.format(
            vault=configured,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        ),
        encoding="utf-8",
    )
    cfg_before = cfg.read_text(encoding="utf-8")
    monkeypatch.setenv("WIKI_PATH", str(configured))

    rc = main(["init", "--yes", "--config", str(cfg), "--vault", str(new_vault)])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Refusing to seed" in err
    assert not new_vault.exists()
    assert cfg.read_text(encoding="utf-8") == cfg_before


def test_cli_init_yes_explicit_vault_fresh_config_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init --yes --vault NEW`` without an existing config must seed NEW and
    write the fresh config pointing at NEW.
    """
    _isolate_hermes_home(tmp_path, monkeypatch)
    new_vault = tmp_path / "new-vault"
    cfg = tmp_path / "cortex" / "config.yaml"
    cfg.parent.mkdir()

    rc = main(["init", "--yes", "--config", str(cfg), "--vault", str(new_vault)])

    assert rc == 0
    assert (new_vault / "SCHEMA.md").exists()
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert raw["vault"]["path"] == str(new_vault.resolve())


def test_cli_init_yes_without_config_uses_active_profile_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile_home = _isolate_hermes_home(tmp_path, monkeypatch)
    configured = tmp_path / "profile-vault"
    new_vault = tmp_path / "new-vault"
    profile_config = profile_home / "cortex" / "config.yaml"
    profile_config.parent.mkdir()
    profile_config.write_text(
        CONFIG_TEMPLATE.format(
            vault=configured,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        ),
        encoding="utf-8",
    )

    rc = main(["init", "--yes", "--vault", str(new_vault)])

    assert rc == 2
    err = capsys.readouterr().err
    assert str(profile_config.resolve()) in err
    assert not new_vault.exists()


def test_cli_init_interactive_mismatch_returns_two_before_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_hermes_home(tmp_path, monkeypatch)
    old_vault = tmp_path / "old-vault"
    new_vault = tmp_path / "new-vault"
    config = tmp_path / "config.yaml"
    config.write_text(f"vault:\n  path: {old_vault}\n", encoding="utf-8")
    plan = InstallPlan(
        vault_path=new_vault,
        config_path=config,
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=None,
        hermes_user_path=None,
        hermes_soul_path=None,
        overwrite_policy="skip",
    )
    monkeypatch.setattr(
        "cortex.cli.build_plan_interactively",
        lambda config_path=None, explicit_vault=None: plan,
    )

    rc = main(["init", "--config", str(config), "--vault", str(new_vault)])

    assert rc == 2
    assert "Refusing to seed" in capsys.readouterr().err
    assert not new_vault.exists()


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


def test_wiki_health_success_and_json_are_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _setup_wiki_health(tmp_path)
    before = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "config_path": str(cfg),
        "vault_path": str(tmp_path / "vault"),
        "ok": True,
        "error_count": 0,
        "warning_count": 0,
        "issue_count": 0,
        "issues": [],
    }
    after = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_wiki_health_reports_missing_contract_items(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _setup_wiki_health(tmp_path)
    vault = tmp_path / "vault"
    (vault / "SCHEMA.md").unlink()
    (vault / "raw" / "papers").rmdir()
    (vault / "50_people").rmdir()

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    issues = {(i["code"], i["path"]) for i in payload["issues"]}
    assert ("missing_root_file", "SCHEMA.md") in issues
    assert ("missing_raw_folder", "raw/papers") in issues
    assert ("missing_cortex_folder", "50_people") in issues


def test_wiki_health_reports_curated_source_config_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _setup_wiki_health(tmp_path)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["vault"]["include_folders"] = ["10_facts", "raw", "00_inbox", "80_templates"]
    raw["vault"]["exclude_folders"] = []
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    drift_paths = [i["path"] for i in payload["issues"] if i["code"] == "config_curated_source_drift"]
    assert drift_paths == ["00_inbox", "80_templates", "raw"]


def test_wiki_health_reports_empty_include_config_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _setup_wiki_health(tmp_path)
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    raw["vault"]["include_folders"] = []
    raw["vault"]["exclude_folders"] = ["00_inbox"]
    cfg.write_text(yaml.safe_dump(raw), encoding="utf-8")

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    drift_paths = [i["path"] for i in payload["issues"] if i["code"] == "config_curated_source_drift"]
    assert drift_paths == ["80_templates", "raw"]


def test_wiki_health_strict_turns_raw_metadata_warnings_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _setup_wiki_health(tmp_path)
    raw_source = tmp_path / "vault" / "raw" / "articles" / "source.md"
    raw_source.write_text("---\nsource_url: not-a-url\ningested: nope\n---\n\n# Body\n", encoding="utf-8")

    rc_default = main(["wiki-health", "--config", str(cfg), "--json"])
    payload_default = json.loads(capsys.readouterr().out)
    rc_strict = main(["wiki-health", "--config", str(cfg), "--json", "--strict"])
    payload_strict = json.loads(capsys.readouterr().out)

    assert rc_default == 0
    assert rc_strict == 1
    warning_codes = {i["code"] for i in payload_default["issues"]}
    assert {"raw_source_invalid_source_url", "raw_source_invalid_ingested", "raw_source_missing_sha256"} <= warning_codes
    assert payload_strict["warning_count"] == payload_default["warning_count"]


def test_wiki_health_reports_raw_body_sha256_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _setup_wiki_health(tmp_path)
    raw_source = tmp_path / "vault" / "raw" / "articles" / "source.md"
    raw_source.write_text(raw_source.read_text(encoding="utf-8") + "mutated\n", encoding="utf-8")

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert ("raw_source_sha256_drift", "raw/articles/source.md") in {
        (i["code"], i["path"]) for i in payload["issues"]
    }


def test_wiki_health_missing_contract_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing-contract/error paths must not mutate the vault tree."""
    cfg = _setup_wiki_health(tmp_path)
    vault = tmp_path / "vault"
    (vault / "SCHEMA.md").unlink()
    (vault / "raw" / "papers").rmdir()
    before = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    after = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_wiki_health_sha256_drift_path_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error path for raw body sha256 drift must not mutate the vault tree."""
    cfg = _setup_wiki_health(tmp_path)
    raw_source = tmp_path / "vault" / "raw" / "articles" / "source.md"
    raw_source.write_text(raw_source.read_text(encoding="utf-8") + "mutated\n", encoding="utf-8")
    before = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    after = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_wiki_health_log_md_as_directory_is_reported_and_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If log.md is a directory (non-file), wiki-health must report it as a
    missing root file and not mutate the vault tree.
    """
    cfg = _setup_wiki_health(tmp_path)
    vault = tmp_path / "vault"
    (vault / "log.md").unlink()
    (vault / "log.md").mkdir()
    before = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}

    rc = main(["wiki-health", "--config", str(cfg), "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert ("missing_root_file", "log.md") in {(i["code"], i["path"]) for i in payload["issues"]}
    after = {p.relative_to(tmp_path).as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


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
