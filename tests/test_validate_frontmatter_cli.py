"""Tests for ``cortex validate-frontmatter``."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from cortex.cli import main
from cortex.config import load_config
from cortex.frontmatter_validator import validate_frontmatter


CONFIG_TEMPLATE = dedent("""\
    vault:
      path: {vault}
{vault_filters}    index:
      chunks_path: {chunks}
      chroma_path: {chroma}
      collection: test-coll
    embeddings:
      model: test-model
      device: cpu
""")


def _write_config(
    tmp_path: Path,
    vault: Path,
    *,
    include_folders: list[str] | None = None,
    exclude_folders: list[str] | None = None,
) -> Path:
    vault_filters = ""
    if include_folders is not None:
        vault_filters += f"      include_folders: [{', '.join(include_folders)}]\n"
    if exclude_folders is not None:
        vault_filters += f"      exclude_folders: [{', '.join(exclude_folders)}]\n"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(
            vault=vault,
            vault_filters=vault_filters,
            chunks=tmp_path / "chunks.jsonl",
            chroma=tmp_path / "chroma",
        ),
        encoding="utf-8",
    )
    return cfg_path


def _valid_note(*, domain: bool = True) -> str:
    lines = [
        "---",
        "type: fact",
        "status: active",
    ]
    if domain:
        lines.append("domain: test")
    lines.extend(
        [
            "tags: [memory]",
            "confidence: high",
            "importance: high",
            "stability: stable",
            "---",
            "",
            "# Valid",
            "",
            "Body.",
            "",
        ]
    )
    return "\n".join(lines)


def test_validate_frontmatter_valid_note_is_clean(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "10_facts").mkdir(parents=True)
    (vault / "10_facts" / "Valid.md").write_text(_valid_note(), encoding="utf-8")
    cfg = _write_config(tmp_path, vault)

    rc = main(["validate-frontmatter", "--config", str(cfg)])

    assert rc == 0


def test_validate_frontmatter_missing_required_is_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Missing.md").write_text(
        dedent("""\
            ---
            type: fact
            tags: []
            confidence: high
            importance: high
            stability: stable
            ---
            # Missing status
        """),
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, vault)

    rc = main(["validate-frontmatter", "--config", str(cfg)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "missing_required" in out
    assert "status" in out


def test_validate_frontmatter_unknown_enum_is_warning_and_strict_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Unknown.md").write_text(
        _valid_note().replace("status: active", "status: accepted"),
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, vault)

    rc = main(["validate-frontmatter", "--config", str(cfg)])
    assert rc == 0
    assert "unknown status" in capsys.readouterr().out

    strict_rc = main(["validate-frontmatter", "--config", str(cfg), "--strict"])
    assert strict_rc == 1


def test_validate_frontmatter_yaml_parse_failure_is_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Broken.md").write_text(
        dedent("""\
            ---
            type: [broken
            ---
            # Broken
        """),
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, vault)

    rc = main(["validate-frontmatter", "--config", str(cfg)])

    assert rc == 1


def test_validate_frontmatter_json_output_is_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Warn.md").write_text(_valid_note(domain=False), encoding="utf-8")
    cfg = _write_config(tmp_path, vault)

    rc = main(["validate-frontmatter", "--config", str(cfg), "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["checked_count"] == 1
    assert payload["error_count"] == 0
    assert payload["warning_count"] == 1
    assert payload["files"][0]["issues"][0]["code"] == "missing_domain"


def test_validate_frontmatter_blank_domain_warns(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "BlankDomain.md").write_text(
        _valid_note().replace("domain: test", "domain:   "),
        encoding="utf-8",
    )
    cfg = _write_config(tmp_path, vault)

    report = validate_frontmatter(load_config(cfg))

    assert report.error_count == 0
    assert report.warning_count == 1
    assert report.files[0].issues[0].code == "missing_domain"


def test_validate_frontmatter_explicit_relative_path_limits_scope(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Good.md").write_text(_valid_note(), encoding="utf-8")
    (vault / "Bad.md").write_text("# no frontmatter\n", encoding="utf-8")
    cfg = _write_config(tmp_path, vault)

    report = validate_frontmatter(load_config(cfg), paths=["Good.md"])

    assert report.checked_count == 1
    assert report.files[0].file == "Good.md"
    assert report.error_count == 0


def test_validate_frontmatter_explicit_directory_uses_vault_skip_rules(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Good.md").write_text(_valid_note(), encoding="utf-8")
    (vault / "README.md").write_text("# intentionally not a note\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "Ignored.md").write_text("# ignored\n", encoding="utf-8")
    cfg = _write_config(tmp_path, vault)

    report = validate_frontmatter(load_config(cfg), paths=["."])

    assert [file.file for file in report.files] == ["Good.md"]
    assert report.error_count == 0


def test_validate_frontmatter_explicit_directory_honors_include_exclude_folders(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "keep").mkdir(parents=True)
    (vault / "skip").mkdir()
    (vault / "other").mkdir()
    (vault / "keep" / "Note.md").write_text(_valid_note(), encoding="utf-8")
    (vault / "skip" / "Note.md").write_text(_valid_note(), encoding="utf-8")
    (vault / "other" / "Note.md").write_text(_valid_note(), encoding="utf-8")
    cfg = _write_config(
        tmp_path,
        vault,
        include_folders=["keep", "skip"],
        exclude_folders=["skip"],
    )

    report = validate_frontmatter(load_config(cfg), paths=["."])
    excluded_dir_report = validate_frontmatter(load_config(cfg), paths=["skip"])

    assert [file.file for file in report.files] == ["keep/Note.md"]
    assert report.error_count == 0
    assert excluded_dir_report.checked_count == 0


def test_validate_frontmatter_explicit_single_file_bypasses_directory_filters(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "skip").mkdir(parents=True)
    (vault / "skip" / "Note.md").write_text(_valid_note(), encoding="utf-8")
    cfg = _write_config(tmp_path, vault, include_folders=["keep"], exclude_folders=["skip"])

    report = validate_frontmatter(load_config(cfg), paths=["skip/Note.md"])

    assert [file.file for file in report.files] == ["skip/Note.md"]
    assert report.error_count == 0


def test_validate_frontmatter_rejects_explicit_path_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = _write_config(tmp_path, vault)

    rc = main(["validate-frontmatter", "--config", str(cfg), "--path", "../outside.md"])

    assert rc == 2


def test_validate_frontmatter_command_registered_in_shared_parser(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["validate-frontmatter", "--help"])

    assert exc.value.code == 0
    assert "--strict" in capsys.readouterr().out
