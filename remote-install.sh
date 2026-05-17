#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: remote-install.sh [options] [-- install.sh options]

Clones or updates hermes-cortex as a standalone Hermes plugin checkout, then
optionally runs install.sh. This script is intended for curl/wget-style installs
and for refreshing the runtime checkout on machines that do not have the repo yet.

Options:
  --repo URL       Git repository URL
                   (default: $HERMES_CORTEX_REPO or https://github.com/Mugen0815/hermes-cortex.git)
  --dir PATH       Runtime plugin checkout directory
                   (default: $HERMES_CORTEX_DIR or ~/.hermes/plugins/cortex)
  --branch NAME    Branch to checkout (default: $HERMES_CORTEX_BRANCH or main)
  --skip-install   Only clone/update the plugin checkout; do not run install.sh
  --help           Show this help

Everything after '--' is passed to install.sh.

Examples:
  # Install/update the Hermes runtime plugin checkout and install deps/skills
  ./remote-install.sh -- --with-hermes-venv --with-hermes-skills

  # Docs-only/runtime-code update; no dependency or skill reinstall
  ./remote-install.sh --skip-install

  # Custom target, e.g. a dev checkout
  ./remote-install.sh --dir ~/hermes-workspace/hermes-cortex --skip-install
EOF
}

REPO_URL="${HERMES_CORTEX_REPO:-https://github.com/Mugen0815/hermes-cortex.git}"
INSTALL_DIR="${HERMES_CORTEX_DIR:-$HOME/.hermes/plugins/cortex}"
BRANCH="${HERMES_CORTEX_BRANCH:-main}"
RUN_INSTALL=1
INSTALL_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_URL="${2:?missing value for --repo}"
      shift 2
      ;;
    --dir)
      INSTALL_DIR="${2:?missing value for --dir}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:?missing value for --branch}"
      shift 2
      ;;
    --skip-install)
      RUN_INSTALL=0
      shift
      ;;
    --)
      shift
      INSTALL_ARGS=("$@")
      break
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

if ! command -v git >/dev/null 2>&1; then
  echo "git is required. Install it first, e.g. sudo apt install git" >&2
  exit 1
fi

if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/.git" ]]; then
  echo "Target exists but is not a Git checkout: $INSTALL_DIR" >&2
  echo "Move it aside or pass --dir to use a different target." >&2
  exit 1
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  echo "==> Updating existing hermes-cortex plugin checkout: $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch origin --prune
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
else
  echo "==> Cloning hermes-cortex plugin checkout into: $INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

if [[ "$RUN_INSTALL" -eq 0 ]]; then
  echo "==> Skipping install.sh (--skip-install)"
  echo "==> Runtime checkout ready: $INSTALL_DIR"
  exit 0
fi

exec "$INSTALL_DIR/install.sh" "${INSTALL_ARGS[@]}"
