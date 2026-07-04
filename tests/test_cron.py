from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

from cortex.config import CronNightlyPromotionConfig, CronWeeklyReviewConfig
from cortex.cron import (
    _NIGHTLY_PROMPT,
    _WEEKLY_PROMPT,
    _build_job,
    _build_weekly_job,
    _job_id,
    _lifecycle_commands,
    _session_source_command,
    _weekly_lifecycle_command,
    install,
    status,
    uninstall,
)


def test_cron_prompt_templates_are_loaded_from_package_resources():
    from importlib import resources

    template_root = resources.files("cortex").joinpath("cron_templates")

    assert _NIGHTLY_PROMPT == template_root.joinpath("nightly_promotion.md").read_text(encoding="utf-8")
    assert _WEEKLY_PROMPT == template_root.joinpath("weekly_review.md").read_text(encoding="utf-8")


def test_cron_prompt_templates_are_persona_platform_and_language_neutral():
    forbidden_terms = (
        "Jarvis",
        bytes.fromhex("44752062697374").decode("utf-8"),
        "Signal",
        bytes.fromhex("46c3bc687265").decode("utf-8"),
        bytes.fromhex("5363687265696265").decode("utf-8"),
        bytes.fromhex("5072c3bc6665").decode("utf-8"),
        bytes.fromhex("46616c6c73").decode("utf-8"),
        bytes.fromhex("5a7573616d6d656e66617373756e67").decode("utf-8"),
        bytes.fromhex("5a6569747a6f6e65").decode("utf-8"),
    )
    for prompt in (_NIGHTLY_PROMPT, _WEEKLY_PROMPT):
        for term in forbidden_terms:
            assert term not in prompt


def test_config_example_defaults_do_not_mention_signal():
    config_example = Path(__file__).resolve().parents[1] / "config.example.yaml"

    assert "Signal" not in config_example.read_text(encoding="utf-8")


def test_nightly_prompt_uses_canonical_first_and_review_only_inbox_contract():
    prompt = _NIGHTLY_PROMPT.format(
        vault_path="/vault",
        cortex_repo="/repo",
        cortex_bin="hermes cortex",
        lookback_days=1,
        timezone="Europe/Berlin",
        state_db_path="~/.hermes/state.db",
        legacy_fallback_enabled=True,
        session_globs_block="   - `~/.hermes/sessions/*.jsonl`\n   - `~/.hermes/sessions/session_*.json`",
        session_source_command=(
            "hermes cortex session-sources --lookback-days 1 --timezone Europe/Berlin "
            "--state-db-path ~/.hermes/state.db --session-glob ~/.hermes/sessions/*.jsonl "
            "--session-glob ~/.hermes/sessions/session_*.json"
        ),
        lifecycle_commands=(
            "hermes cortex lifecycle nightly --dry-run && \\\n   hermes cortex lifecycle nightly --write && \\\n   hermes cortex lifecycle maintenance"
        ),
    )

    assert "directly to canonical Vault folders" in prompt
    assert "`10_facts/`" in prompt
    assert "`20_decisions/`" in prompt
    assert "`30_projects/`" in prompt
    assert "`40_runbooks/`" in prompt
    assert "status: draft" in prompt
    assert "review_status: pending" in prompt
    assert "review_reason:" in prompt
    assert "promote: true" in prompt
    assert "promote_type: fact|decision|runbook|project" in prompt
    assert "Never use `status: review`" in prompt
    assert "Never use `status: active` for live inbox candidates" in prompt
    assert "*.jsonl" in prompt
    assert "session_*.json" in prompt
    assert "request_dump_*.json" in prompt
    assert "hermes cortex session-sources" in prompt
    assert "Primary SessionDB" in prompt
    assert "Source: backend=<state_db|legacy_files>" in prompt
    assert "/private/dev/hermes-cortex/.venv/bin/cortex" not in prompt


def test_lifecycle_commands_apply_with_write_after_optional_dry_run():
    commands = _lifecycle_commands("hermes cortex", dry_run_first=True)

    assert commands == (
        "hermes cortex lifecycle nightly --dry-run && \\\n   hermes cortex lifecycle nightly --write && \\\n   hermes cortex lifecycle maintenance"
    )
    assert commands.count("--dry-run") == 1
    assert commands.count("--write") == 1


def test_lifecycle_commands_apply_with_write_without_dry_run_first():
    commands = _lifecycle_commands("hermes cortex", dry_run_first=False)

    assert commands == (
        "hermes cortex lifecycle nightly --write && \\\n   hermes cortex lifecycle maintenance"
    )
    assert "--dry-run" not in commands
    assert commands.count("--write") == 1


