---
name: memory-query-flow
description: "Retrieval procedure for the agent's memory system — lookup order, source attribution, meta-check rules, write workflow, and vault conventions. MUST be loaded at EVERY knowledge-related task (vault, sessions, cortex, memory, documentation, facts)."
---

# Memory Query Flow

## Status — Runtime skill bootstrap is configuration-controlled

Current Cortex runtime uses semantic hook blocks. In the standard deployed
configuration, this skill is intentionally injected every turn through:

```yaml
hooks:
  skill_context:
    enabled: true
    when: each_turn
    load_skill: true
```

That hook loads `memory-query-flow` from the active/default profile skill path and
injects it as **user-message hook context**. It is not a system prompt fragment,
and it is separate from Vault hit injection. `recent_context`, `dynamic_context`,
and `bootstrap_context` are separate channels and may be disabled while this skill
bootstrap remains active.

Legacy `hooks.context_injection.load_skill` can still appear in old configs, but
it is ignored whenever semantic hook blocks are present. Check `hermes cortex
status` or `hermes cortex config show` before assuming whether this skill is
loaded automatically.

### When to load manually

Call `skill_view('memory-query-flow')` when you need:
- The `references/` files (ranking diagnostics, CLI gotchas, session perf)
- The detailed failure pattern documentation (below)
- You are in an environment/profile where `hooks.skill_context.load_skill` is false
  or Cortex hook injection is not available

### Absolute triggers (check SOUL.md first — it's in your prompt):

- User asks "what do you know about X?" or "remember Y" or "erinnere Z"
- A response references content from MEMORY.md / USER.md / vault / sessions
- You are about to call ANY vault tool: `vault_search`, `vault_read_note`, `vault_build_context`
- You are about to call `session_search`
- You are writing new knowledge to memory, vault, or skills
- A task involves cortex (index, embed, or vault management)

### Why this is critical

This skill contains the **lookup order** (vault → sessions, not sessions → vault),
**source attribution** rules, and **write workflow** (cortex index + embed after vault writes).
Without it, the agent defaults to `session_search` or raw code reading — which the
user has explicitly criticised as incorrect behavior.

### Common failure patterns

**Failure #1 — Session-search-first reflex**

1. User asks a question about architecture/tooling/workflow
2. Agent calls `session_search` or starts grepping code
3. Agent misses that `vault_search()` already has the structured answer

**Fix:** Always start at step 2 of the retrieval order (vault), never at step 3 (sessions).
Even when the question feels like "I need to look at code/config" — check the vault first.
The vault exists specifically to cache the answers you'd otherwise have to grep for.

**Failure #2 — assuming the skill bootstrap is off because legacy config says so**

1. Older configs used `hooks.context_injection.load_skill`, and some historical
   docs said `load_skill: false` meant this skill was no longer auto-loaded.
2. Current configs use semantic hook blocks. The effective switch is now
   `hooks.skill_context.enabled` + `hooks.skill_context.load_skill`.
3. If that semantic block is enabled, `memory-query-flow` is injected every turn
   even when the legacy `context_injection` block is disabled or ignored.
4. Hermes injects hook output into the **user message**, not the system prompt, so
   this is runtime context rather than durable prompt policy.

**Fix:** Do not infer behavior from stale legacy YAML. Run `hermes cortex status`
and inspect the Hook lifecycle table:

- `pre_llm skill_bootstrap yes/effective each_turn` → this skill is injected.
- `legacy_context_injection ... legacy-ignored` → old `context_injection` keys are
  compatibility baggage, not the active path.

**Lesson:** Treat Cortex hook state as a lifecycle table, not a single boolean.
The distinction is tedious, yes. It also prevents expensive ghost hunts.

**Failure #3 — vault_search false negative (top_k blind spot)**

1. Task requires finding or checking existence of a specific vault note (e.g., `Project - foo.md`)
2. Agent calls `vault_search("Project foo")` with default `top_k=10`
3. vault_search returns results but the target note is NOT among them
4. Agent concludes the note doesn't exist (WRONG — it does)
5. Agent creates a duplicate or writes incorrect cross-references

**Why:** BM25 ranks short Wikilink chunks (`"- [[Project - foo]]"` from OTHER notes)
higher than the actual target note's long-form content. At default top_k=10 the
target note may not appear even when it has 10+ indexed chunks.

**Fix (4-step diagnostic):**
1. Re-run vault_search with `top_k=30` — the target may still be outside the window
2. Use `search_files(pattern="*foo*", target="files")` for filesystem-level verification
3. Check `grep '"file": "30_projects/Project - foo.md"' ~/.hermes/cortex/chunks.jsonl`
   to confirm the file IS indexed
4. If confirmed indexed but not ranking: accept that fine-grained ranking is a known
   limitation; use `vault_read_note` directly with the known path

**Detailed diagnostic reference:** `references/vault_search_ranking_diagnostic.md`
— full transcript of the ranking analysis, BM25 vs Wikilink scoring, and raw
`cortex search` output.

**Scoring triage reference:** `references/cortex_scoring_triage.md`
— boosted-vs-unboosted comparison workflow, boost-dominance symptoms,
link-only chunk penalty ideas, German query weakness, and ticket acceptance criteria.

## Responsibility split

