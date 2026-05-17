#!/usr/bin/env bash
set -euo pipefail

REMOTE="${HERMES_CORTEX_REPO:-https://github.com/Mugen0815/hermes-cortex.git}"
BRANCH="${HERMES_CORTEX_BRANCH:-main}"
USER_HOME="$(getent passwd "${SUDO_USER:-${USER:-}}" 2>/dev/null | cut -d: -f6 || true)"
if [[ -z "$USER_HOME" ]]; then
  USER_HOME="$HOME"
fi
TARGET="$USER_HOME/.hermes/plugins/cortex"
APPLY=0

usage() {
  cat <<'EOF'
Usage: scripts/prepare-plugin-checkout.sh [--apply] [--branch NAME] [--target PATH] [--remote URL]

Prepare ~/.hermes/plugins/cortex as a Git checkout for the Cortex Hermes plugin.

Default mode is dry-run: prints the Git operations but does not change files.
Use --apply only after the plugin directory migration is ready for runtime testing.

This script intentionally does not copy or rsync repository files. The target is
created/updated only via git clone/fetch/switch/pull.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --branch)
      BRANCH="${2:?missing value for --branch}"
      shift 2
      ;;
    --target)
      TARGET="${2:?missing value for --target}"
      shift 2
      ;;
    --remote)
      REMOTE="${2:?missing value for --remote}"
      shift 2
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

run() {
  printf '+'
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  if [[ "$APPLY" -eq 1 ]]; then
    "$@"
  fi
}

if [[ "$TARGET" != /* ]]; then
  echo "Target must be an absolute path: $TARGET" >&2
  exit 2
fi

if [[ "$APPLY" -eq 0 ]]; then
  echo "DRY-RUN only. Re-run with --apply to execute."
fi

echo "Remote: $REMOTE"
echo "Branch: $BRANCH"
echo "Target: $TARGET"

if [[ -e "$TARGET" && ! -d "$TARGET" ]]; then
  echo "Target exists but is not a directory: $TARGET" >&2
  exit 1
fi

if [[ -d "$TARGET/.git" ]]; then
  run git -C "$TARGET" fetch origin --prune
  run git -C "$TARGET" switch "$BRANCH"
  run git -C "$TARGET" pull --ff-only origin "$BRANCH"
elif [[ -d "$TARGET" ]] && [[ -n "$(find "$TARGET" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  echo "Refusing to overwrite non-empty non-Git target: $TARGET" >&2
  echo "Move the old stub aside manually, then rerun with --apply." >&2
  if [[ "$APPLY" -eq 0 ]]; then
    echo "Dry-run note: --apply would stop here until the stub is moved aside." >&2
    exit 0
  fi
  exit 1
else
  PARENT="$(dirname "$TARGET")"
  run mkdir -p "$PARENT"
  run git clone --branch "$BRANCH" --single-branch "$REMOTE" "$TARGET"
fi

if [[ "$APPLY" -eq 1 ]]; then
  git -C "$TARGET" status --short --branch
else
  echo "Dry-run complete; no filesystem changes made."
fi
