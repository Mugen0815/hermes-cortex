"""Read-only health checks for Cortex's shared Python runtime."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HealthCheck:
    ok: bool
    detail: str


def _one_line(text: str) -> str:
    return "; ".join(line.strip() for line in text.splitlines() if line.strip())


def embedding_stack_health(
    *,
    import_module: Callable[[str], Any] = importlib.import_module,
) -> HealthCheck:
    """Prove that Chroma and SentenceTransformers can both be imported."""
    for module_name in ("chromadb", "sentence_transformers"):
        try:
            import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - third-party imports can fail in many ways
            return HealthCheck(
                ok=False,
                detail=f"{module_name}: {type(exc).__name__}: {exc}",
            )
    return HealthCheck(ok=True, detail="chromadb and sentence_transformers imports ok")


def pip_check_health(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    find_executable: Callable[[str], str | None] = shutil.which,
) -> HealthCheck:
    """Run pip's installed-distribution consistency check without mutating state."""
    try:
        result = run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return HealthCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")

    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0 and "No module named pip" in combined_output:
        uv = find_executable("uv")
        if uv:
            try:
                result = run(
                    [uv, "pip", "check", "--python", sys.executable],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return HealthCheck(ok=False, detail=f"{type(exc).__name__}: {exc}")

    detail = _one_line(result.stdout or result.stderr)
    if not detail:
        detail = f"pip check exited {result.returncode} without output"
    return HealthCheck(ok=result.returncode == 0, detail=detail)
