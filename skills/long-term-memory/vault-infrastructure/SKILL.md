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

Every note needs YAML frontmatter. Cortex only indexes notes with these fields:

```yaml
---
type: fact | decision | project | runbook | map | person
status: active | archived | draft
tags: [tag1, tag2]
confidence: high | medium | low
importance: high | medium | low
stability: stable | evolving | deprecated
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

When setting up a new vault, create these first:
- `60_maps/Map - Knowledge Index.md`
- `30_projects/Project - Hermes VM.md`
- `20_decisions/Decision - Obsidian as long-term memory.md`

## Cortex runtime layout and lifecycle

Keep these concerns separate:

- `~/.hermes/plugins/cortex/` = plugin code/runtime Git checkout. Update with `git pull --ff-only` (and the install/sync step if the repo requires it). Do not store user config, Chroma data, chunks, or other mutable state here.
- `~/.hermes/cortex/` = profile-local Cortex config and runtime state (`config.yaml`, chunks, embeddings/vector store, graph artifacts). This location is acceptable because it keeps mutable state out of the plugin repo, but expose it clearly in UX/docs (`hermes cortex status`, `config path/show/edit`) so it does not feel hidden.
- `~/hermes-workspace/hermes-cortex/` = development checkout.

Current workflows should use `hermes cortex ...`; remove stale deployment-path instructions instead of preserving legacy compatibility.

There are two live lifecycle mechanisms to keep distinct when auditing Cortex:

1. **Nightly promotion cron** — promotes/maintains durable knowledge on a schedule.
2. **Post-vault-write maintenance** — after Jarvis changes vault notes, run `hermes cortex lifecycle maintenance` (or the equivalent index/embed/graph sequence) and verify.

## Nightly Promotion (Cronjob)

A cronjob (`hermes-cortex-nightly-promotion`, job_id `c55272c78fa26773`) runs
daily at **02:00 UTC** to promote durable knowledge from Hermes sessions into the
vault. The job is canonical-first:

- clear, high-confidence facts/decisions/projects/runbooks are written directly
  to `10_facts/`, `20_decisions/`, `30_projects/`, or `40_runbooks/`
- uncertain or duplicate-sensitive items go to `00_inbox/` only when human review
  is needed
- active `00_inbox/` candidates need `status: draft`, `review_status: pending`,
  and `review_reason`
- archived source notes must not keep `promote: true`

After writing, the job runs lifecycle maintenance (index, embeddings, graph) and
sends a Signal summary to the user. For scheduler validation, prefer the
scheduler/home context because arbitrary worker profiles may show a different
cron store:

```bash
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cron list
env -u HERMES_HOME HOME=/path/to/scheduler-home hermes cortex cron status
```

**Session sources scanned by the prompt:**
- Signal sessions: `~/.hermes/sessions/*.jsonl`
- TUI sessions: `~/.hermes/sessions/session_*.json`
- `request_dump_*.json` files are ignored (API dumps, not sessions)

**Pitfall — missing TUI sessions:** The prompt originally only searched `*.jsonl`, which catches Signal sessions but misses all TUI sessions (stored as `session_*.json`). Both globs are required. If the cronjob output shows only 1-2 sessions on a heavy workday, check whether the prompt includes the `session_*.json` glob.

## Pitfalls

- Do not confuse raw session history with curated knowledge
- Do not keep the vault only in templates; create real notes too
- Do not let legacy vault paths survive a cutover
- Do not run `install.sh` as root
- **When asked to audit, fix, or geradeziehen the cortex setup:** code changes come first, documentation is the follow-up. Never produce only docs while leaving the actual project code untouched.
- **Nightly promotion cron:** When editing the cronjob prompt, ensure it searches both `*.jsonl` (Signal) and `session_*.json` (TUI). The jsonl-only trap caused 20+ TUI sessions to be silently skipped for weeks.
- For systematic cortex health checks, see `references/cortex-health-check.md`.
- For config-path and hook-debugging deep-dive (search order, timing traps, log verification), see `references/cortex-config-debugging.md`.
- For full setup workflow beyond this skill, see the repo's `SETUP.md`.
