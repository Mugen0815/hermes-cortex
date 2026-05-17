---
name: memory-query-flow
description: "Retrieval procedure for the agent's memory system — lookup order, source attribution, meta-check rules, write workflow, and vault conventions. MUST be loaded at EVERY knowledge-related task (vault, sessions, cortex, memory, documentation, facts)."
---

# Memory Query Flow

## Status — Core Rules moved to SOUL.md (2026-05-11)

**As of 2026-05-11, the core retrieval rules are in SOUL.md:**
- Lookup order (SOUL → MEMORY/USER → vault → filesystem → sessions)
- Source attribution (every answer must cite its source)
- Auto-use cortex triggers
- Post-write workflow (index → embed → Map-Note)
- top_k pitfall (default 10 too low, always use 20+)

`load_skill: false` in `~/.hermes/cortex/config.yaml` — this skill is no longer
auto-loaded. The hook (`_pre_llm_call`) still injects vault context every turn.

### When to load manually

Call `skill_view('memory-query-flow')` when you need:
- The `references/` files (ranking diagnostics, CLI gotchas, session perf)
- The detailed failure pattern documentation (below)
- You're in an environment without SOUL.md (fresh install, new profile)

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

**Failure #2 — Horn only fires on Turn 1 (cortex hook was turn-limited)**

1. The `_pre_llm_call` hook used to gate injection with `if not is_first_turn: return None`; current runtime lives in `plugin_runtime.py` and uses `hooks.context_injection.enabled`
2. This meant the memory-query-flow skill + vault context were injected ONLY into Turn 1
3. Hermes injects hook output into the **user message**, not the system prompt
4. Ab Turn 2 war der Skill-Kontext weg — der Agent hatte keine Regeln mehr

**Fix (applied 2026-05-11):**
- Core retrieval rules moved from this skill → **SOUL.md** (bleibt immer im System-Prompt)
- `is_first_turn`-Guard entfernt → `_pre_llm_call` injectet vault context **jeden Turn**
- `load_skill: false` in `~/.hermes/cortex/config.yaml` (redundant, da SOUL.md Regeln enthält)

**Lesson:** Wenn du dich dabei erwischst, dass du immer wieder die gleiche Source laden musst,
gehören die Regeln in SOUL.md — nicht in einen Hook oder Skill. Hooks sind ephemeral
(user message, nicht system prompt). SOUL.md ist persistent.

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

## Automation / Guardrails — Deactivated (2026-05-11)

`load_skill: false` in `~/.hermes/cortex/config.yaml` — this skill is no longer
auto-loaded into the prompt on session start. Core rules now live in **SOUL.md**.

The cortex `_pre_llm_call` hook still runs every turn to inject vault context
(query-based project/decision/task summaries), but does NOT load this skill.

### What this means for you

1. The core retrieval rules are in **SOUL.md** — read them there
2. Load this skill manually (`skill_view`) when you need references/ or detailed
   failure pattern docs
3. If SOUL.md rules seem incomplete or wrong, fix SOUL.md — not this skill

### Architecture note: why auto-injection was removed

The `_pre_llm_call` hook injects into the **user message**, not the system prompt,
and Hermes' `is_first_turn` guard meant it only fired on Turn 1. From Turn 2 onward
the skill was invisible. Moving rules to SOUL.md guarantees they're always present.
