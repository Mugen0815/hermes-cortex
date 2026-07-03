---
type: fact
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27
domain: meta
project: hermes-cortex
tags: [vault, schema, frontmatter, conventions]
aliases: [Frontmatter Schema, Note Schema]
source: manual
related:
  - '[[Jarvis Memory Architecture]]'
  - '[[Project - hermes-cortex]]'
confidence: high
importance: high
stability: evolving
---

# Vault Schema

## Summary
Every note carries YAML frontmatter combining classic Obsidian fields
(`type`, `status`, `created`, `updated`, `tags`, `aliases`, `source`,
`related`, `domain`) with cortex retrieval signals (`confidence`,
`importance`, `stability`, `last_verified`).

## Required for indexing
`type`, `status`, `tags`, `confidence`, `importance`, `stability`

## Folder routing
| Folder | `type` |
|---|---|
| `10_facts/` | `fact` |
| `20_decisions/` | `decision` |
| `30_projects/` | `project` |
| `40_runbooks/` | `runbook` |
| `50_people/` | `person` |
| `60_maps/` | `map` |
| `00_inbox/` | draft/review-only candidates, excluded from curated answers by default |
| `80_templates/` | templates/scaffolding, excluded from curated answers by default |
| `raw/` | immutable source/provenance material, excluded from curated answers by default |

Root files (`SCHEMA.md`, `index.md`, `log.md`) are llm-wiki-compatible
orientation/operator files in the configured Vault root. They are operational by
default, not curated answer chunks.

## Raw provenance
Raw files may include frontmatter with `source_url`, `ingested`, and a body-only
`sha256` so `hermes cortex wiki-health` can report drift without mutating sources.

## Templates
Located in `80_templates/` — copy and adjust:
- `fact-note.md`
- `decision-note.md`
- `project-note.md`
- `runbook-note.md`

## Authoritative reference
`docs/METADATA.md` in the cortex repo.

## Related
- [[Project - hermes-cortex]]
