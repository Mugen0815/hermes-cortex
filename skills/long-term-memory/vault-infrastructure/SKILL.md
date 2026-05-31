---
name: vault-infrastructure
description: Infrastructure for the hermes-cortex vault — folder structure, note types, templates, and promotion rules. Used when bootstrapping or repairing the long-term memory system.
---

# Vault Infrastructure

## Three layers

- **Vault** = curated long-term memory (Obsidian notes)
- **Sessions** = raw conversation transcripts
- **Git repo** = versioned code and templates

Do **not** put raw sessions into the vault. Do **not** put curated knowledge only in git.

## Vault structure

```
00_inbox/       Review-only candidates that need human judgment
10_facts/       Stable facts about systems, tools, environment
20_decisions/   Decisions with rationale
30_projects/    Active project contexts
40_runbooks/    Repeatable procedures, troubleshooting
50_people/      People notes
60_maps/        Index and navigation notes
80_templates/   Note templates
```

## Note types and frontmatter

Every note needs YAML frontmatter. Use the canonical enum tables in `docs/METADATA.md`; do not invent local status values. Cortex indexes notes with these fields:

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

Folder-to-type mapping: `10_facts/`=fact, `20_decisions/`=decision, `30_projects/`=project, `40_runbooks/`=runbook, `60_maps/`=map, `50_people/`=person.

## Naming convention

`<Type> - <Title>.md` example: `Decision - Use Obsidian as long-term memory.md`

No special characters besides hyphens and spaces. Shell-friendly folder names (underscores), readable note titles (spaces).

## Promotion rules

When processing session knowledge, decide where to put it:

- **→ Vault** if the item is: durable, reusable, a fact/decision/runbook/project update worth finding later
- **→ Git repo** if: setup docs, scripts, templates, reference material
- **→ Leave in sessions only** if: temporary, speculative, discarded, noisy chat

## Templates

Maintain these templates in the vault's `80_templates/`: `fact-note.md`, `decision-note.md`, `project-note.md`, `runbook-note.md`.

## Bootstrap

When setting up a new vault, seed public-safe starter notes only. Example shape:
- `60_maps/Map - Knowledge Index.md`
- `30_projects/Project - Example System.md`
- `20_decisions/Decision - Use a curated vault for long-term memory.md`

Do not put personal machine names, delivery targets, private scheduler state, or
real account identifiers into tracked seed notes or examples.

## Cortex runtime layout and lifecycle

Keep these concerns separate:

- `~/.hermes/plugins/cortex/` = plugin code/runtime Git checkout. Update with `git pull --ff-only` (and the install/sync step if the repo requires it). Do not store user config, Chroma data, chunks, or other mutable state here.
- `~/.hermes/cortex/` = profile-local Cortex config and runtime state (`config.yaml`, chunks, embeddings/vector store, graph artifacts). This location is acceptable because it keeps mutable state out of the plugin repo, but expose it clearly in UX/docs (`hermes cortex status`, `config path/show/edit`) so it does not feel hidden.
- `<workspace>/hermes-cortex/` = development checkout.

Current workflows should use `hermes cortex ...`; remove stale deployment-path instructions instead of preserving legacy compatibility.

There are two live lifecycle mechanisms to keep distinct when auditing Cortex:

1. **Nightly promotion cron** — promotes/maintains durable knowledge on a schedule.
2. **Post-vault-write maintenance** — after Jarvis changes vault notes, run `hermes cortex lifecycle maintenance` (or the equivalent index/embed/graph sequence) and verify.

## Nightly Promotion (Cronjob)

Cortex can package a scheduled Hermes job that promotes durable session knowledge
into the vault. Treat this as an example lifecycle pattern, not evidence of any
specific operator's scheduler. Tracked docs must not include real job IDs,
delivery channels, account names, or private notification targets.

The promotion pattern is canonical-first:

- clear, high-confidence facts/decisions/projects/runbooks are written directly
  to `10_facts/`, `20_decisions/`, `30_projects/`, or `40_runbooks/`
- uncertain or duplicate-sensitive items go to `00_inbox/` only when human review
  is needed
- active `00_inbox/` candidates need `status: draft`, `review_status: pending`,
  and `review_reason`
- archived source notes must not keep `promote: true`

After writing, a scheduled job should run lifecycle maintenance (index,
embeddings, graph) and deliver any summary through operator-local/private config.
For scheduler validation, prefer the scheduler/home context because arbitrary
worker profiles may show a different cron store:

```bash
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cron list
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cortex cron status
```

**Session sources:** Prefer the active Hermes session database when available.
If a maintenance prompt or legacy workflow reads session log files directly, make
that source explicit and keep channel-specific names generic, e.g. JSONL logs,
JSON session logs, and ignored API request dumps.

## Pitfalls

- Do not confuse raw session history with curated knowledge
- Do not keep the vault only in templates; create real notes too
- Do not let legacy vault paths survive a cutover
- Do not run `install.sh` as root
- **When asked to audit, fix, or geradeziehen the cortex setup:** code changes come first, documentation is the follow-up. Never produce only docs while leaving the actual project code untouched.
- **Nightly promotion cron:** Keep scheduler prompts public-safe. Do not document real job IDs, delivery targets, or private channel names in tracked repo docs.
- **Session sources:** Prefer the active Hermes session database. If reading logs directly, describe sources generically and verify that both JSONL and JSON session formats are covered when relevant.
- For systematic cortex health checks, see `references/cortex-health-check.md`.
- For config-path and hook-debugging deep-dive (search order, timing traps, log verification), see `references/cortex-config-debugging.md`.
- For setup and CLI workflow, see the repo `README.md` and `docs/CLI.md`.
