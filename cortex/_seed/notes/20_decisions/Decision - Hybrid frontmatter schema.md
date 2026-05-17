---
type: decision
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27
domain: architecture
project: hermes-cortex
tags: [decision, vault, schema]
aliases: []
source: session
related:
  - '[[Vault Schema]]'
  - '[[Project - hermes-cortex]]'
confidence: high
importance: high
stability: stable
---

# Decision - Hybrid frontmatter schema

## Context
The pre-existing vault used a classic Obsidian frontmatter
(`type`, `status`, `created`, `updated`, `domain`, `tags`, `aliases`,
`source`, `related`). The new cortex retrieval system needs additional
signals (`confidence`, `importance`, `stability`, `last_verified`).

## Options Considered
1. **Replace** — enforce cortex-only schema; rewrite all 33 existing notes.
   Pros: clean. Cons: throws away useful classic fields, breaks Obsidian habits.
2. **Adopt existing** — use only the classic schema; cortex makes do without retrieval signals.
   Pros: zero migration. Cons: loses confidence/importance/stability — core retrieval features.
3. **Hybrid** — keep classic fields, add cortex retrieval fields.
   Pros: best of both, no information loss. Cons: more fields to maintain.

## Decision
Option 3 — hybrid schema.

## Consequences
- Templates and `docs/METADATA.md` document the full hybrid schema.
- Existing vault stays operational on the old schema in parallel.
- New cortex-managed vault at `~/hermes-workspace/vault/` uses hybrid from day one.
- Migration of old notes happens lazily — when cortex touches a note,
  it upgrades the frontmatter.

## Related
- [[Vault Schema]]
- [[Project - hermes-cortex]]
