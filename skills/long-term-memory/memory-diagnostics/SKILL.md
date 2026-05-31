---
name: memory-diagnostics
description: "Use when debugging Hermes/Cortex memory retrieval: hook bootstrap state, semantic-vs-legacy context config, vault_search false negatives, session_search latency, source attribution drift, or Cortex index/embed CLI failures."
---

# Memory Diagnostics

## When to use

Load this when the normal `memory-query-flow` hotpath is not enough:

- The agent cannot find a known Vault note.
- Hook-loaded `memory-query-flow` seems missing, stale, duplicated, or too noisy.
- `session_search` feels slow or returns confusing context.
- `cortex index` / `cortex embed` / `cortex status` behavior is unclear.
- Source attribution or memory-vs-vault routing appears wrong.

## 1. Hook bootstrap / config state

First record the effective runtime state:

```bash
hermes cortex status
hermes cortex config show
```

Read the Hook lifecycle table, not a single boolean:

- `pre_llm skill_bootstrap yes/effective each_turn` means the skill body is injected.
- `target: user-message hook context` means it is **not** system prompt policy.
- `legacy_context_injection ... legacy-ignored` means old `hooks.context_injection`
  keys are compatibility baggage when semantic hook blocks are present.
- `recent_context`, `dynamic_context`, and `bootstrap_context` are separate channels;
  do not conflate them with `skill_context`.

Long-running TUI, gateway, and worker processes may keep old code/config until a
new session/process or approved restart owns the change. Yes, process lifetime:
the traditional goblin in the machine.

## 2. `vault_search` false negatives

Symptom: a specific note exists, but default `vault_search(..., top_k=10)` does
not show it. Common cause: short Wikilink chunks from other notes outrank the
actual long-form note.

Diagnostic:

1. Re-run with `top_k=30`.
2. Verify the file exists with `search_files(pattern="*title*", target="files")`.
3. Check whether the note is indexed in `~/.hermes/cortex/chunks.jsonl` when needed.
4. If indexed but not ranking, use `vault_read_note` with the known path.
5. If `vault_read_note` fails for a path confirmed in `chunks.jsonl`, report the
   exact error; do not fabricate content. Suggest `cortex index` to resync.

Detailed references in the source tree:

- `skills/long-term-memory/memory-query-flow/references/vault_search_ranking_diagnostic.md`
- `skills/long-term-memory/memory-query-flow/references/cortex_scoring_triage.md`

## 3. `session_search` latency

The FTS/SQLite lookup is usually fast; user-visible latency mainly comes from
LLM summarization of matched sessions.

Triage:

- Prefer Vault lookup first for durable knowledge.
- Use narrower queries or exact phrases.
- For tuning, inspect `tools/session_search_tool.py` limits and auxiliary model
  routing in the Hermes profile.
- Use recent-session browse mode when the user asks generally what was recent.

Reference:

- `skills/long-term-memory/memory-query-flow/references/session_search_performance.md`
- `skills/long-term-memory/memory-query-flow/references/session_search_architecture.md`

## 4. Cortex CLI/index/embed gotchas

`cortex index` and `cortex embed` do **not** accept `--path`; they read the Vault
path from the effective Cortex config.

Check before blaming the universe:

```bash
hermes cortex config show
cortex index
cortex embed
```

If either command exits non-zero, report the exact stderr/stdout and stop. Do not
claim the Vault is searchable until both index and embed succeeded.

Reference:

- `skills/long-term-memory/memory-query-flow/references/cortex-cli-gotchas.md`

## 5. Source attribution drift

If answers cite the wrong source:

1. List the actual tools/files used in the turn.
2. Distinguish visible prompt memory from Vault hits and session hits.
3. If the answer relies on repo/runtime behavior, verify with `read_file`, git, or
   command output and attribute it as runtime/repo evidence.
4. If sources are mixed, say so instead of inventing a single canonical source.

## Verification checklist

- [ ] `hermes cortex status` path and lifecycle table recorded.
- [ ] Specific-note searches used `top_k=30` plus filesystem fallback.
- [ ] Session answers came from `session_search`, not claimed as Vault facts.
- [ ] Vault writes were followed by successful `cortex index` and `cortex embed`.
- [ ] Final answer names the real source(s) used.
