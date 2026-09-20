"""Regression tests for safe Cortex installation into Hermes' shared venv."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cortex import shared_venv
from cortex.runtime_health import HealthCheck
from cortex.shared_venv import build_constraints, check_critical_imports


def _write_lock(path: Path, packages: list[tuple[str, str]]) -> Path:
    blocks = [f'[[package]]\nname = "{name}"\nversion = "{version}"\n' for name, version in packages]
    path.write_text("version = 1\nrevision = 3\n\n" + "\n".join(blocks), encoding="utf-8")
    return path


def test_build_constraints_preserves_installed_host_versions_and_locks_missing_packages(
    tmp_path: Path,
) -> None:
    lock = _write_lock(
        tmp_path / "uv.lock",
        [
            ("host-owned", "2.0.0"),
            ("plugin-only", "3.1.4"),
        ],
    )

    constraints = build_constraints(
        lock,
        installed={
            "host_owned": "1.9.0",
            "unrelated.host": "7.0.0",
        },
    )

    assert constraints == [
        "host-owned==1.9.0",
        "plugin-only==3.1.4",
        "unrelated-host==7.0.0",
    ]


def test_build_constraints_rejects_ambiguous_lock_versions(tmp_path: Path) -> None:
    lock = _write_lock(
        tmp_path / "uv.lock",
        [
            ("duplicate_name", "1.0.0"),
            ("duplicate-name", "2.0.0"),
        ],
    )

    with pytest.raises(ValueError, match="multiple locked versions.*duplicate-name"):
        build_constraints(lock, installed={})


def test_build_constraints_does_not_pin_the_installed_cortex_version(
    tmp_path: Path,
) -> None:
    lock = _write_lock(
        tmp_path / "uv.lock",
        [
            ("hermes-cortex", "0.2.0"),
            ("host-owned", "2.0.0"),
        ],
    )

    constraints = build_constraints(
        lock,
        installed={
            "hermes-cortex": "0.1.0",
            "host-owned": "1.9.0",
        },
    )

    assert constraints == [
        "hermes-cortex==0.2.0",
        "host-owned==1.9.0",
    ]


def test_shared_venv_install_path_is_constrained_and_fail_closed() -> None:
    script = (Path(__file__).parents[1] / "install.sh").read_text(encoding="utf-8")

    assert "-m cortex.shared_venv constraints" in script
    assert '--constraints "$SHARED_VENV_CONSTRAINTS"' in script
    assert script.count("-m cortex.shared_venv dependency-check") == 2
    assert "-m cortex.shared_venv import-check" in script


def test_constraints_cli_writes_host_preserving_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _write_lock(tmp_path / "uv.lock", [("host-owned", "2.0.0")])
    output = tmp_path / "constraints.txt"
    monkeypatch.setattr(shared_venv, "installed_versions", lambda: {"host-owned": "1.9.0"})

    rc = shared_venv.main(
        ["constraints", "--lock", str(lock), "--output", str(output)]
    )

    assert rc == 0
    assert output.read_text(encoding="utf-8") == "host-owned==1.9.0\n"


def test_critical_import_check_names_the_broken_module() -> None:
    def broken_import(name: str):
        if name == "chromadb":
            raise ModuleNotFoundError("missing coordinated dependency")
        return object()

    with pytest.raises(
        RuntimeError,
        match="chromadb: ModuleNotFoundError: missing coordinated dependency",
    ):
        check_critical_imports(import_module=broken_import)


def test_critical_import_check_accepts_healthy_stack() -> None:
    assert check_critical_imports(import_module=lambda _name: object()) == (
        "chromadb",
        "sentence_transformers",
    )


def test_dependency_check_cli_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        shared_venv,
        "pip_check_health",
        lambda: HealthCheck(ok=False, detail="broken shared environment"),
    )

    rc = shared_venv.main(["dependency-check"])

    assert rc == 1
    assert "broken shared environment" in capsys.readouterr().err


def test_plugin_manifest_bounds_the_verified_hermes_compatibility_line() -> None:
    manifest_path = Path(__file__).parents[1] / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert manifest["requires_hermes"] == ">=0.17,<0.22"
