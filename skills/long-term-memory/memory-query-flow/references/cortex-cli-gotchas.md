# Cortex CLI Gotchas

> Addendum to vault workflow — common CLI mistakes and pitfalls.
> Referenced from: `memory-query-flow` skill.

## `cortex index` — no `--path` flag

❌ **Wrong:** `cortex index --path ~/vault/10_facts/`
✅ **Right:** `cortex index`

The indexer reads the vault path from `~/.hermes/cortex/config.yaml`.  Use
`cortex index --config /path/to/config.yaml` to point at a different config,
but there is no per-call `--path` override.

Same applies to `cortex embed`.

## `cortex index --force`

Re-indexes ALL files (ignoring the chunk hash cache).  Use after:
- Changing the vault path in config.yaml
- Manually editing chunks.jsonl
- Adding many new notes where the incremental diff becomes unreliable

## Live deployment — standalone Hermes plugin checkout

Current runtime (verified 2026-05-13): Cortex is loaded as a **directory-based Hermes plugin** from
`~/.hermes/plugins/cortex/`, which is a real Git checkout.

Preferred workflow:

```bash
# Develop in source repo
cd ~/hermes-workspace/hermes-cortex
# ... make code changes ...
git add ... && git commit && git push

# Deploy by fast-forwarding the runtime plugin checkout
cd ~/.hermes/plugins/cortex
git pull --ff-only
./install.sh --with-hermes-venv --with-hermes-skills  # if dependencies/skills changed

# Smoke-test
hermes cortex --help
hermes cortex search "cortex plugin runtime" --top-k 3 --json
```

The Hermes venv should load `hermes-cortex` from `~/.hermes/plugins/cortex`.

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip show hermes-cortex
~/.hermes/hermes-agent/venv/bin/python - <<'PY'
import cortex
print(cortex.__file__)  # expected: ~/.hermes/plugins/cortex/cortex/__init__.py
PY
```

## `vault_search` — default top_k trap

Default `top_k=10` is too low for vaults with 300+ indexed chunks.  Short
Wikilink chunks from OTHER notes (`"- [[Project - Foo]]"`) dominate BM25
ranking and push the actual target note out of the top-10 window.

**Always use `top_k=20+` when searching for a specific note by name.**

See `references/vault_search_ranking_diagnostic.md` for the full transcript
and raw `cortex search` output analysis.