def test_build_job_uses_runtime_cli_without_stale_hardcoded_path():
    job = _build_job("/vault", "/repo", "hermes cortex")

    assert "hermes cortex lifecycle nightly --dry-run" in job["prompt"]
    assert "hermes cortex lifecycle nightly --write" in job["prompt"]
    assert "/private/dev/hermes-cortex/.venv/bin/cortex" not in job["prompt"]
    assert "python3 -m cortex.session_sources" not in job["prompt"]


def test_build_job_uses_default_public_safe_config():
    job = _build_job("/vault", "/repo", "hermes cortex")

    assert job["name"] == "hermes-cortex-nightly-promotion"
    assert job["schedule_display"] == "0 2 * * *"
    assert job["deliver"] == "origin"
    assert job["enabled_toolsets"] == ["file", "terminal"]
    assert "~/.hermes/state.db" in job["prompt"]
    assert "hermes cortex session-sources" in job["prompt"]
    assert "~/.hermes/sessions/*.jsonl" in job["prompt"]
    assert "~/.hermes/sessions/session_*.json" in job["prompt"]
    assert job["metadata"]["cortex"]["session_source"]["state_db_path"] == "~/.hermes/state.db"
    assert job["metadata"]["cortex"]["session_source"]["legacy_fallback_enabled"] is True
    assert "fallback_reason" in job["metadata"]["cortex"]["session_source"]["diagnostics_expected"]
    assert job["metadata"]["cortex"]["timezone"] == "Europe/Berlin"
    assert job["metadata"]["cortex"]["timezone_scope"].startswith("prompt/lookback only")


def test_build_job_uses_custom_config_values():
    cfg = CronNightlyPromotionConfig(
        enabled=True,
        name="custom-nightly",
        schedule="15 4 * * *",
        timezone="UTC",
        deliver="origin",
        enabled_toolsets=["file"],
        lookback_days=3,
        state_db_path="/tmp/state.db",
        legacy_fallback_enabled=False,
        session_globs=["/tmp/sessions/*.json"],
        dry_run_first=False,
    )

    job = _build_job("/vault", "/repo", "hermes cortex", cfg)

    assert job["id"] == _job_id("custom-nightly")
    assert job["name"] == "custom-nightly"
    assert job["schedule"] == {"kind": "cron", "expr": "15 4 * * *", "display": "15 4 * * *"}
    assert job["deliver"] == "origin"
    assert job["enabled_toolsets"] == ["file"]
    assert "last 3 day(s) (UTC)" in job["prompt"]
    assert "/tmp/state.db" in job["prompt"]
    assert "--no-legacy-fallback" in job["prompt"]
    assert "/tmp/sessions/*.json" in job["prompt"]
    assert "lifecycle nightly --dry-run" not in job["prompt"]
    assert "lifecycle nightly --write" in job["prompt"]
    assert job["metadata"]["cortex"]["dry_run_first"] is False
    assert job["metadata"]["cortex"]["session_source"]["state_db_path"] == "/tmp/state.db"
    assert job["metadata"]["cortex"]["session_source"]["legacy_fallback_enabled"] is False


def test_session_source_command_includes_state_db_and_legacy_controls():
    cfg = CronNightlyPromotionConfig(
        lookback_days=2,
        timezone="UTC",
        state_db_path="/tmp/state.db",
        legacy_fallback_enabled=False,
        session_globs=["/tmp/*.json"],
    )

    command = _session_source_command(cfg)

    assert "hermes cortex session-sources" in command
    assert "--lookback-days 2" in command
    assert "--timezone UTC" in command
    assert "--state-db-path /tmp/state.db" in command
    assert "--no-legacy-fallback" in command
    assert "--session-glob '/tmp/*.json'" in command


def test_install_uses_runtime_cortex_cli(monkeypatch, tmp_path):
    captured = {}

    def fake_build_job(vault_path, cortex_repo, cortex_bin, cron_config=None):
        captured["vault_path"] = str(vault_path)
        captured["cortex_repo"] = str(cortex_repo)
        captured["cortex_bin"] = str(cortex_bin)
        captured["cron_config"] = cron_config
        return {
            "id": "test-job",
            "name": "hermes-cortex-nightly-promotion",
            "created_at": "now",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "repeat": {"times": None, "completed": 0},
        }

    configured_vault = tmp_path / "cfg-vault"
    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(nightly_promotion=CronNightlyPromotionConfig(enabled=True)),
            vault=SimpleNamespace(path=configured_vault),
        ),
    )
    monkeypatch.setattr("cortex.cron._build_job", fake_build_job)
    monkeypatch.setattr("cortex.cron._load_jobs", lambda: {"jobs": []})
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: None)

    result = install(vault_path=str(configured_vault))

    assert result["action"] == "created"
    assert captured["cortex_bin"] == "hermes cortex"
    assert captured["cortex_repo"]
    assert not captured["cortex_bin"].startswith("/private/dev/hermes-cortex")
    assert captured["cron_config"].deliver == "origin"


