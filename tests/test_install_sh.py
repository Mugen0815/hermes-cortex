"""Regression tests for the top-level install.sh wrapper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_with_hermes_venv_uses_uv_when_hermes_venv_has_no_pip(tmp_path: Path) -> None:
    """A uv-created Hermes venv may have python but no bin/pip."""
    repo = Path(__file__).resolve().parents[1]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"$*\" >> {uv_log}\n"
    )
    fake_uv.chmod(0o755)

    project_venv = tmp_path / "project-venv"
    (project_venv / "bin").mkdir(parents=True)
    (project_venv / "bin" / "python").write_text("#!/usr/bin/env sh\nexit 0\n")
    (project_venv / "bin" / "python").chmod(0o755)

    hermes_venv = tmp_path / "hermes-venv"
    (hermes_venv / "bin").mkdir(parents=True)
    (hermes_venv / "bin" / "python").write_text("#!/usr/bin/env sh\nexit 0\n")
    (hermes_venv / "bin" / "python").chmod(0o755)
    assert not (hermes_venv / "bin" / "pip").exists()

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["HOME"] = str(tmp_path)
    env["HERMES_AGENT_VENV"] = str(hermes_venv)

    result = subprocess.run(
        ["bash", "install.sh", "--prod", "--venv", str(project_venv), "--with-hermes-venv"],
        cwd=repo,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    uv_calls = uv_log.read_text()
    assert f"pip install --python {project_venv / 'bin' / 'python'} -e ." in uv_calls
    assert f"pip install --python {hermes_venv / 'bin' / 'python'} -e {repo}" in uv_calls