| Location | What goes there |
|---|---|
| **SOUL.md** | Persona, behavior rules, lookup procedure |
| **MEMORY.md** | Minimal runtime coordinates and pointers only; no detailed project/host documentation |
| **USER.md** | User profile, preferences |
| **Vault** | Structured durable knowledge: facts, projects, runbooks |
| **Skills** | Procedural knowledge: bug fixes, setup workflows |

**Rule of thumb:** If it says *what to do* → SOUL.md. If it says *what exists* → MEMORY.md, vault, or skills.

## Retrieval order

```
0. SOUL.md              — in prompt (behavior rules, memory lookup procedure)
1. MEMORY.md / USER.md  — in prompt (facts, coordinates, profile)
2. vault_search()       — vault (cortex hybrid search; mind the top_k pitfall below)
3. [VERIFY] search_files(target='files') — fallback filesystem search when vault_search
   produces no or unexpected results for a known filename/pattern
4. session_search()     — past sessions (only when vault is insufficient)
```

> **session_search performance:** The DB query is fast (~0.1s); the latency comes
> from LLM summarization of matched sessions. If search feels slow, check
> `references/session_search_performance.md` for tuning options (model choice,
> max_concurrency, MAX_SESSION_CHARS).

**Rule:** For any "what do you know about..." question, **always** call `vault_search()`
first, even if MEMORY.md already has a hit. The vault has depth.

> **⚠️ vault_search top_k pitfall:** Default `top_k=10` is too low for vaults with
> 300+ chunks. Short Wikilink chunks (`"- [[Project - Foo]]"`) from OTHER notes
> can dominate the top ranks via BM25, pushing the actual target note outside
> the window. A specific note may exist (confirmed via `search_files` or `grep`
> in chunks.jsonl) yet not appear in vault_search results even at `top_k=30`.
> **Always use `top_k=20+` when searching for a specific note by name.** If the
> expected note doesn't appear, verify existence at the filesystem level before
> concluding it doesn't exist.

## Source attribution

After every successful lookup, **explicitly** state where the information came from:

| Source | Say |
|---|---|
| MEMORY.md / USER.md (prompt) | "from memory" or "from my prompt" |
| vault_search() → vault | "from the vault (via cortex)" |
| session_search() | "from past sessions" |
| Uncertain / mixed sources | Do not fabricate a source |

## Meta-check

**Never make a claim about the contents of MEMORY.md / USER.md / SOUL.md without
having seen it in your prompt first.**

If unsure whether a fact is in your prompt: search the vault instead and attribute
there. If unsure about your own architecture: `read_file` on the relevant file,
then answer. Do not guess.

## Auto-use cortex

Use `vault_search`, `vault_read_note`, `vault_build_context` automatically when:

- User asks "what do you know about [person/system/project]?"
- User asks "remember [concept/decision/fact]"
- Answer needs details not in your prompt memory
- Any doubt whether prompt info is current/complete

Do **not** use for:
- Trivial facts you are certain are in your prompt (own name, current session IDs)
- Pure conversation questions ("how are you?", "what's 2+2?")

## Writing (vault promotion)

When the user says "save to long-term memory", "put that in the vault",
"document this" — or when you identify a fact worth preserving:

### What goes where

| User says | What to do |
|---|---|
| "Remember...", "Note this...", correction about tool/behavior | `memory` tool (compact single facts) |
| "Save to long-term memory / document / vault" | Write to vault → index → embed |

### Write workflow

1. Write or update note in `vault/<folder>/<file>.md`
2. Keep frontmatter status inside Cortex's known enum. Use `active`, `draft`, `archived`, `deprecated`, `stale`, or `superseded`; do not invent workflow states like `approved`/`accepted` as `status`. Put those as separate fields such as `approved: YYYY-MM-DD` or `implemented: YYYY-MM-DD`.
3. Run `cortex index` (rebuild chunks)
4. Run `cortex embed` (rebuild embeddings)

> **Cortex CLI pitfalls:** `cortex index` / `cortex embed` do NOT accept
> a `--path` flag — they read the vault path from
> `~/.hermes/cortex/config.yaml`.  See `references/cortex-cli-gotchas.md`
> for common mistakes and the editable-install deployment workflow.

### After writing

Check whether the new note should be linked from `60_maps/Map - Knowledge Index.md`.

---

## Automation / Guardrails — Semantic hook mode (current)

In current semantic hook mode, the `memory-query-flow` skill may be auto-loaded on
each turn by the Cortex `skill_context` block:

```yaml
hooks:
  skill_context:
    enabled: true
    when: each_turn
    load_skill: true
```

The hook output is injected into the **user message**, not the system prompt. This
means it is excellent for runtime guardrails and operational reminders, but it is
not a replacement for truly durable profile/system policy.

### What this means for you

1. Check `hermes cortex status` for the effective hook lifecycle before debugging
   memory behavior.
2. If `skill_bootstrap` is effective, this skill is already present in hook
   context; still call `skill_view` when you need linked `references/` files.
3. If the rules here are wrong, fix this skill in the repo and reinstall/sync
   skills into the runtime profile. If profile policy is wrong, fix the profile
   bootstrap separately.
4. Keep `recent_context` and `dynamic_context` conceptually separate: they inject
   retrieved content, while `skill_context` injects retrieval procedure.