def test_install_updates_legacy_job_and_removes_duplicate(monkeypatch):
    saved = {}
    legacy = {"id": "legacy-id", "name": "session-knowledge-promotion", "created_at": "old"}
    duplicate = {"id": _job_id("hermes-cortex-nightly-promotion"), "name": "hermes-cortex-nightly-promotion"}

    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(nightly_promotion=CronNightlyPromotionConfig(enabled=True)),
            vault=SimpleNamespace(path=Path("/cfg-vault")),
        ),
    )
    monkeypatch.setattr("cortex.cron._load_jobs", lambda: {"jobs": [legacy, duplicate]})
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.update(data))

    result = install(vault_path=str(Path("/cfg-vault").resolve()))

    assert result["action"] == "updated"
    assert result["removed_duplicates"] == 1
    assert len(saved["jobs"]) == 1
    assert saved["jobs"][0]["name"] == "hermes-cortex-nightly-promotion"
    assert saved["jobs"][0]["created_at"] == "old"


def test_status_reports_duplicates(monkeypatch):
    cfg = CronNightlyPromotionConfig(name="custom-nightly", schedule="5 5 * * *")
    monkeypatch.setattr("cortex.cron._load_cortex_config", lambda config_path=None: SimpleNamespace(cron=SimpleNamespace(nightly_promotion=cfg)))
    monkeypatch.setattr(
        "cortex.cron._load_jobs",
        lambda: {
            "jobs": [
                {"id": _job_id("custom-nightly"), "name": "custom-nightly", "schedule_display": "5 5 * * *", "enabled": True, "state": "scheduled", "deliver": "origin"},
                {"id": _job_id("session-knowledge-promotion"), "name": "session-knowledge-promotion"},
            ]
        },
    )

    result = status()

    assert result["installed"] is True
    assert result["name"] == "custom-nightly"
    assert result["configured_schedule"] == "5 5 * * *"
    assert result["duplicates"] == 1


def test_cron_status_output_does_not_claim_daily_schedule(monkeypatch, capsys):
    from cortex import cli

    monkeypatch.setattr(
        "cortex.cron.status",
        lambda config_path=None, job="nightly": {
            "installed": True,
            "name": "custom-nightly",
            "job_id": "custom-id",
            "schedule": "*/30 * * * *",
            "configured_schedule": "*/30 * * * *",
            "deliver": "origin",
            "enabled_toolsets": ["file", "terminal"],
            "enabled": True,
            "configured_enabled": True,
            "state": "scheduled",
            "duplicates": 0,
            "last_run": None,
            "last_status": None,
        },
    )

    assert cli._cmd_cron_status(Namespace(config=None, job="nightly")) == 0
    output = capsys.readouterr().out
    assert "Schedule:   */30 * * * *" in output
    assert "(daily)" not in output


def test_cron_status_defaults_to_all_jobs(monkeypatch, capsys):
    from cortex import cli

    seen = {}

    def fake_status(config_path=None, job="nightly"):
        seen["job"] = job
        return {
            "action": "multiple",
            "jobs": [
                {
                    "job": "nightly",
                    "installed": True,
                    "name": "nightly-job",
                    "job_id": "nightly-id",
                    "schedule": "0 2 * * *",
                    "configured_schedule": "0 2 * * *",
                    "deliver": "origin",
                    "enabled_toolsets": ["file", "terminal"],
                    "enabled": True,
                    "configured_enabled": True,
                    "state": "scheduled",
                    "duplicates": 0,
                    "last_run": None,
                    "last_status": None,
                },
                {
                    "job": "weekly",
                    "installed": True,
                    "name": "weekly-job",
                    "job_id": "weekly-id",
                    "schedule": "0 8 * * 1",
                    "configured_schedule": "0 8 * * 1",
                    "deliver": "origin",
                    "enabled_toolsets": ["terminal"],
                    "enabled": True,
                    "configured_enabled": True,
                    "state": "scheduled",
                    "duplicates": 0,
                    "last_run": None,
                    "last_status": None,
                },
            ],
        }

    monkeypatch.setattr("cortex.cron.status", fake_status)

    assert cli._cmd_cron_status(Namespace(config=None)) == 0
    output = capsys.readouterr().out
    assert seen["job"] == "all"
    assert "Job:        nightly" in output
    assert "Name:       nightly-job" in output
    assert "Job:        weekly" in output
    assert "Name:       weekly-job" in output


