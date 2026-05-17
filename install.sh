#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Installs hermes-cortex into a project-local virtual environment and avoids
system pip / PEP 668 entirely. Default is an editable dev install because this
repo is currently intended to be run from source.

Options:
  --prod                  Install without dev extras (default: dev extras)
  --dev                   Install with dev extras (default)
  --python PATH           Python executable to use (default: python3)
  --venv PATH             Virtualenv path (default: .venv)

  --with-hermes-venv      Install hermes-cortex dependencies/CLI into Hermes Agent's venv
                          (default: ~/.hermes/hermes-agent/venv). Plugin loading
                          uses the standalone Git checkout at ~/.hermes/plugins/cortex/.
  --hermes-venv PATH      Hermes Agent venv path (default: auto-detect)

  --with-hermes-skills    Install long-term-memory skills from skills/ to
                          ~/.hermes/skills/long-term-memory/

  --help                  Show this help

Environment overrides:
  HERMES_CORTEX_PYTHON
  HERMES_CORTEX_VENV
  HERMES_AGENT_VENV
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${HERMES_CORTEX_PYTHON:-python3}"
VENV_DIR="${HERMES_CORTEX_VENV:-.venv}"
DEV_INSTALL=1
INSTALL_HERMES_VENV=0
INSTALL_HERMES_SKILLS=0
HERMES_AGENT_VENV="${HERMES_AGENT_VENV:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prod)
      DEV_INSTALL=0
      shift
      ;;
    --dev)
      DEV_INSTALL=1
      shift
      ;;
    --python)
      PYTHON_BIN="${2:?missing value for --python}"
      shift 2
      ;;
    --venv)
      VENV_DIR="${2:?missing value for --venv}"
      shift 2
      ;;
    --with-hermes-venv)
      INSTALL_HERMES_VENV=1
      shift
      ;;
    --hermes-venv)
      HERMES_AGENT_VENV="${2:?missing value for --hermes-venv}"
      shift 2
      ;;
    --with-hermes-skills)
      INSTALL_HERMES_SKILLS=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# ── Prerequisites ─────────────────────────────────────────────────────────────

if [[ ! -f pyproject.toml ]]; then
  echo "install.sh must be run from, or live in, the hermes-cortex repo root." >&2
  exit 1
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python not found: $PYTHON_BIN" >&2
  echo "Install Python 3.11+ first, e.g. sudo apt install python3-full python3-venv" >&2
  exit 1
fi

# ── Step 1: Project venv ─────────────────────────────────────────────────────

if command -v uv >/dev/null 2>&1; then
  if [[ -f "$VENV_DIR/bin/python" ]]; then
    echo "==> Virtualenv already exists: $VENV_DIR"
  else
    echo "==> Creating virtualenv with uv: $VENV_DIR"
    uv venv --python "$PYTHON_BIN" "$VENV_DIR"
  fi
  PIP_CMD=(uv pip install --python "$VENV_DIR/bin/python")
else
  echo "==> Creating virtualenv with python -m venv: $VENV_DIR"
  if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
    echo "Failed to create virtualenv. On Debian/Ubuntu run:" >&2
    echo "  sudo apt install python3-full python3-venv" >&2
    exit 1
  fi
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  PIP_CMD=("$VENV_DIR/bin/python" -m pip install)
fi

PACKAGE_SPEC="-e"
if [[ "$DEV_INSTALL" -eq 1 ]]; then
  TARGET=".[dev]"
else
  TARGET="."
fi

echo "==> Installing hermes-cortex: $PACKAGE_SPEC $TARGET"
"${PIP_CMD[@]}" "$PACKAGE_SPEC" "$TARGET"

# ── Step 2: Hermes venv dependencies/CLI ─────────────────────────────────────

if [[ "$INSTALL_HERMES_VENV" -eq 1 ]]; then
  if [[ -z "$HERMES_AGENT_VENV" ]]; then
    for candidate in "$HOME/.hermes/hermes-agent/venv" "$HOME/.hermes/hermes-agent/.venv"; do
      if [[ -f "$candidate/bin/python" ]]; then
        HERMES_AGENT_VENV="$candidate"
        break
      fi
    done
  fi

  if [[ -z "$HERMES_AGENT_VENV" ]]; then
    echo "WARNING: Hermes Agent venv not found. Skipping --with-hermes-venv." >&2
    echo "  Install Hermes first, or pass --hermes-venv PATH explicitly." >&2
  else
    echo "==> Installing hermes-cortex dependencies/CLI into Hermes Agent venv: $HERMES_AGENT_VENV"
    "$HERMES_AGENT_VENV/bin/pip" install -e "$SCRIPT_DIR" -q 2>&1 | tail -3

    echo "==> Hermes venv ready. Runtime plugin loading now uses the Git checkout:"
    echo "    $HOME/.hermes/plugins/cortex"
    echo "==> Enable the plugin and toolset if needed:"
    echo "    hermes plugins enable cortex"
    echo "    hermes tools enable cortex"
    echo "  Then start a new session (or /reset)."
  fi
fi

# ── Step 3: Hermes skills ────────────────────────────────────────────────────

if [[ "$INSTALL_HERMES_SKILLS" -eq 1 ]]; then
  SKILLS_SRC="$SCRIPT_DIR/skills"
  SKILLS_DST="$HOME/.hermes/skills"
  if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "WARNING: No skills/ directory found at $SKILLS_SRC. Skipping --with-hermes-skills." >&2
  else
    echo "==> Installing long-term-memory skills to $SKILLS_DST"
    mkdir -p "$SKILLS_DST"
    cp -r "$SKILLS_SRC/long-term-memory" "$SKILLS_DST/"
    echo "==> Done. Skills installed:"
    for sk in "$SKILLS_DST/long-term-memory"/*/; do
      name=$(basename "$sk")
      desc=""
      if [[ -f "$sk/SKILL.md" ]]; then
        desc=$(head -5 "$sk/SKILL.md" | grep "^description:" | sed 's/^description: //')
      fi
      printf "  • %s  — %s\n" "$name" "${desc:-}"
    done
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────

CORTEX_BIN="$SCRIPT_DIR/$VENV_DIR/bin/cortex"

echo
printf 'Installed hermes-cortex successfully.\n'
printf '\nPreferred Hermes CLI:\n  hermes cortex --help\n'
printf '\nShow active config/status:\n  hermes cortex config path && hermes cortex status\n'
printf '\nInitialize vault/config:\n  hermes cortex init --yes\n'
printf '\nBuild/refresh artifacts:\n  hermes cortex lifecycle maintenance\n'
printf '\nDirect venv CLI, mainly for installer debugging:\n  %q --help\n' "$CORTEX_BIN"
