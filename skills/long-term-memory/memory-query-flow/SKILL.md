---
name: memory-query-flow
description: "Use when retrieving, attributing, or writing durable memory with Cortex/Vault. Hotpath rules for lookup order, source attribution, memory-vs-vault routing, and post-write index/embed workflow."
---

# Memory Query Flow

## Purpose

This is the **hotpath** memory workflow. Keep it small enough to be useful when it
is auto-injected by the Cortex `skill_context` hook.

Load `memory-diagnostics` only when debugging hook injection, ranking failures,
`session_search` latency, or Cortex CLI/index/embed problems. Tiny hotpath, big
diagnostics elsewhere. Revolutionary stuff.

## Core rules

1. **Vault before sessions.** Use `vault_search` / `vault_read_note` for durable
   knowledge before `session_search` or raw repo spelunking.
2. **Attribute sources.** Say whether an answer came from prompt memory, the Vault
   via Cortex, past sessions, files/runtime evidence, or mixed sources.
3. **Do not claim prompt-file contents unless visible.** If unsure about
   `SOUL.md`, `MEMORY.md`, or `USER.md`, inspect/search instead of guessing.
4. **Specific note lookups need a wider net.** Use `top_k=30` for note names;
   if the expected note is absent or results look wrong, verify with
   `search_files(target="files")` before declaring it missing.
5. **Use sessions for history, not canonical truth.** Call `session_search` when
   the user asks what happened in prior chats or when the Vault is insufficient.

## Retrieval order

```text
0. Prompt context          — SOUL.md / MEMORY.md / USER.md when visibly injected
1. vault_search/read       — structured durable knowledge via Cortex
2. search_files            — filesystem verification for known/specific note paths
3. session_search          — raw past conversations, only when needed
4. repo/runtime inspection — code/config/files when current implementation matters
```

For "what do you know about X?" start with `vault_search(query=X, top_k=20+)`.
For a specific note title or filename, use `top_k=30` and filesystem fallback.

## Source attribution phrases

| Source | Phrase |
|---|---|
| Prompt memory / visible SOUL, MEMORY, USER | "from prompt memory" / "from my prompt" |
| Vault tools | "from the Vault via Cortex" |
| `session_search` | "from past sessions" |
| Files, git, commands, status output | "from runtime/repo evidence" |
| Mixed/uncertain | Name the sources you actually used; do not invent one |

## Memory vs Vault routing

| User intent | Target |
|---|---|
| "Remember...", "Note this...", "merk dir das" about a user preference, correction, or stable tool behavior | `memory` tool: compact single fact |
| "Save to long-term memory", "put this in the vault", "document this" | Vault note: structured durable knowledge |
| "Remember..." / "merk dir das" about a project decision, runbook, architecture, design direction, or durable system fact | Vault note first; optionally add a compact memory pointer |
| Project decision, runbook, architecture, durable system fact | Vault note, unless user explicitly asks only for compact prompt memory |
| Temporary task status, PR/issue IDs, stale-by-next-week details | Leave in session / task context, not durable memory |

Do **not** route by phrase alone. Phrases like "remember this", "note this", or
"merk dir das" can request either compact prompt memory or durable Vault storage.
Choose by content durability: project decisions, architecture, design direction,
runbooks, and memory-system facts belong in the Vault and require the index/embed
workflow.

## Vault write workflow

When writing to the Vault:

1. Search first: `vault_search(..., top_k=30)`; use `search_files` for known note
   names or suspicious search results.
2. Update an existing matching note instead of creating a duplicate.
3. Write/update `vault/<folder>/<file>.md` with valid frontmatter.
4. Use only canonical `status` values from `docs/METADATA.md`: `active`,
   `draft`, `archived`, `deprecated`, `stale`, or `superseded`. Put workflow
   labels like `proposed`, `planned`, `approved`, or `implemented` in separate
   fields.
5. Run Cortex indexing via the Hermes wrapper: `hermes cortex index`. If it fails, report the exact error and stop.
6. Run Cortex embedding via the Hermes wrapper: `hermes cortex embed`. If it fails, report the exact error and stop.
7. When updating Cortex-related docs or skills, search the active skill/source/vault roots for wrapperless Cortex command examples and accidental doubled Hermes prefixes; fix active sources, but do not rewrite historical backups or editor history unless explicitly asked.
8. Link from `60_maps/Map - Knowledge Index.md` when the note should be discoverable
   from the map.

`hermes cortex index` and `hermes cortex embed` read the vault path from the active Cortex config; they do **not** take `--path`.

**Operator CLI rule:** document and suggest Cortex commands only through the Hermes wrapper (`hermes cortex ...`). Do not recommend wrapperless Cortex commands, even as compatibility notes; stale wrapperless examples should be corrected when found.

## Hook/runtime note

Current Cortex deployments may auto-inject this skill each turn via:

```yaml
hooks:
  skill_context:
    enabled: true
    when: each_turn
    load_skill: true
```

That hook injects **user-message context**, not system prompt policy. Check
`hermes cortex status` or `hermes cortex config show` when debugging effective
behavior. If semantic hook blocks are present, legacy `hooks.context_injection`
may still be parsed for compatibility but is not the active path.

Still call `skill_view('memory-query-flow')` when a task explicitly requires this
skill, when references are needed, or when hook injection is unavailable/uncertain.
