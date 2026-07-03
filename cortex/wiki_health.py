"""Read-only health checks for the Cortex/llm-wiki Vault contract."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cortex.config import Config
from cortex.indexer import _normalize_newlines, parse_frontmatter
from cortex.installer import RAW_FOLDERS, VAULT_FOLDERS

ROOT_FILES = ["SCHEMA.md", "index.md", "log.md"]
CURATED_EXCLUSION_FOLDERS = ["raw", "00_inbox", "80_templates"]
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class WikiHealthIssue:
    """A deterministic health finding."""

    severity: str
    code: str
    path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass(frozen=True)
class WikiHealthReport:
    """Full read-only wiki-health report."""

    config_path: str
    vault_path: str
    issues: tuple[WikiHealthIssue, ...]

    @property
    def errors(self) -> tuple[WikiHealthIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[WikiHealthIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": self.config_path,
            "vault_path": self.vault_path,
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issue_count": len(self.issues),
            "issues": [i.to_dict() for i in self.issues],
        }


def check_wiki_health(cfg: Config) -> WikiHealthReport:
    """Return a deterministic, read-only health report for the configured Vault."""

    vault = cfg.vault.path
    config_path = str(cfg.source_path or "(unknown)")
    issues: list[WikiHealthIssue] = []

    if not vault.exists():
        issues.append(
            WikiHealthIssue(
                "error",
                "vault_missing",
                ".",
                f"Configured vault path does not exist: {vault}",
            )
        )
        return WikiHealthReport(config_path, str(vault), tuple(issues))
    if not vault.is_dir():
        issues.append(
            WikiHealthIssue(
                "error",
                "vault_not_directory",
                ".",
                f"Configured vault path is not a directory: {vault}",
            )
        )
        return WikiHealthReport(config_path, str(vault), tuple(issues))

    issues.extend(_missing_root_file_issues(vault))
    issues.extend(_missing_folder_issues(vault, RAW_FOLDERS[1:], "missing_raw_folder"))
    issues.extend(_missing_folder_issues(vault, VAULT_FOLDERS, "missing_cortex_folder"))
    issues.extend(_config_drift_issues(cfg))
    issues.extend(_raw_source_issues(vault))

    issues.sort(key=lambda i: (i.severity, i.code, i.path, i.message))
    return WikiHealthReport(config_path, str(vault), tuple(issues))


def _missing_root_file_issues(vault: Path) -> list[WikiHealthIssue]:
    return [
        WikiHealthIssue("error", "missing_root_file", rel, f"Missing root file: {rel}")
        for rel in ROOT_FILES
        if not (vault / rel).is_file()
    ]


def _missing_folder_issues(vault: Path, folders: list[str], code: str) -> list[WikiHealthIssue]:
    issues: list[WikiHealthIssue] = []
    for rel in folders:
        path = vault / rel
        if not path.is_dir():
            issues.append(WikiHealthIssue("error", code, rel, f"Missing folder: {rel}"))
    return issues


def _config_drift_issues(cfg: Config) -> list[WikiHealthIssue]:
    include = set(cfg.vault.include_folders)
    exclude = set(cfg.vault.exclude_folders)
    issues: list[WikiHealthIssue] = []
    for folder in CURATED_EXCLUSION_FOLDERS:
        explicitly_included = folder in include
        indexable_by_empty_include = not include and folder not in exclude
        indexable_by_include = explicitly_included and folder not in exclude
        if indexable_by_empty_include or indexable_by_include:
            issues.append(
                WikiHealthIssue(
                    "error",
                    "config_curated_source_drift",
                    folder,
                    f"{folder} would be part of the default curated corpus; exclude it unless explicitly requested",
                    {
                        "include_folders": sorted(include),
                        "exclude_folders": sorted(exclude),
                        "reason": "include_folders empty" if indexable_by_empty_include else "explicit include without exclude",
                    },
                )
            )
    return issues


def _raw_source_issues(vault: Path) -> list[WikiHealthIssue]:
    raw = vault / "raw"
    if not raw.is_dir():
        return []

    issues: list[WikiHealthIssue] = []
    for path in sorted(raw.rglob("*.md"), key=lambda p: p.relative_to(vault).as_posix()):
        rel = path.relative_to(vault).as_posix()
        if rel == "raw/README.md":
            continue
        try:
            text = _normalize_newlines(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            issues.append(
                WikiHealthIssue(
                    "error",
                    "raw_source_decode_error",
                    rel,
                    "Raw source Markdown is not valid UTF-8",
                    {"error": str(exc)},
                )
            )
            continue
        except OSError as exc:
            issues.append(
                WikiHealthIssue(
                    "error",
                    "raw_source_read_error",
                    rel,
                    "Raw source Markdown could not be read",
                    {"error": str(exc)},
                )
            )
            continue

        fm, body = parse_frontmatter(text)
        if not fm:
            issues.append(
                WikiHealthIssue(
                    "warning",
                    "raw_source_missing_frontmatter",
                    rel,
                    "Raw source Markdown has no parseable frontmatter",
                )
            )
            continue

        issues.extend(_raw_metadata_issues(rel, fm))
        sha = fm.get("sha256")
        if isinstance(sha, str) and _SHA256_RE.fullmatch(sha.strip()):
            body_for_hash = body[1:] if body.startswith("\n") else body
            body_sha = hashlib.sha256(body_for_hash.encode("utf-8")).hexdigest()
            if body_sha.lower() != sha.strip().lower():
                issues.append(
                    WikiHealthIssue(
                        "error",
                        "raw_source_sha256_drift",
                        rel,
                        "Raw source body sha256 does not match frontmatter sha256",
                        {"expected": sha.strip().lower(), "actual": body_sha},
                    )
                )
    return issues


def _raw_metadata_issues(rel: str, fm: dict[str, Any]) -> list[WikiHealthIssue]:
    issues: list[WikiHealthIssue] = []

    source_url = fm.get("source_url")
    if source_url is None or source_url == "":
        issues.append(
            WikiHealthIssue(
                "warning",
                "raw_source_missing_source_url",
                rel,
                "Raw source frontmatter has no source_url",
            )
        )
    elif not _looks_like_url(str(source_url)):
        issues.append(
            WikiHealthIssue(
                "warning",
                "raw_source_invalid_source_url",
                rel,
                "Raw source frontmatter source_url is not a parseable URL",
                {"source_url": str(source_url)},
            )
        )

    ingested = fm.get("ingested")
    if ingested is None or ingested == "":
        issues.append(
            WikiHealthIssue(
                "warning",
                "raw_source_missing_ingested",
                rel,
                "Raw source frontmatter has no ingested timestamp/date",
            )
        )
    elif not _parseable_date_or_datetime(ingested):
        issues.append(
            WikiHealthIssue(
                "warning",
                "raw_source_invalid_ingested",
                rel,
                "Raw source frontmatter ingested is not parseable as ISO date/datetime",
                {"ingested": str(ingested)},
            )
        )

    sha = fm.get("sha256")
    if sha is None or sha == "":
        issues.append(
            WikiHealthIssue(
                "warning",
                "raw_source_missing_sha256",
                rel,
                "Raw source frontmatter has no sha256",
            )
        )
    elif not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha.strip()):
        issues.append(
            WikiHealthIssue(
                "warning",
                "raw_source_invalid_sha256",
                rel,
                "Raw source frontmatter sha256 is not 64 hex characters",
                {"sha256": str(sha)},
            )
        )
    return issues


def _looks_like_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def _parseable_date_or_datetime(value: Any) -> bool:
    if isinstance(value, datetime | date):
        return True
    raw = str(value).strip()
    if not raw:
        return False
    try:
        datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    try:
        date.fromisoformat(raw)
        return True
    except ValueError:
        return False
