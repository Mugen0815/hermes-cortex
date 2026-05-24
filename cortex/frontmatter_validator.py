"""Frontmatter validation for vault notes.

This module powers the ``cortex validate-frontmatter`` CLI.  It deliberately
reuses ``cortex.frontmatter`` for field vocabulary and normalization warnings,
while keeping validation read-only: no note content is rewritten here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from cortex.config import Config
from cortex.frontmatter import missing_required, normalize
from cortex.indexer import _ALWAYS_SKIP_DIRS, _normalize_newlines, iter_vault_files

_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
SCHEMA_VERSION = 1


@dataclass
class FrontmatterIssue:
    """One validation issue for a markdown note."""

    severity: str  # "error" | "warning"
    code: str
    message: str
    field: str | None = None


@dataclass
class FrontmatterFileResult:
    """Validation result for one markdown note."""

    file: str
    path: str
    issues: list[FrontmatterIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")

    @property
    def ok(self) -> bool:
        return self.error_count == 0 and self.warning_count == 0


@dataclass
class FrontmatterValidationReport:
    """Aggregate validation report for CLI and automation consumers."""

    vault_path: str
    files: list[FrontmatterFileResult] = field(default_factory=list)

    @property
    def checked_count(self) -> int:
        return len(self.files)

    @property
    def error_count(self) -> int:
        return sum(result.error_count for result in self.files)

    @property
    def warning_count(self) -> int:
        return sum(result.warning_count for result in self.files)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "vault_path": self.vault_path,
            "checked_count": self.checked_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "files": [
                {
                    "file": result.file,
                    "path": result.path,
                    "error_count": result.error_count,
                    "warning_count": result.warning_count,
                    "issues": [asdict(issue) for issue in result.issues],
                }
                for result in self.files
            ],
        }


def _display_file(path: Path, vault_root: Path) -> str:
    try:
        return path.relative_to(vault_root).as_posix()
    except ValueError:
        return str(path)


def _parse_frontmatter_strict(text: str) -> tuple[dict[str, Any] | None, FrontmatterIssue | None]:
    """Parse YAML frontmatter and preserve parse failures as validation errors."""
    text = _normalize_newlines(text)
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, FrontmatterIssue(
            severity="error",
            code="missing_frontmatter",
            message="missing YAML frontmatter block",
        )
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        return None, FrontmatterIssue(
            severity="error",
            code="yaml_parse_error",
            message=str(exc),
        )
    if not isinstance(parsed, dict):
        return None, FrontmatterIssue(
            severity="error",
            code="frontmatter_not_mapping",
            message="frontmatter must be a YAML mapping/object",
        )
    return parsed, None


def validate_frontmatter_file(path: Path, vault_root: Path) -> FrontmatterFileResult:
    """Validate one markdown note without mutating it."""
    path = path.expanduser().resolve()
    result = FrontmatterFileResult(
        file=_display_file(path, vault_root),
        path=str(path),
    )
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.issues.append(
            FrontmatterIssue(
                severity="error",
                code="read_error",
                message=str(exc),
            )
        )
        return result

    fm, parse_issue = _parse_frontmatter_strict(raw_text)
    if parse_issue is not None:
        result.issues.append(parse_issue)
        return result

    for field_name in missing_required(fm):
        result.issues.append(
            FrontmatterIssue(
                severity="error",
                code="missing_required",
                field=field_name,
                message=f"missing required frontmatter field: {field_name}",
            )
        )

    # Domain is mandatory by current Vault policy, but adding it to REQUIRED_FIELDS
    # would immediately change indexer behavior across the live vault.  Keep this
    # as a warning for this validator slice so strict mode can fail it explicitly.
    if fm is not None and not str(fm.get("domain") or "").strip():
        result.issues.append(
            FrontmatterIssue(
                severity="warning",
                code="missing_domain",
                field="domain",
                message="missing frontmatter field required by Vault policy: domain",
            )
        )

    norm = normalize(fm)
    for warning in norm.warnings:
        result.issues.append(
            FrontmatterIssue(
                severity="warning",
                code="normalization_warning",
                message=warning,
            )
        )

    return result


def _is_valid_vault_note(path: Path, vault_root: Path) -> bool:
    """Return whether a path matches the default vault traversal note rules."""
    try:
        rel = path.relative_to(vault_root).as_posix()
    except ValueError:
        return False
    if rel == "README.md":
        return False
    parts = rel.split("/")
    if any(part.startswith(".") for part in parts):
        return False
    if any(part in _ALWAYS_SKIP_DIRS for part in parts[:-1]):
        return False
    return path.is_file() and path.suffix == ".md"


def _resolve_vault_path(raw: str, vault_root: Path) -> Path:
    """Resolve an explicit path and reject paths outside the configured vault."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = vault_root / p
    p = p.resolve()
    try:
        p.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError(f"explicit path escapes configured vault: {raw}") from exc
    return p


def _iter_explicit_paths(paths: Iterable[str], cfg: Config) -> list[Path]:
    """Resolve explicit note or directory paths, relative to vault when needed.

    Directory paths use the same configured vault traversal policy as indexing.
    Direct file paths intentionally remain exact targets: a caller may validate a
    single note in an otherwise excluded folder as long as it stays inside the
    vault and matches the default note rules.
    """
    vault_root = cfg.vault.path.resolve()
    traversable_notes = list(iter_vault_files(cfg))
    resolved: list[Path] = []
    for raw in paths:
        p = _resolve_vault_path(raw, vault_root)
        if p.is_dir():
            resolved.extend(note for note in traversable_notes if note.is_relative_to(p))
        elif _is_valid_vault_note(p, vault_root):
            resolved.append(p)
        else:
            # Preserve explicit nonexistent/read-error paths so the validator can
            # return a normal per-file error instead of silently hiding typos.
            resolved.append(p)
    # Dedupe preserving order.
    out: list[Path] = []
    seen: set[Path] = set()
    for p in resolved:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def collect_frontmatter_files(cfg: Config, paths: Iterable[str] | None = None) -> list[Path]:
    """Collect vault markdown files, optionally constrained by explicit paths."""
    explicit = list(paths or [])
    if explicit:
        return _iter_explicit_paths(explicit, cfg)
    return list(iter_vault_files(cfg))


def validate_frontmatter(cfg: Config, paths: Iterable[str] | None = None) -> FrontmatterValidationReport:
    """Validate configured vault notes or explicit paths."""
    vault_root = cfg.vault.path.resolve()
    report = FrontmatterValidationReport(vault_path=str(vault_root))
    for note_path in collect_frontmatter_files(cfg, paths):
        report.files.append(validate_frontmatter_file(note_path, vault_root))
    return report
