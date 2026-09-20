"""Fail-closed helpers for installing Cortex into Hermes' shared Python venv."""

from __future__ import annotations

import argparse
import importlib
import re
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import distributions
from pathlib import Path
from typing import Any

from cortex.runtime_health import pip_check_health

_CRITICAL_IMPORTS = ("chromadb", "sentence_transformers")
_TARGET_DISTRIBUTION = "hermes-cortex"


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _unique_versions(entries: Sequence[tuple[str, str]], *, source: str) -> dict[str, str]:
    versions: dict[str, set[str]] = {}
    for raw_name, raw_version in entries:
        name = _canonical_name(raw_name)
        versions.setdefault(name, set()).add(raw_version)

    ambiguous = {
        name: sorted(values)
        for name, values in versions.items()
        if len(values) != 1
    }
    if ambiguous:
        details = ", ".join(
            f"{name}={','.join(values)}" for name, values in sorted(ambiguous.items())
        )
        raise ValueError(f"{source} contains multiple locked versions: {details}")
    return {name: next(iter(values)) for name, values in versions.items()}


def locked_versions(lock_path: Path) -> dict[str, str]:
    """Return one canonical package version per entry in a uv lockfile."""
    with lock_path.open("rb") as handle:
        payload = tomllib.load(handle)
    packages = payload.get("package")
    if not isinstance(packages, list):
        raise ValueError(f"uv lockfile has no package list: {lock_path}")

    entries: list[tuple[str, str]] = []
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError(f"uv lockfile contains an invalid package entry: {lock_path}")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError(f"uv lockfile package lacks name/version: {lock_path}")
        entries.append((name, version))
    return _unique_versions(entries, source=str(lock_path))


def installed_versions() -> dict[str, str]:
    """Read the distributions already owned by the current host environment."""
    entries: list[tuple[str, str]] = []
    for dist in distributions():
        name = dist.metadata.get("Name")
        if name:
            entries.append((name, dist.version))
    return _unique_versions(entries, source="installed environment")


def build_constraints(
    lock_path: Path,
    *,
    installed: Mapping[str, str] | None = None,
) -> list[str]:
    """Merge Cortex's lock with host versions, with the host winning overlaps."""
    merged = locked_versions(lock_path)
    host = installed_versions() if installed is None else dict(installed)
    for raw_name, version in host.items():
        name = _canonical_name(raw_name)
        if name == _TARGET_DISTRIBUTION:
            continue
        merged[name] = version
    return [f"{name}=={merged[name]}" for name in sorted(merged)]


def check_critical_imports(
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> tuple[str, ...]:
    """Import the embedding stack and name every broken module in one error."""
    failures: list[str] = []
    for module_name in _CRITICAL_IMPORTS:
        try:
            import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - report third-party import failures verbatim
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError("critical import check failed: " + "; ".join(failures))
    return _CRITICAL_IMPORTS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    constraints = subparsers.add_parser(
        "constraints",
        help="write host-preserving constraints for a shared Hermes venv install",
    )
    constraints.add_argument("--lock", required=True, type=Path)
    constraints.add_argument("--output", required=True, type=Path)

    subparsers.add_parser(
        "import-check",
        help="verify the critical Cortex embedding imports",
    )
    subparsers.add_parser(
        "dependency-check",
        help="verify that installed distributions have compatible requirements",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "constraints":
            rows = build_constraints(args.lock)
            args.output.write_text("\n".join(rows) + "\n", encoding="utf-8")
            print(f"Wrote {len(rows)} shared-venv constraints to {args.output}")
            return 0
        if args.command == "dependency-check":
            result = pip_check_health()
            if not result.ok:
                print(f"Shared-venv dependency check failed: {result.detail}", file=sys.stderr)
                return 1
            print(f"Shared-venv dependency check: ok — {result.detail}")
            return 0
        modules = check_critical_imports()
        print("Critical imports: ok (" + ", ".join(modules) + ")")
        return 0
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"Shared-venv check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
