# Vault Metadata

Cortex uses YAML frontmatter for routing, filtering, boosts, and graph
diagnostics. The Markdown note remains readable without Cortex; the metadata just
makes retrieval less stupid.

`docs/METADATA.md` is the public, canonical schema reference. Skills, templates,
seed notes, and examples should refer back here instead of carrying their own
status vocabulary. The runtime source of truth is `cortex/frontmatter.py`.

## Minimal frontmatter

```yaml
---
type: fact | decision | project | runbook | map | person | note
status: active | draft | archived | deprecated | stale | superseded
tags: [memory, retrieval]
confidence: high        # or numeric 0..1
importance: medium      # or numeric 1..5
stability: stable | evolving | experimental
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

## Canonical enums

Cortex warns about unknown enum values but keeps them as-is so old vaults remain
readable. Public docs and examples should use only the canonical values below.

### `type`

| Value | Meaning |
|---|---|
| `fact` | Stable fact or concept |
| `decision` | Decision plus rationale/tradeoffs |
| `project` | Project context, state, or roadmap |
| `runbook` | Repeatable operational procedure or troubleshooting guide |
| `map` | Index/navigation note |
| `person` | Person/contact note |
| `note` | Generic note when a narrower type does not fit |

### `status`

| Value | Meaning | Typical use |
|---|---|---|
| `active` | Current and intended for normal retrieval | Canonical facts, live projects/runbooks, current maps |
| `draft` | Work in progress or pending review | `00_inbox/` review candidates, unfinished notes |
| `archived` | Retired historical record | Completed projects, promoted source notes, old incidents |
| `deprecated` | Still informative but should not guide new work | Old APIs, superseded workflows kept for context |
| `stale` | Likely outdated and needs review before use | Notes flagged by review jobs or manual audit |
| `superseded` | Replaced by a newer note/decision | Older decisions with `superseded_by`/related links |

Do **not** use workflow labels like `proposed`, `planned`, `approved`,
`implemented`, or `review` in `status`. Put those in explicit fields such as
`review_status`, `lifecycle_status`, `decision_state`, or `roadmap_phase`.

### `stability`

| Value | Meaning |
|---|---|
| `stable` | Unlikely to change without an explicit migration |
| `evolving` | Current but expected to change |
| `experimental` | Trial/early design; validate before relying on it |

Do **not** use `deprecated` or `draft` as `stability`; those are lifecycle
signals and belong in `status`.

## Field reference

| Field | Purpose | Used by |
|---|---|---|
| `type` | Routes notes and enables type filters | indexer, search, promotion |
| `status` | Canonical lifecycle state; see table above | search, lifecycle, graph diagnostics |
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
| `stability` | Design maturity: `stable`, `evolving`, or `experimental` | search filter |

## Folder mapping

| Folder | Type / purpose |
|---|---|
| `00_inbox/` | review-only candidates that need human judgment |
| `10_facts/` | `fact` |
| `20_decisions/` | `decision` |
| `30_projects/` | `project` |
| `40_runbooks/` | `runbook` |
| `50_people/` | `person` |
| `60_maps/` | `map` / index notes |
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
