#!/usr/bin/env bash
set -euo pipefail

HERMES_BIN="${HERMES_BIN:-hermes}"
TMP_JSON="${TMPDIR:-/tmp}/cortex-search-eval-smoke.json"
TMP_ERR="${TMPDIR:-/tmp}/cortex-search-eval-smoke.err"

if ! command -v "$HERMES_BIN" >/dev/null 2>&1; then
  echo "Hermes binary not found: $HERMES_BIN" >&2
  exit 127
fi

if ! "$HERMES_BIN" cortex --help 2>&1 | grep -q 'search-eval'; then
  echo "FAIL: 'hermes cortex --help' does not expose search-eval" >&2
  "$HERMES_BIN" cortex --help >&2 || true
  exit 1
fi

if ! "$HERMES_BIN" cortex config path >/dev/null; then
  echo "FAIL: hermes cortex config path exited non-zero" >&2
  exit 1
fi

if ! "$HERMES_BIN" cortex status >/dev/null; then
  echo "FAIL: hermes cortex status exited non-zero" >&2
  exit 1
fi

# --allow-failures makes this a runtime surface/JSON-shape smoke check, not a
# scoring gate. Scoring regressions are handled by the dedicated eval tasks.
if ! "$HERMES_BIN" cortex search-eval --json --allow-failures >"$TMP_JSON" 2>"$TMP_ERR"; then
  echo "FAIL: hermes cortex search-eval --json --allow-failures exited non-zero" >&2
  cat "$TMP_ERR" >&2
  exit 1
fi

python3 - "$TMP_JSON" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text())
required = {"schema_version", "top_k", "case_count", "passed", "failed", "cases"}
missing = sorted(required - set(payload))
if missing:
    raise SystemExit(f"missing JSON keys: {missing}")
if not isinstance(payload["cases"], list):
    raise SystemExit("JSON key 'cases' is not a list")
print(
    "OK: hermes cortex search-eval JSON shape present "
    f"({payload['passed']}/{payload['case_count']} cases pass; scoring failures allowed)"
)
PY
