"""Cron job management for hermes-cortex — install/manage nightly promotion."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from cortex.config import CronNightlyPromotionConfig, CronWeeklyReviewConfig, load_config

log = logging.getLogger("cortex.cron")

# ---------------------------------------------------------------------------
# Paths — profile-aware via hermes_constants if available
# ---------------------------------------------------------------------------

try:
    from hermes_constants import get_hermes_home as _get_hermes_home

    _HERMES_HOME = Path(_get_hermes_home()).expanduser()
except ImportError:
    _HERMES_HOME = Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    ).expanduser()
_CRON_DIR = _HERMES_HOME / "cron"
_JOBS_FILE = _CRON_DIR / "jobs.json"

# ---------------------------------------------------------------------------
# Job identity
# ---------------------------------------------------------------------------

_DEFAULT_JOB_NAME = "hermes-cortex-nightly-promotion"
_DEFAULT_WEEKLY_JOB_NAME = "hermes-cortex-weekly-review"
_OLD_JOB_NAMES = {"session-knowledge-promotion"}
JobSelector = Literal["nightly", "weekly", "all"]


def _job_id(name: str = _DEFAULT_JOB_NAME) -> str:
    """Deterministic ID based on the job name so re-installs don't pile up."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Prompt templates — packaged as resources, not embedded in code
# ---------------------------------------------------------------------------


def _load_prompt_template(filename: str) -> str:
    from importlib import resources

    return resources.files("cortex").joinpath("cron_templates", filename).read_text(encoding="utf-8")


_NIGHTLY_PROMPT = _load_prompt_template("nightly_promotion.md")
_WEEKLY_PROMPT = _load_prompt_template("weekly_review.md")

# ---------------------------------------------------------------------------
# Job definition builder
# ---------------------------------------------------------------------------


def _session_globs_block(session_globs: list[str]) -> str:
    return "\n".join(f"   - `{glob}`" for glob in session_globs)


def _session_source_command(cfg: CronNightlyPromotionConfig) -> str:
    parts = [
        "hermes cortex session-sources",
        f"--lookback-days {cfg.lookback_days}",
        f"--timezone {shlex.quote(cfg.timezone)}",
        f"--state-db-path {shlex.quote(cfg.state_db_path)}",
    ]
    if not cfg.legacy_fallback_enabled:
        parts.append("--no-legacy-fallback")
    parts.extend(f"--session-glob {shlex.quote(glob)}" for glob in cfg.session_globs)
    return " \\\n  ".join(parts)


def _lifecycle_prefix(cortex_bin: str, config_path: str | Path | None = None) -> str:
    prefix = f"{cortex_bin} lifecycle"
    if config_path is not None:
        prefix += f" --config {shlex.quote(str(Path(config_path).expanduser().resolve()))}"
    return prefix


def _lifecycle_commands(
    cortex_bin: str,
    dry_run_first: bool,
    config_path: str | Path | None = None,
) -> str:
    prefix = _lifecycle_prefix(cortex_bin, config_path)
    commands = []
    if dry_run_first:
        commands.append(f"{prefix} nightly --dry-run")
    commands.extend([
        f"{prefix} nightly --write",
        f"{prefix} maintenance",
    ])
    return " && \\\n   ".join(commands)



def _weekly_lifecycle_command(
    cortex_bin: str,
    cfg: CronWeeklyReviewConfig,
    config_path: str | Path | None = None,
) -> str:
    parts = [
        _lifecycle_prefix(cortex_bin, config_path),
        "weekly",
    ]
    if cfg.dry_run:
        parts.append("--dry-run")
    parts.extend([
        "--stale-days",
        str(cfg.stale_days),
        "--stale-min-importance",
        str(cfg.stale_min_importance),
        "--consolidation-min-degree",
        str(cfg.consolidation_min_degree),
    ])
    return " ".join(parts)


