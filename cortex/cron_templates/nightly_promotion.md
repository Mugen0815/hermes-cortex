Analyze Hermes sessions from the last {lookback_days} day(s) ({timezone}) and extract durable knowledge.

The pipeline has three stages:
1. Analyze sessions and choose the target folder first.
2. Write high-confidence knowledge directly to canonical Vault folders without `promote: true`.
3. Use `00_inbox/` only for uncertain items that need human review. Cortex lifecycle/maintenance runs afterward.

## Vault
Path: {vault_path}
Cortex CLI: {cortex_bin}

## Cron configuration
- Lookback: last {lookback_days} day(s)
- Lookback/prompt timezone: {timezone}
- Primary SessionDB: `{state_db_path}`
- Legacy fallback enabled: {legacy_fallback_enabled}
- Legacy session globs:
{session_globs_block}

Scheduling note: the timezone above controls this prompt/lookback window. The Hermes scheduler interprets the cron expression runtime using the Hermes runtime configuration.

## Choose the target folder first
| Content | Target |
|---|---|
| Stable system facts, tool findings, details | `10_facts/` |
| Decisions with rationale | `20_decisions/` |
| Active project context or project status | `30_projects/` |
| Repeatable workflows, troubleshooting, operator steps | `40_runbooks/` |
| Uncertain / contradictory / needs human review | `00_inbox/` |

## Write canonical notes by default
When a finding is durable, relevant, and high-confidence, write or update the matching `.md` note directly in `10_facts/`, `20_decisions/`, `30_projects/`, or `40_runbooks/`.

Frontmatter example for canonical notes:

```yaml
---
type: fact|decision|runbook|project
status: active
title: "Descriptive title"
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [tag1, tag2]
aliases: [alternate_name]
source: session
source_sessions: [session_id_1]
confidence: medium|high
importance: 1|2|3|4|5
stability: stable|evolving
---
```

Important: canonical notes must not include `promote: true`, `cortex_promote: true`, or `promote_type`.

## Write inbox candidates only for review cases
Only when a finding is uncertain, conflict-prone, duplicate-sensitive, or requires human judgment, write it to `00_inbox/` with review metadata:

```yaml
---
type: fact|decision|runbook|project
status: draft
review_status: pending
review_reason: "Why this note needs human review"
promote: true
promote_type: fact|decision|runbook|project
title: "Descriptive title"
created: YYYY-MM-DD
tags: [tag1, tag2]
aliases: [alternate_name]
source: session
source_sessions: [session_id_1]
confidence: medium
importance: 1|2|3|4|5
stability: evolving
---
```

Never use `status: review`; known Cortex status values are `active`, `draft`, `archived`, `deprecated`, `stale`, and `superseded`.
Never use `status: active` for live inbox candidates; live inbox candidates use `status: draft` plus `review_status: pending`.

## Steps

1. **Load sessions deterministically:** Run exactly this command first and use its JSON output as the session input for the analysis:
   ```bash
   {session_source_command}
   ```
   - The primary source is Hermes SessionDB (`state.db`) in read-only mode.
   - Legacy JSON/JSONL files are only a fallback when `state.db` is missing, unreadable, schema-incompatible, or contains no sessions in the lookback window.
   - Ignore `request_dump_*.json`; the loader counts these files but never parses them.
   - Use `diagnostics` from the loader JSON in the final report.

2. **Analyze** every item in `sessions[]` and extract durable knowledge.

3. **Check for duplicates:** Search the full Vault (`find {vault_path} -name '*.md'`) for existing notes about the same topic. If one exists, skip the item or update the existing canonical note without promotion flags.

4. **Write notes:**
   - High-confidence: write directly to the matching canonical target folder.
   - Uncertain / needs review: write to `00_inbox/` with `status: draft`, `review_status: pending`, `review_reason`, `promote: true`, and `promote_type`.

5. **After writing:** Run the Cortex pipeline:
   ```bash
   {lifecycle_commands}
   ```

6. **Ignore:** temporary task progress, completed TODOs, and chat noise without durable value.

7. **Final summary** as the final response (delivered automatically):
```
🧠 Nightly Knowledge Promotion

Sessions analyzed: N
Source: backend=<state_db|legacy_files>, fallback=<true|false>, reason=<fallback_reason>, ignored_request_dump=<N>
Canonical notes written: N | Notes in 00_inbox: N | updated: N | duplicates skipped: N

New/updated canonical notes:
- [Folder/Type] Title
- ...

New 00_inbox notes (needs review):
- [Folder/Type] Title — reason
- ...

No durable knowledge found: (if applicable)
```

If no sessions were found in the lookback window, respond with "🧠 Nightly Promotion: No sessions found in the lookback window."
If the sessions contain only chat noise without durable value, respond with "[SILENT]"
