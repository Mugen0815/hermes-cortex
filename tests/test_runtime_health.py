"""Tests for read-only runtime dependency health checks."""

from __future__ import annotations

import subprocess

from cortex.runtime_health import embedding_stack_health, pip_check_health


def test_embedding_stack_health_reports_transitive_import_failure() -> None:
    def broken_import(_name: str):
        raise ModuleNotFoundError("missing opentelemetry exporter helper")

    result = embedding_stack_health(import_module=broken_import)

    assert result.ok is False
    assert result.detail == "chromadb: ModuleNotFoundError: missing opentelemetry exporter helper"


def test_embedding_stack_health_reports_sentence_transformers_failure() -> None:
    def broken_import(name: str):
        if name == "sentence_transformers":
            raise ImportError("broken model stack")
        return object()

    result = embedding_stack_health(import_module=broken_import)

    assert result.ok is False
    assert result.detail == "sentence_transformers: ImportError: broken model stack"


def test_embedding_stack_health_reports_success() -> None:
    result = embedding_stack_health(import_module=lambda _name: object())

    assert result.ok is True
    assert result.detail == "chromadb and sentence_transformers imports ok"


def test_pip_check_health_reports_broken_requirements() -> None:
    def broken_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="grpc exporter requires sdk==1.44.0\nsecond problem\n",
            stderr="",
        )

    result = pip_check_health(run=broken_run)

    assert result.ok is False
    assert result.detail == "grpc exporter requires sdk==1.44.0; second problem"


def test_pip_check_health_reports_success() -> None:
    def clean_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="No broken requirements found.\n",
            stderr="",
        )

    result = pip_check_health(run=clean_run)

    assert result.ok is True
    assert result.detail == "No broken requirements found."


def test_pip_check_health_uses_uv_when_target_environment_has_no_pip() -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr="target-python: No module named pip\n",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="Checked 120 packages in 2ms\nAll installed packages are compatible\n",
            stderr="",
        )

    result = pip_check_health(
        run=run,
        find_executable=lambda name: "/usr/bin/uv" if name == "uv" else None,
    )

    assert result.ok is True
    assert calls[1][:3] == ["/usr/bin/uv", "pip", "check"]
    assert "All installed packages are compatible" in result.detail
