# Vault Metadata

Cortex uses YAML frontmatter for routing, filtering, boosts, and graph
diagnostics. The Markdown note remains readable without Cortex; the metadata just
makes retrieval less stupid.

## Minimal frontmatter

```yaml
---
type: fact | decision | project | runbook
status: active | archived | draft | superseded
tags: [memory, retrieval]
confidence: high        # or numeric 0..1
importance: medium      # or numeric 1..5
stability: stable | evolving | deprecated
---
```

## Recommended frontmatter

```yaml
---
type: fact
status: active
created: 2026-04-27
updated: 2026-04-27
last_verified: 2026-04-27

domain: memory
project: hermes-cortex
tags: [retrieval, obsidian]
aliases: [Alternative Title]

source: manual
related:
  - "[[Other Note]]"

confidence: high
importance: medium
stability: stable
---
```

## Field reference

| Field | Purpose | Used by |
|---|---|---|
| `type` | Routes notes and enables type filters | indexer, search, promotion |
| `status` | Active/draft/archived/superseded lifecycle | search, lifecycle |
| `created` | Original note creation date | audit/history |
| `updated` | Last note edit date | diagnostics |
| `last_verified` | Last confirmed-correct date | recency boost, stale review |
| `domain` | Broad subject area | search filter |
| `project` | Project scope | search filter |
| `tags` | Topic filters | search, graph |
| `aliases` | Alternative note titles | graph resolver |
| `source` | Provenance: manual/session/external/etc. | audit |
| `related` | Wikilinks to related notes | graph |
| `confidence` | Trust signal; accepts `low/medium/high` or numeric `0..1` | filters/diagnostics |
| `importance` | Retrieval boost signal; accepts `low/medium/high` or numeric `1..5` | search boost |
| `stability` | `stable`, `evolving`, or `deprecated` | search filter |

## Folder mapping

| Folder | Type / purpose |
|---|---|
| `00_inbox/` | review-only candidates that need human judgment |
| `10_facts/` | `fact` |
| `20_decisions/` | `decision` |
| `30_projects/` | `project` |
| `40_runbooks/` | `runbook` |
| `60_maps/` | map/index notes |
| `80_templates/` | templates, not normal content |

## `00_inbox/` review-candidate contract

The nightly promotion lifecycle is canonical-first: if knowledge is clear and
high-confidence, write it directly to the canonical folder. Use `00_inbox/` only
when a human decision is genuinely needed.

Active inbox candidates should have review metadata like:

```yaml
status: draft
review_status: pending
review_reason: "why human review is needed"
promote: true
promote_type: fact | decision | project | runbook
```

After a source candidate has been promoted and archived, it must not stay
eligible for promotion. This state is invalid:

```yaml
status: archived
promote: true
```

Use `promote: false` with `promoted_to: "[[Target Note]]"`, or remove promotion
fields entirely, depending on the cleanup path.

## Normalization

Cortex accepts human-friendly values and normalizes them internally:

| Input | Internal meaning |
|---|---|
| `confidence: low` | low trust |
| `confidence: medium` | medium trust |
| `confidence: high` | high trust |
| `importance: low` | low retrieval boost |
| `importance: medium` | medium retrieval boost |
| `importance: high` | high retrieval boost |

Numeric values are also accepted:

- `confidence`: `0..1`
- `importance`: `1..5`

Missing confidence or importance is neutral. It does not get a phantom boost.

## Templates

Seed templates live in:

```text
cortex/_seed/templates/
```

They are copied into new vaults by:

```bash
cortex init --yes
```
