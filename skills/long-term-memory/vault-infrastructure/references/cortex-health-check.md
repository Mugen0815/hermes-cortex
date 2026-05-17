# cortex health check — systematic audit

When asked to "check the cortex integration" or when troubleshooting memory retrieval,
verify these four dimensions systematically:

## 1. Git parity

```bash
# Runtime plugin checkout
cd ~/.hermes/plugins/cortex && git rev-parse --short HEAD && git status --short --branch
# Dev/source clone
cd ~/hermes-workspace/hermes-cortex && git rev-parse --short HEAD && git status --short --branch
# Check for unpushed commits in each checkout
cd ~/.hermes/plugins/cortex && git log --oneline origin/main..HEAD
cd ~/hermes-workspace/hermes-cortex && git log --oneline origin/main..HEAD
```

Both clones should be on the same commit and `main` branch.

## 2. Plugin wiring

```bash
ls ~/.hermes/plugins/cortex/
# Must contain: plugin.yaml  __init__.py

grep -A2 "cortex" ~/.hermes/config.yaml
# Expected: platform_toolsets.cli includes cortex, plugins.enabled includes cortex
```

## 3. Index health

```bash
wc -l ~/.hermes/cortex/chunks.jsonl
du -sh ~/.hermes/cortex/chroma/
hermes cortex graph status
# Expected: 0 unresolved refs, 0 orphans, 0 stale candidates
```

## 4. Configuration alignment

```bash
# MEMORY.md vault path
grep "Vault:" ~/.hermes/memories/MEMORY.md

# Cortex config vault path (must match MEMORY.md)
grep "path:" ~/.hermes/cortex/config.yaml

# CORTEX_CONFIG may override default config; CORTEX_REPO is legacy and should not be load-bearing
grep -E 'CORTEX_CONFIG|CORTEX_REPO' ~/.hermes/.env || true

# SOUL.md must reference the cortex vault (not legacy path)
grep "vault" ~/.hermes/SOUL.md
```

## Pitfalls during audit

- The `session_search` tool queries SQLite FTS5 (`~/.hermes/state.db`), not the JSON session files.
  An empty result means the session isn't in the DB — it may still exist on disk.
- `cortex init --dry-run` is interactive and hangs without a TTY. Use `--yes` for scripting.
- After enabling the toolset, the gateway must be restarted (prompt-caching).
- Never report an audit finding as "not found in X" without checking both DB and disk.