def test_uninstall_removes_configured_default_and_legacy(monkeypatch):
    saved = {}
    cfg = CronNightlyPromotionConfig(name="custom-nightly")
    keep = {"id": "other", "name": "other"}
    jobs = [
        {"id": _job_id("custom-nightly"), "name": "custom-nightly"},
        {"id": _job_id("hermes-cortex-nightly-promotion"), "name": "hermes-cortex-nightly-promotion"},
        {"id": _job_id("session-knowledge-promotion"), "name": "session-knowledge-promotion"},
        keep,
    ]

    monkeypatch.setattr("cortex.cron._load_cortex_config", lambda config_path=None: SimpleNamespace(cron=SimpleNamespace(nightly_promotion=cfg)))
    monkeypatch.setattr("cortex.cron._load_jobs", lambda: {"jobs": jobs.copy()})
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.update(data))

    result = uninstall()

    assert result["action"] == "removed"
    assert result["removed_count"] == 3
    assert saved["jobs"] == [keep]


def test_install_disabled_config_skips_creation(monkeypatch):
    saved = []
    cfg = CronNightlyPromotionConfig(enabled=False)
    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(nightly_promotion=cfg),
            vault=SimpleNamespace(path=Path("/cfg-vault")),
        ),
    )
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.append(data))

    result = install(vault_path=str(Path("/cfg-vault").resolve()))

    assert result["action"] == "disabled"
    assert result["installed"] is False
    assert saved == []


def test_weekly_lifecycle_command_respects_dry_run_true_false():
    cfg = CronWeeklyReviewConfig(dry_run=True)
    command = _weekly_lifecycle_command("hermes cortex", cfg)
    assert command == (
        "hermes cortex lifecycle weekly --dry-run --stale-days 180 "
        "--stale-min-importance 4.0 --consolidation-min-degree 3"
    )

    no_dry_run = _weekly_lifecycle_command("hermes cortex", CronWeeklyReviewConfig(dry_run=False))
    assert "--dry-run" not in no_dry_run
    assert no_dry_run.startswith("hermes cortex lifecycle weekly --stale-days")


def test_build_weekly_job_uses_default_public_safe_config():
    job = _build_weekly_job("/vault", "/repo", "hermes cortex")

    assert job["id"] == _job_id("hermes-cortex-weekly-review")
    assert job["name"] == "hermes-cortex-weekly-review"
    assert job["schedule_display"] == "0 8 * * 1"
    assert job["deliver"] == "origin"
    assert job["metadata"]["cortex"]["job_type"] == "weekly_review"
    assert job["metadata"]["cortex"]["output_format"] == "markdown"
    assert job["metadata"]["cortex"]["read_only"] is True
    assert "hermes cortex lifecycle weekly --dry-run" in job["prompt"]


def test_build_weekly_job_uses_custom_config_values():
    cfg = CronWeeklyReviewConfig(
        name="custom-weekly",
        schedule="30 7 * * 1",
        timezone="UTC",
        deliver="origin",
        output_format="markdown",
        dry_run=False,
        stale_days=90,
        stale_min_importance=3.5,
        consolidation_min_degree=5,
    )

    job = _build_weekly_job("/vault", "/repo", "hermes cortex", cfg)

    assert job["id"] == _job_id("custom-weekly")
    assert job["schedule_display"] == "30 7 * * 1"
    assert "--dry-run" not in job["metadata"]["cortex"]["command"]
    assert "--stale-days 90" in job["prompt"]
    assert job["metadata"]["cortex"]["timezone"] == "UTC"
    assert job["metadata"]["cortex"]["stale_min_importance"] == 3.5
    assert job["metadata"]["cortex"]["consolidation_min_degree"] == 5


