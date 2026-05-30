"""Tests for cortex.installer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cortex.config import load_config
from cortex.frontmatter_validator import validate_frontmatter
from cortex.graph_index import build_graph
from cortex.indexer import index_vault
from cortex.installer import (
    InstallPlan,
    Installer,
    Prompt,
    VAULT_FOLDERS,
    build_plan_interactively,
)


# ---- Fake prompt for scripted interactions ---------------------------------


class FakePrompt(Prompt):
    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.output: list[str] = []

        def reader(_msg: str) -> str:
            if not self.answers:
                raise AssertionError("FakePrompt out of answers; output so far:\n  " + "\n  ".join(self.output))
            return self.answers.pop(0)

        def writer(msg: str) -> None:
            self.output.append(msg)

        super().__init__(reader=reader, writer=writer)


# ---- Plan execution --------------------------------------------------------


@pytest.fixture
def plan(tmp_path: Path) -> InstallPlan:
    return InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=None,
        hermes_user_path=None,
        hermes_soul_path=None,
        overwrite_policy="force",
    )


def test_run_creates_vault_skeleton(plan: InstallPlan) -> None:
    Installer(plan, prompt=FakePrompt([])).run()
    for folder in VAULT_FOLDERS:
        assert (plan.vault_path / folder).is_dir()


def test_run_installs_templates(plan: InstallPlan) -> None:
    Installer(plan, prompt=FakePrompt([])).run()
    templates = list((plan.vault_path / "80_templates").glob("*.md"))
    names = sorted(p.name for p in templates)
    assert names == ["decision-note.md", "fact-note.md", "project-note.md", "runbook-note.md"]


def test_run_installs_seed_notes(plan: InstallPlan) -> None:
    Installer(plan, prompt=FakePrompt([])).run()
    assert (plan.vault_path / "10_facts" / "Vault Schema.md").exists()
    assert (plan.vault_path / "60_maps" / "Map - Jarvis Knowledge Index.md").exists()
    assert not (plan.vault_path / "60_maps" / "README.md").exists()
    assert (plan.vault_path / "30_projects" / "Project - hermes-cortex.md").exists()


def test_seeded_vault_is_frontmatter_index_and_graph_clean(plan: InstallPlan) -> None:
    Installer(plan, prompt=FakePrompt([])).run()
    cfg = load_config(plan.config_path)

    fm_report = validate_frontmatter(cfg)
    assert fm_report.error_count == 0
    assert fm_report.warning_count == 0

    index_report = index_vault(cfg, force=True)
    assert index_report.notes_missing_frontmatter == []
    assert index_report.notes_invalid_frontmatter == []
    assert index_report.notes_with_warnings == []
    assert index_report.errors == []

    chunk_files = {
        json.loads(line)["file"]
        for line in plan.chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert "60_maps/README.md" not in chunk_files

    graph_report = build_graph(cfg, force=True)
    assert graph_report.broken == 0
    broken_path = plan.chunks_path.parent / "graph" / "graph_broken.jsonl"
    assert broken_path.read_text(encoding="utf-8").strip() == ""

    seed_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (plan.vault_path / "60_maps").glob("*.md")
    )
    for private_target in (
        "[[Alpha Workstation]]",
        "[[Jarvis VM]]",
        "[[Obsidian Vault]]",
        "[[Skynet Host]]",
        "[[Project - Jarvis Homebase]]",
        "[[Runbook - Promote session knowledge]]",
    ):
        assert private_target not in seed_text


def test_run_writes_valid_config(plan: InstallPlan) -> None:
    Installer(plan, prompt=FakePrompt([])).run()
    raw = yaml.safe_load(plan.config_path.read_text())
    assert raw["vault"]["path"] == str(plan.vault_path)
    assert raw["index"]["chroma_path"] == str(plan.chroma_path)
    assert raw["context_builder"]["include_hermes_memory"] is False
    assert raw["cron"]["nightly_promotion"]["deliver"] == "origin"
    assert raw["cron"]["nightly_promotion"]["session_globs"] == [
        "~/.hermes/sessions/*.jsonl",
        "~/.hermes/sessions/session_*.json",
    ]
    assert "PersonalName" not in plan.config_path.read_text()
    assert "hermes_memory" in raw  # may be empty dict


def test_default_install_plan_does_not_imply_markdown_mutation() -> None:
    plan = InstallPlan()
    assert plan.update_hermes_memory is False
    assert plan.update_hermes_soul_memory_rules is False


def test_run_with_hermes_memory_paths(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    mem.write_text("x")
    plan = InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=mem,
        hermes_user_path=None,
        hermes_soul_path=None,
        update_hermes_memory=False,
        overwrite_policy="force",
    )
    Installer(plan, prompt=FakePrompt([])).run()
    raw = yaml.safe_load(plan.config_path.read_text())
    assert raw["hermes_memory"]["memory_path"] == str(mem)
    assert "user_path" not in raw["hermes_memory"]


def test_default_install_plan_does_not_mutate_hermes_markdown(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    user = tmp_path / "USER.md"
    soul = tmp_path / "SOUL.md"
    originals = {
        mem: "# Runtime\n\n- Obsidian vault: `/old`\n",
        user: "# User\n\noriginal user\n",
        soul: "# Soul\n\noriginal soul\n",
    }
    for path, content in originals.items():
        path.write_text(content)
        assert path.exists()

    plan = InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=mem,
        hermes_user_path=user,
        hermes_soul_path=soul,
        overwrite_policy="force",
    )

    Installer(plan, prompt=FakePrompt([])).run()

    for path, content in originals.items():
        assert path.read_text() == content
    assert not mem.with_suffix(".md.bak").exists()
    assert not any("Update Hermes MEMORY.md" in a for a in plan.actions)
    assert not any("Update SOUL.md" in a for a in plan.actions)
    assert not any("path not configured or file not found" in a for a in plan.actions)


def test_default_install_plan_keeps_soul_memory_rules_unchanged(tmp_path: Path) -> None:
    soul = tmp_path / "SOUL.md"
    original = "# Soul\n\nNo memory rules here yet.\n"
    soul.write_text(original)

    plan = InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=None,
        hermes_user_path=None,
        hermes_soul_path=soul,
        overwrite_policy="force",
    )

    Installer(plan, prompt=FakePrompt([])).run()

    assert soul.read_text() == original
    assert "# Memory Rules" not in soul.read_text()
    assert not any("Update SOUL.md" in a for a in plan.actions)


def test_run_updates_hermes_memory_vault_coordinates(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    old_vault = tmp_path / "old-vault"
    new_vault = tmp_path / "new-vault"
    mem.write_text(
        "# Runtime\n\n"
        f"- Obsidian vault: `{old_vault}`\n"
        "- Workspace: `~/hermes-workspace`\n\n"
        "# Memory Model\n\n"
        "- Obsidian vault = Jarvis' curated long-term memory for durable knowledge.\n"
    )
    plan = InstallPlan(
        vault_path=new_vault,
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=mem,
        hermes_user_path=None,
        hermes_soul_path=None,
        update_hermes_memory=True,
        overwrite_policy="force",
    )

    Installer(plan, prompt=FakePrompt([])).run()

    updated = mem.read_text()
    assert f"- Obsidian vault: `{new_vault}`" in updated
    assert f"- Cortex-backed vault: `{new_vault}`\n\n# Memory Model" in updated
    assert str(new_vault) in updated
    assert str(old_vault) not in updated
    assert mem.with_suffix(".md.bak").read_text().startswith("# Runtime")
    assert any("Update Hermes MEMORY.md vault coordinates" in a for a in plan.actions)


def test_dry_run_does_not_update_hermes_memory(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    original = "# Runtime\n\n- Obsidian vault: `/old`\n"
    mem.write_text(original)
    plan = InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=mem,
        hermes_user_path=None,
        hermes_soul_path=None,
        update_hermes_memory=True,
        dry_run=True,
        overwrite_policy="force",
    )

    Installer(plan, prompt=FakePrompt([])).run()

    assert mem.read_text() == original
    assert not mem.with_suffix(".md.bak").exists()


def test_explicit_legacy_soul_memory_rules_opt_in(tmp_path: Path) -> None:
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Soul\n\noriginal soul\n")
    plan = InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=None,
        hermes_user_path=None,
        hermes_soul_path=soul,
        update_hermes_soul_memory_rules=True,
        overwrite_policy="force",
    )

    Installer(plan, prompt=FakePrompt([])).run()

    updated = soul.read_text()
    assert "# Memory Rules" in updated
    assert str(plan.vault_path) in updated
    assert any("Update SOUL.md with Memory Rules section" in a for a in plan.actions)


def test_cli_init_yes_does_not_mutate_default_hermes_markdown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    memories = hermes_home / "memories"
    memories.mkdir(parents=True)
    soul = hermes_home / "SOUL.md"
    mem = memories / "MEMORY.md"
    user = memories / "USER.md"
    originals = {
        soul: "# Soul\n\noriginal soul\n",
        mem: "# Runtime\n\n- Obsidian vault: `/old`\n",
        user: "# User\n\noriginal user\n",
    }
    for path, content in originals.items():
        path.write_text(content)
        assert path.exists()

    before = {path: path.read_bytes() for path in originals}
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "PYTHONPATH": str(repo_root),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cortex.cli",
            "init",
            "--yes",
            "--vault",
            str(tmp_path / "vault"),
            "--config",
            str(hermes_home / "cortex" / "config.yaml"),
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert all(path.read_bytes() == content for path, content in before.items())
    assert "Update Hermes MEMORY.md" not in output
    assert "Update SOUL.md" not in output
    assert "path not configured or file not found" not in output
    assert "hermes_memory:" in (hermes_home / "cortex" / "config.yaml").read_text()


def test_dry_run_writes_nothing(plan: InstallPlan) -> None:
    plan.dry_run = True
    Installer(plan, prompt=FakePrompt([])).run()
    assert not plan.vault_path.exists()
    assert not plan.config_path.exists()
    # but actions are recorded
    assert any("Create" in a or "OVERWRITE" in a for a in plan.actions)


def test_overwrite_policy_skip(plan: InstallPlan) -> None:
    plan.overwrite_policy = "force"
    Installer(plan, prompt=FakePrompt([])).run()
    # Touch a known seed file
    target = plan.vault_path / "10_facts" / "Vault Schema.md"
    target.write_text("MY EDIT")

    plan.overwrite_policy = "skip"
    plan.actions.clear()
    Installer(plan, prompt=FakePrompt([])).run()
    assert target.read_text() == "MY EDIT"
    assert any("SKIP" in a for a in plan.actions)


def test_overwrite_policy_ask_yes(plan: InstallPlan) -> None:
    plan.overwrite_policy = "force"
    Installer(plan, prompt=FakePrompt([])).run()
    target = plan.vault_path / "10_facts" / "Vault Schema.md"
    target.write_text("MY EDIT")

    plan.overwrite_policy = "ask"
    # We need enough "y" answers for every existing file the run encounters.
    # The simpler check: provide many y's and ensure file gets overwritten.
    answers = ["y"] * 50
    Installer(plan, prompt=FakePrompt(answers)).run()
    assert target.read_text() != "MY EDIT"


def test_install_flags_disable_optional_steps(tmp_path: Path) -> None:
    plan = InstallPlan(
        vault_path=tmp_path / "vault",
        config_path=tmp_path / "config.yaml",
        chunks_path=tmp_path / "chunks.jsonl",
        chroma_path=tmp_path / "chroma",
        hermes_memory_path=None, hermes_user_path=None, hermes_soul_path=None,
        install_templates=False,
        install_seed_notes=False,
        install_vault_readme=False,
        overwrite_policy="force",
    )
    Installer(plan, prompt=FakePrompt([])).run()
    assert not list((plan.vault_path / "80_templates").glob("*.md"))
    assert not (plan.vault_path / "10_facts" / "Vault Schema.md").exists()
    assert not (plan.vault_path / "README.md").exists()
    # Skeleton folders still created
    assert (plan.vault_path / "10_facts").is_dir()


# ---- Interactive plan builder ----------------------------------------------


def test_build_plan_interactively_defaults(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    cfg = tmp_path / "cfg.yaml"
    answers = [
        str(vault),       # vault path
        str(cfg),         # config path
        "",               # templates? default Y
        "",               # seed notes? default Y
        "",               # vault README? default Y
        "n",              # auto-detect hermes paths? -> NO (so we get to prompt for them)
        "",               # MEMORY.md (default — probably won't exist)
        "",               # USER.md
        "",               # SOUL.md
        "",               # legacy update MEMORY.md vault coordinates? default N
        "",               # legacy patch SOUL.md Memory Rules? default N
        "",               # overwrite policy default ask
        "",               # proceed? default Y
    ]
    fake = FakePrompt(answers)
    plan = build_plan_interactively(prompt=fake)
    assert plan.vault_path == vault.resolve()
    assert plan.config_path == cfg.resolve()
    assert plan.install_templates is True
    assert plan.install_seed_notes is True
    assert plan.overwrite_policy == "ask"


def test_build_plan_abort(tmp_path: Path) -> None:
    answers = [
        str(tmp_path / "vault"),
        str(tmp_path / "cfg.yaml"),
        "", "", "",
        "y",  # auto-detect
        "",   # legacy update MEMORY.md vault coordinates? default false
        "",   # legacy patch SOUL.md Memory Rules? default false
        "",   # overwrite default
        "n",  # proceed? -> NO
    ]
    with pytest.raises(SystemExit):
        build_plan_interactively(prompt=FakePrompt(answers))
