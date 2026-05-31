---
name: obsidian
description: Read, search, and create notes in the Obsidian vault — conventions, frontmatter, wikilinks, folder routing, and proactive writing.
---

# Vault notes (obsidian)

## Folder routing

| Content | Folder |
|---|---|
| Stable facts about systems, tools, environment | `10_facts/` |
| Decisions with rationale | `20_decisions/` |
| Active project contexts | `30_projects/` |
| Repeatable procedures, troubleshooting | `40_runbooks/` |
| People notes | `50_people/` |
| Index and navigation notes | `60_maps/` |
| Note templates | `80_templates/` |
| Human-review candidates only | `00_inbox/` |

## Frontmatter (required)

Every note must have YAML frontmatter with these fields.
The **closing `---` delimiter is required** — without it the parser treats the note as having no frontmatter at all!

```yaml
---
type: fact | decision | project | runbook | map | person
status: active | draft | archived | deprecated | stale | superseded   # use `archived` for finished/retired projects; `completed` is invalid and triggers cortex warnings
domain: infrastructure | operations | workflow | tools | software  # optional but recommended
tags: [tag1, tag2]
aliases: [alternativer_name]                                       # optional, enables redirects
confidence: high | medium | low
importance: high | medium | low
stability: stable | evolving | deprecated | draft
last_verified: YYYY-MM-DD
created: YYYY-MM-DD
updated: YYYY-MM-DD
---
```

### Pitfall: Missing closing `---`

If a note has valid YAML inside the frontmatter block but **no closing `---`**, the cortex parser returns an **empty frontmatter dict**. The note is still indexed (chunks exist) but fields like `type`, `status`, `domain` are empty strings.

Consequences:
- Graph diagnostics flag the note as **suspicious** ("missing status", "missing domain")
- The note can't be routed to a target folder by `cortex lifecycle nightly`
- The note is invisible to `fm_status`/`fm_type`-based filters in the graph viewer

**Check:** After writing/editing a note, run `cortex index --force && cortex graph build --force` and call `cortex graph status` — 0 ambiguous + 0 broken means frontmatter is clean.

### Pipeline fields (for `00_inbox/` review candidates)

Prefer canonical-first writing: high-confidence durable knowledge goes directly
to `10_facts/`, `20_decisions/`, `30_projects/`, or `40_runbooks/`. Use
`00_inbox/` only for uncertain, conflicting, duplicate-sensitive, or otherwise
human-review-needed cases.

Review candidates in `00_inbox/` need explicit review metadata:

```yaml
status: draft
review_status: pending
review_reason: "why human review is needed"
promote: true
promote_type: fact|decision|project|runbook
cortex_promote: true             # alternative to promote: true
```

`promote_type` maps: `fact`→`10_facts`, `decision`→`20_decisions`, `project`→`30_projects`, `runbook`→`40_runbooks`.
These fields are stripped from the canonified note during promotion.

Do not use `status: review`; it is not a known Cortex status. Do not leave an
archived source note promotable:

```yaml
status: archived
promote: true                    # invalid
```

## Wikilinks

Link notes together:

```markdown
See [[Runbook - Promote session knowledge]] for details.
```

Use `related:` in frontmatter for structured cross-references:

```yaml
related: ['[[Runbook - Promote session knowledge]]', '[[Project - Hermes VM]]']
```

### Renaming or archiving notes

When renaming an Obsidian note, do not stop at the file move:

1. Search for old wikilinks with `search_files`/`grep` (e.g. `[[Old Title]]`).
2. Patch links to the new title or remove them if the note is intentionally gone.
3. Run `hermes cortex index && hermes cortex embed && hermes cortex graph build`.
4. Run `hermes cortex graph broken` and fix newly introduced broken references.
5. If unrelated broken references already exist, name them as pre-existing instead of mixing them into the current task.

This is especially important for generic active Decision names reused across runs; archive them with run-specific titles before starting a fresh run.

## Tools (use cortex, not shell)

| Tool | Purpose |
|---|---|
| `vault_search(query, filters, top_k)` | Hybrid search: BM25 + vector + RRF |
| `vault_read_note(file, heading_path)` | Full text of a note |
| `vault_build_context(query, budget)` | Token-budgeted context block |

## When to write proactively

Write to the vault **without being asked** when:

- A non-trivial decision was made (what, why, alternatives) → `20_decisions/`
- A new system fact, tool quirk, or environment detail was discovered → `10_facts/`
- A repeatable workflow or troubleshooting pattern emerged → `40_runbooks/`
- An active project gained new context or status → `30_projects/`
- Something durable that hasn't been categorized yet → `00_inbox/`

**Do not write:** session noise, transient state, task progress logs.

### Continuation notes when the conversation gets too dense

If the user says the analysis is too much to scroll, asks to "save this for later", or the session has produced a large audit/decision tree:

1. Prefer updating the existing active `30_projects/Project - ...` note over creating a new one.
2. Add a short dated section with: verified facts, user decisions, open questions, and next actions. Keep it skimmable; do not paste the whole transcript.
3. Preserve the user's corrections as explicit bullets (e.g. "remove, don't merely mark legacy paths").
4. Run `hermes cortex lifecycle maintenance` after the vault write.
5. Verify retrieval. If `vault_read_note(..., heading_path=...)` misses the new heading because chunking/indexing has not exposed it yet, confirm with a filesystem grep/search before assuming the note was not saved.

### ⚠️ Check-before-create

**Before creating any new note, ALWAYS verify it doesn't already exist:**
1. `vault_search("Note Title", top_k=30)` — use high top_k, default 10 is too low
2. `search_files(pattern="*Note Title*", target="files")` — filesystem backup when vault_search doesn't find it
3. `grep '"file": "30_projects/Project - Note Title.md"' ~/.hermes/cortex/chunks.jsonl` — confirm it's indexed

Missing this check leads to duplicate notes, wrong cross-references, and
user frustration ("Natürlich gibt es eine Project-Note!"). Vault_search's BM25
ranker can push long target notes outside the top-k window — don't trust a
negative result at face value. See `memory-diagnostics` for the full diagnostic
flow.

## User language mapping (English and German)

| User says | Action |
|---|---|
| "Remember...", "Note this..." | `memory` tool (compact fact) |
| "Save to long-term memory / vault / Obsidian", "in den Vault / ins Langzeitgedächtnis" | Write to vault → index → embed |

## Principles

- Notes are atomic — one fact per note, don't inflate
- Wikilinks (`[[...]]`) for cross-references between notes
- `related:` in frontmatter for structured links
- Folder names: underscores (shell-friendly). Note titles: spaces (readable)