def _build_weekly_job(
    vault_path: str,
    cortex_repo: str,
    cortex_bin: str,
    cron_config: CronWeeklyReviewConfig | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = cron_config or CronWeeklyReviewConfig()
    command = _weekly_lifecycle_command(cortex_bin, cfg, config_path)
    prompt = _WEEKLY_PROMPT.format(
        vault_path=str(vault_path),
        cortex_repo=str(cortex_repo),
        cortex_bin=str(cortex_bin),
        timezone=cfg.timezone,
        output_format=cfg.output_format,
        dry_run=cfg.dry_run,
        stale_days=cfg.stale_days,
        stale_min_importance=cfg.stale_min_importance,
        consolidation_min_degree=cfg.consolidation_min_degree,
        weekly_command=command,
    )
    job_id = _job_id(cfg.name)
    return {
        "id": job_id,
        "name": cfg.name,
        "prompt": prompt,
        "skills": [],
        "skill": None,
        "model": None,
        "provider": None,
        "base_url": None,
        "script": None,
        "context_from": None,
        "schedule": {"kind": "cron", "expr": cfg.schedule, "display": cfg.schedule},
        "schedule_display": cfg.schedule,
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": cfg.deliver,
        "origin": None,
        "enabled_toolsets": ["terminal"],
        "workdir": None,
        "metadata": {
            "managed_by": "hermes-cortex",
            "cortex": {
                "job_type": "weekly_review",
                "repo": str(cortex_repo),
                "config_path": str(config_path) if config_path is not None else None,
                "command": command,
                "timezone": cfg.timezone,
                "timezone_scope": "prompt/report only; schedule timezone follows Hermes scheduler runtime config",
                "output_format": cfg.output_format,
                "dry_run": cfg.dry_run,
                "stale_days": cfg.stale_days,
                "stale_min_importance": cfg.stale_min_importance,
                "consolidation_min_degree": cfg.consolidation_min_degree,
                "read_only": True,
            },
        },
    }


def _build_job(
    vault_path: str,
    cortex_repo: str,
    cortex_bin: str,
    cron_config: CronNightlyPromotionConfig | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = cron_config or CronNightlyPromotionConfig()
    prompt = _NIGHTLY_PROMPT.format(
        vault_path=str(vault_path),
        cortex_repo=str(cortex_repo),
        cortex_bin=str(cortex_bin),
        lookback_days=cfg.lookback_days,
        timezone=cfg.timezone,
        state_db_path=cfg.state_db_path,
        legacy_fallback_enabled=cfg.legacy_fallback_enabled,
        session_globs_block=_session_globs_block(cfg.session_globs),
        session_source_command=_session_source_command(cfg),
        lifecycle_commands=_lifecycle_commands(cortex_bin, cfg.dry_run_first, config_path),
    )
    job_id = _job_id(cfg.name)
    return {
        "id": job_id,
        "name": cfg.name,
        "prompt": prompt,
        "skills": [],
        "skill": None,
        "model": None,
        "provider": None,
        "base_url": None,
        "script": None,
        "context_from": None,
        "schedule": {"kind": "cron", "expr": cfg.schedule, "display": cfg.schedule},
        "schedule_display": cfg.schedule,
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "last_delivery_error": None,
        "deliver": cfg.deliver,
        "origin": None,
        "enabled_toolsets": list(cfg.enabled_toolsets),
        "workdir": None,
        "metadata": {
            "managed_by": "hermes-cortex",
            "cortex": {
                "repo": str(cortex_repo),
                "config_path": str(config_path) if config_path is not None else None,
                "lookback_days": cfg.lookback_days,
                "session_source": {
                    "state_db_path": cfg.state_db_path,
                    "legacy_fallback_enabled": cfg.legacy_fallback_enabled,
                    "diagnostics_expected": [
                        "source_backend_primary",
                        "state_db_path",
                        "state_db_schema_version",
                        "state_db_readable",
                        "sessions_seen_by_backend",
                        "sessions_selected",
                        "sessions_selected_by_source",
                        "messages_scanned",
                        "fallback_used",
                        "fallback_reason",
                        "ignored_files.request_dump",
                        "lookback_cutoff",
                        "timezone",
                    ],
                },
                "session_globs": list(cfg.session_globs),
                "dry_run_first": cfg.dry_run_first,
                "timezone": cfg.timezone,
                "timezone_scope": "prompt/lookback only; schedule timezone follows Hermes scheduler runtime config",
            },
        },
    }


# ---------------------------------------------------------------------------
# Read / write helpers
# ---------------------------------------------------------------------------


def _load_jobs() -> dict[str, Any]:
    if not _JOBS_FILE.exists():
        return {"jobs": [], "updated_at": datetime.now(timezone.utc).isoformat()}
    return json.loads(_JOBS_FILE.read_text(encoding="utf-8"))


def _save_jobs(data: dict[str, Any]) -> None:
    _CRON_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _JOBS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _known_job_names(cfg: CronNightlyPromotionConfig) -> set[str]:
    return {cfg.name, _DEFAULT_JOB_NAME} | _OLD_JOB_NAMES


def _known_job_ids(cfg: CronNightlyPromotionConfig) -> set[str]:
    return {_job_id(name) for name in _known_job_names(cfg)}


def _find_job_indices(jobs: list[dict[str, Any]], cfg: CronNightlyPromotionConfig) -> list[int]:
    known_names = _known_job_names(cfg)
    known_ids = _known_job_ids(cfg)
    return [
        i
        for i, job in enumerate(jobs)
        if job.get("id") in known_ids or job.get("name") in known_names
    ]


def _find_weekly_job_indices(jobs: list[dict[str, Any]], cfg: CronWeeklyReviewConfig) -> list[int]:
    known_names = {cfg.name, _DEFAULT_WEEKLY_JOB_NAME}
    known_ids = {_job_id(name) for name in known_names}
    return [
        i
        for i, job in enumerate(jobs)
        if job.get("id") in known_ids or job.get("name") in known_names
    ]


def _preserve_lifecycle_fields(new_job: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "created_at",
        "last_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "repeat",
        "next_run_at",
    ):
        if key in existing:
            new_job[key] = existing[key]
    return new_job


def _load_cortex_config(config_path: str | Path | None = None):
    return load_config(config_path)


def _cron_config(config_path: str | Path | None = None) -> CronNightlyPromotionConfig:
    return _load_cortex_config(config_path).cron.nightly_promotion


def _weekly_cron_config(config_path: str | Path | None = None) -> CronWeeklyReviewConfig:
    return _load_cortex_config(config_path).cron.weekly_review


def _normalize_job(job: str) -> JobSelector:
    if job not in {"nightly", "weekly", "all"}:
        raise ValueError("job must be one of: nightly, weekly, all")
    return job  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _install_nightly(
    cortex_cfg: Any,
    vault_path: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = cortex_cfg.cron.nightly_promotion
    job_id = _job_id(cfg.name)
    if not cfg.enabled:
        return {
            "job": "nightly",
            "action": "disabled",
            "installed": False,
            "job_id": job_id,
            "name": cfg.name,
            "reason": "cron.nightly_promotion.enabled is false",
        }

    vault = Path(vault_path) if vault_path else cortex_cfg.vault.path
    cortex_repo = Path(__file__).resolve().parents[1]
    cortex_bin = "hermes cortex"

    job = _build_job(str(vault), str(cortex_repo), cortex_bin, cfg, config_path)
    return _upsert_job(job, cfg, _find_job_indices, job_name="nightly")


def _install_weekly(
    cortex_cfg: Any,
    vault_path: str | None = None,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = cortex_cfg.cron.weekly_review
    job_id = _job_id(cfg.name)
    if not cfg.enabled:
        return {
            "job": "weekly",
            "action": "disabled",
            "installed": False,
            "job_id": job_id,
            "name": cfg.name,
            "reason": "cron.weekly_review.enabled is false",
        }

    vault = Path(vault_path) if vault_path else cortex_cfg.vault.path
    cortex_repo = Path(__file__).resolve().parents[1]
    cortex_bin = "hermes cortex"

    job = _build_weekly_job(str(vault), str(cortex_repo), cortex_bin, cfg, config_path)
    return _upsert_job(job, cfg, _find_weekly_job_indices, job_name="weekly")


def _upsert_job(job: dict[str, Any], cfg: Any, finder: Any, *, job_name: str) -> dict[str, Any]:
    data = _load_jobs()
    data.setdefault("jobs", [])
    indices = finder(data["jobs"], cfg)

    if indices:
        primary = indices[0]
        existing = data["jobs"][primary]
        data["jobs"][primary] = _preserve_lifecycle_fields(job, existing)
        for idx in reversed(indices[1:]):
            data["jobs"].pop(idx)
        action = "updated"
    else:
        data["jobs"].append(job)
        action = "created"

    _save_jobs(data)
    return {
        "job": job_name,
        "action": action,
        "job_id": job["id"],
        "name": cfg.name,
        "schedule": cfg.schedule,
        "deliver": cfg.deliver,
        "removed_duplicates": max(0, len(indices) - 1),
    }


class VaultMismatchError(ValueError):
    """Raised when ``--vault PATH`` does not match the configured ``vault.path``."""


def _resolve_vault_override(
    cortex_cfg: Any,
    vault_path: str | None,
) -> Path | None:
    """Resolve and validate an explicit cron ``--vault`` override.

    The single canonical Vault contract requires the lifecycle commands in cron
    prompts to operate on the same ``vault.path`` that config-based runtime uses.
    ``--vault PATH`` is accepted only as a deprecated/no-op compatibility input
    whose normalized value matches the configured ``vault.path``. A mismatch
    raises :class:`VaultMismatchError` *before* any job build/save so the user
    cannot silently install a job pointing at a different Vault.
    """
    if vault_path is None:
        return None
    supplied = Path(vault_path).expanduser().resolve()
    configured = Path(str(cortex_cfg.vault.path)).expanduser().resolve()
    if supplied != configured:
        raise VaultMismatchError(
            f"cron install --vault {vault_path} does not match configured vault.path "
            f"{configured}. The Cortex config vault.path is the single source of truth; "
            "mismatching --vault is rejected. Update the config file to change the Vault, "
            "or omit --vault to use the configured path."
        )
    return configured


def install(
    vault_path: str | None = None,
    config_path: str | Path | None = None,
    job: JobSelector = "nightly",
) -> dict[str, Any]:
    """Install (or update) configured cortex cron job(s). Defaults to NightlyPromotion."""
    selected = _normalize_job(job)
    cortex_cfg = _load_cortex_config(config_path)
    resolved_vault = _resolve_vault_override(cortex_cfg, vault_path)
    source_path = getattr(cortex_cfg, "source_path", None)
    resolved_config = (
        Path(source_path).expanduser().resolve()
        if source_path is not None
        else (Path(config_path).expanduser().resolve() if config_path is not None else None)
    )
    if selected == "nightly":
        return _install_nightly(
            cortex_cfg,
            str(resolved_vault) if resolved_vault else None,
            resolved_config,
        )
    if selected == "weekly":
        return _install_weekly(
            cortex_cfg,
            str(resolved_vault) if resolved_vault else None,
            resolved_config,
        )
    return {
        "action": "multiple",
        "jobs": [
            _install_nightly(
                cortex_cfg,
                str(resolved_vault) if resolved_vault else None,
                resolved_config,
            ),
            _install_weekly(
                cortex_cfg,
                str(resolved_vault) if resolved_vault else None,
                resolved_config,
            ),
        ],
    }


def _remove_job(cfg: Any, finder: Any, *, job_name: str) -> dict[str, Any]:
    job_id = _job_id(cfg.name)
    data = _load_jobs()
    data.setdefault("jobs", [])
    indices = finder(data["jobs"], cfg)

    if not indices:
        return {"job": job_name, "action": "not_found", "job_id": job_id, "name": cfg.name}

    removed = [data["jobs"][idx] for idx in indices]
    for idx in reversed(indices):
        data["jobs"].pop(idx)
    _save_jobs(data)
    return {
        "job": job_name,
        "action": "removed",
        "job_id": job_id,
        "name": cfg.name,
        "removed_names": [job.get("name") for job in removed],
        "removed_count": len(removed),
    }


def uninstall(config_path: str | Path | None = None, job: JobSelector = "nightly") -> dict[str, Any]:
    """Remove configured/default cortex cron job(s). Defaults to NightlyPromotion."""
    selected = _normalize_job(job)
    cortex_cfg = _load_cortex_config(config_path)
    if selected == "nightly":
        return _remove_job(cortex_cfg.cron.nightly_promotion, _find_job_indices, job_name="nightly")
    if selected == "weekly":
        return _remove_job(cortex_cfg.cron.weekly_review, _find_weekly_job_indices, job_name="weekly")
    return {
        "action": "multiple",
        "jobs": [
            _remove_job(cortex_cfg.cron.nightly_promotion, _find_job_indices, job_name="nightly"),
            _remove_job(cortex_cfg.cron.weekly_review, _find_weekly_job_indices, job_name="weekly"),
        ],
    }


def _job_status(cfg: Any, finder: Any, *, job_name: str) -> dict[str, Any]:
    job_id = _job_id(cfg.name)
    data = _load_jobs()
    data.setdefault("jobs", [])
    indices = finder(data["jobs"], cfg)

    if not indices:
        return {
            "job": job_name,
            "installed": False,
            "job_id": job_id,
            "name": cfg.name,
            "configured_enabled": cfg.enabled,
        }

    job = data["jobs"][indices[0]]
    return {
        "job": job_name,
        "installed": True,
        "job_id": job_id,
        "name": job.get("name"),
        "configured_name": cfg.name,
        "schedule": job.get("schedule_display"),
        "configured_schedule": cfg.schedule,
        "enabled": job.get("enabled", False),
        "configured_enabled": cfg.enabled,
        "state": job.get("state"),
        "deliver": job.get("deliver"),
        "enabled_toolsets": job.get("enabled_toolsets"),
        "last_run": job.get("last_run_at"),
        "last_status": job.get("last_status"),
        "duplicates": max(0, len(indices) - 1),
        "metadata": job.get("metadata"),
    }


def status(config_path: str | Path | None = None, job: JobSelector = "nightly") -> dict[str, Any]:
    """Return the current status of configured cortex cron job(s)."""
    selected = _normalize_job(job)
    cortex_cfg = _load_cortex_config(config_path)
    if selected == "nightly":
        return _job_status(cortex_cfg.cron.nightly_promotion, _find_job_indices, job_name="nightly")
    if selected == "weekly":
        return _job_status(cortex_cfg.cron.weekly_review, _find_weekly_job_indices, job_name="weekly")
    return {
        "action": "multiple",
        "jobs": [
            _job_status(cortex_cfg.cron.nightly_promotion, _find_job_indices, job_name="nightly"),
            _job_status(cortex_cfg.cron.weekly_review, _find_weekly_job_indices, job_name="weekly"),
        ],
    }
