---
name: vault-infrastructure
description: "Use when bootstrapping, auditing, or repairing a Cortex/Obsidian vault: folder layout, note types, frontmatter, naming, promotion rules, seed hygiene, and post-write maintenance."
---

# Vault Infrastructure

## Purpose

Use this skill for the **curated Vault layer** of Cortex memory: how notes are
organized, how durable knowledge is promoted, and how vault changes are verified.

This skill is intentionally not a deployment or incident-debugging guide. Put
rare diagnostics in `memory-diagnostics` or a reference file instead of polluting
this hotpath.

## Layers

- **Vault** — the single configured Cortex root from `vault.path`; it contains
  curated long-term memory plus llm-wiki-compatible root/raw material.
- **Sessions** — raw conversation history and temporary evidence. Do not copy raw
  transcripts into the Vault.
- **Runtime state** — Cortex config, chunks, embeddings, and generated graph data.
  Keep this separate from curated notes.

## Vault structure

```text
00_inbox/       Review-only candidates that need human judgment
10_facts/       Stable facts about systems, tools, environment
20_decisions/   Decisions with rationale
30_projects/    Active project contexts
40_runbooks/    Repeatable procedures and troubleshooting
50_people/      People notes
60_maps/        Index and navigation notes
80_templates/   Note templates / scaffolding (excluded from curated answers)
raw/           Immutable source/provenance material (excluded from curated answers)
SCHEMA.md      Vault conventions and provenance rules
index.md       Operator-facing catalog/map
log.md         Append-only operator/lifecycle history
```

Do not introduce a second durable wiki path. `WIKI_PATH` is only an init/default
input for otherwise unconfigured installs; runtime Cortex work uses configured
`vault.path`.

## Note types and frontmatter

Every curated note needs YAML frontmatter. Use the canonical enum tables in
`docs/METADATA.md`; do not invent local status values.

```yaml
---
type: fact | decision | project | runbook | map | person | note
status: active | draft | archived | deprecated | stale | superseded
tags: [tag1, tag2]
confidence: high | medium | low
importance: high | medium | low
stability: stable | evolving | experimental
last_verified: YYYY-MM-DD
---
```

Folder-to-type mapping:

| Folder | Type |
|---|---|
| `10_facts/` | `fact` |
| `20_decisions/` | `decision` |
| `30_projects/` | `project` |
| `40_runbooks/` | `runbook` |
| `50_people/` | `person` |
| `60_maps/` | `map` |

## Naming convention

Use readable, stable note names:

```text
<Type> - <Title>.md
```

Examples:

- `Fact - Matrix Gateway Home Room.md`
- `Decision - Use Obsidian as long-term memory.md`
- `Runbook - Rebuild Cortex index.md`

Avoid special characters beyond spaces and hyphens. Keep titles descriptive
enough to identify the note without opening it.

## Promotion rules

Promote information into the Vault only when it is durable and useful later.

| Source item | Target |
|---|---|
| Stable system/tool/environment fact | `10_facts/` |
| Decision with rationale or tradeoffs | `20_decisions/` |
| Active project context or roadmap | `30_projects/` |
| Repeatable procedure or troubleshooting workflow | `40_runbooks/` |
| Person preference/profile detail suitable for curated notes | `50_people/` |
| Navigation/index material | `60_maps/` |
| Uncertain candidate needing review | `00_inbox/` |
| Temporary task status, raw chat, stale IDs, speculative notes | leave in sessions |

Prefer updating an existing matching note over creating a near-duplicate.

## Templates and seed notes

Maintain canonical templates in `80_templates/`:

- `fact-note.md`
- `decision-note.md`
- `project-note.md`
- `runbook-note.md`

Seed notes must be public-safe and generic. Do not include real account IDs,
private delivery targets, machine-specific secrets, or operator-only automation
state in shipped seed notes.

## Post-write maintenance

After creating or editing curated Vault notes:

1. Validate frontmatter when the workflow provides a validator.
2. Run `hermes cortex index`.
3. Run `hermes cortex embed`.
4. Re-run graph/lifecycle maintenance when links, aliases, or maps changed.
5. Verify the updated note is discoverable with `vault_search` or the Cortex CLI.

`hermes cortex index` and `hermes cortex embed` read the vault path from the active Cortex config; they do not take a vault path argument. Prefer `hermes cortex lifecycle maintenance` when available; otherwise run index, embed, and graph build as applicable.

Run `hermes cortex wiki-health` for a read-only check of missing root files, raw
folders, curated-source exclusions (`raw`, `00_inbox`, `80_templates`), and raw
source frontmatter/hash drift.

## Common pitfalls

- Do not confuse raw sessions with curated knowledge.
- Do not index `raw/`, `00_inbox/`, or `80_templates` as default curated answer sources.
- Do not create or document `wiki.path`, blind wiki-directory fallback, or a second durable Vault.
- Do not create duplicate notes when an existing note can be updated.
- Do not invent frontmatter status values; use the canonical metadata enums.
- Do not store generated index, embedding, graph, cache, or runtime config files
  inside the curated Vault notes tree.
- Do not put operator-specific private data into shipped templates or seed notes.

## Verification checklist

- [ ] Note is in the correct folder for its `type`.
- [ ] Frontmatter is present and uses canonical enum values.
- [ ] Existing notes were checked before creating a new one.
- [ ] Public/shared seed content contains no private operator details.
- [ ] `wiki-health` passes or any root/raw/source-exclusion warnings are intentional and reported.
- [ ] Index and embeddings were refreshed after Vault writes.
- [ ] Important notes are linked from a relevant map when discoverability matters.