def test_install_weekly_uses_weekly_config(monkeypatch, tmp_path):
    captured = {}
    weekly_cfg = CronWeeklyReviewConfig(enabled=True, name="custom-weekly", dry_run=False)

    def fake_build_weekly_job(vault_path, cortex_repo, cortex_bin, cron_config=None):
        captured["vault_path"] = str(vault_path)
        captured["cortex_bin"] = str(cortex_bin)
        captured["cron_config"] = cron_config
        return {
            "id": "weekly-job",
            "name": "custom-weekly",
            "created_at": "now",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_delivery_error": None,
            "repeat": {"times": None, "completed": 0},
        }

    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(
                nightly_promotion=CronNightlyPromotionConfig(),
                weekly_review=weekly_cfg,
            ),
            vault=SimpleNamespace(path=tmp_path / "cfg-vault"),
        ),
    )
    monkeypatch.setattr("cortex.cron._build_weekly_job", fake_build_weekly_job)
    monkeypatch.setattr("cortex.cron._load_jobs", lambda: {"jobs": []})
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: None)

    result = install(vault_path=str(tmp_path / "cfg-vault"), job="weekly")

    assert result["job"] == "weekly"
    assert result["action"] == "created"
    assert captured["cortex_bin"] == "hermes cortex"
    assert captured["cron_config"] is weekly_cfg


def test_weekly_status_does_not_collide_with_nightly(monkeypatch):
    weekly_cfg = CronWeeklyReviewConfig(name="custom-weekly", schedule="0 8 * * 1")
    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(
                nightly_promotion=CronNightlyPromotionConfig(name="custom-nightly"),
                weekly_review=weekly_cfg,
            )
        ),
    )
    monkeypatch.setattr(
        "cortex.cron._load_jobs",
        lambda: {
            "jobs": [
                {"id": _job_id("custom-nightly"), "name": "custom-nightly"},
                {
                    "id": _job_id("custom-weekly"),
                    "name": "custom-weekly",
                    "schedule_display": "0 8 * * 1",
                    "enabled": True,
                    "state": "scheduled",
                    "deliver": "origin",
                },
            ]
        },
    )

    result = status(job="weekly")

    assert result["installed"] is True
    assert result["job"] == "weekly"
    assert result["job_id"] == _job_id("custom-weekly")
    assert result["name"] == "custom-weekly"


def test_install_nightly_rejects_mismatching_vault(monkeypatch, tmp_path):
    """cron install --vault MISMATCH must fail without saving jobs."""
    from cortex.cron import VaultMismatchError

    configured = tmp_path / "configured-vault"
    other = tmp_path / "other-vault"
    saved = []

    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(nightly_promotion=CronNightlyPromotionConfig(enabled=True)),
            vault=SimpleNamespace(path=configured),
        ),
    )
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.append(data))

    import pytest as _pytest

    with _pytest.raises(VaultMismatchError):
        install(vault_path=str(other), job="nightly")
    assert saved == []


def test_install_weekly_rejects_mismatching_vault(monkeypatch, tmp_path):
    """cron install --job weekly --vault MISMATCH must fail without saving jobs."""
    from cortex.cron import VaultMismatchError

    configured = tmp_path / "configured-vault"
    other = tmp_path / "other-vault"
    saved = []

    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(
                nightly_promotion=CronNightlyPromotionConfig(),
                weekly_review=CronWeeklyReviewConfig(enabled=True),
            ),
            vault=SimpleNamespace(path=configured),
        ),
    )
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.append(data))

    import pytest as _pytest

    with _pytest.raises(VaultMismatchError):
        install(vault_path=str(other), job="weekly")
    assert saved == []


def test_install_nightly_accepts_matching_vault(monkeypatch, tmp_path):
    """cron install --vault MATCH (normalized == configured) succeeds."""
    configured = tmp_path / "configured-vault"
    saved = []

    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(nightly_promotion=CronNightlyPromotionConfig(enabled=True)),
            vault=SimpleNamespace(path=configured),
        ),
    )
    monkeypatch.setattr("cortex.cron._load_jobs", lambda: {"jobs": []})
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.append(data))

    result = install(vault_path=str(configured), job="nightly")

    assert result["action"] == "created"
    assert len(saved) == 1


def test_install_nightly_accepts_no_vault_override(monkeypatch, tmp_path):
    """cron install without --vault succeeds normally."""
    configured = tmp_path / "configured-vault"
    saved = []

    monkeypatch.setattr(
        "cortex.cron._load_cortex_config",
        lambda config_path=None: SimpleNamespace(
            cron=SimpleNamespace(nightly_promotion=CronNightlyPromotionConfig(enabled=True)),
            vault=SimpleNamespace(path=configured),
        ),
    )
    monkeypatch.setattr("cortex.cron._load_jobs", lambda: {"jobs": []})
    monkeypatch.setattr("cortex.cron._save_jobs", lambda data: saved.append(data))

    result = install(job="nightly")

    assert result["action"] == "created"
    assert len(saved) == 1
