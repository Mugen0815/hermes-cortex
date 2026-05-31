Run the read-only Cortex WeeklyReview and deliver the report as Markdown.

## Vault
Path: {vault_path}
Cortex CLI: {cortex_bin}

## Cron configuration
- Review/prompt timezone: {timezone}
- Output format: {output_format}
- Dry-run label: {dry_run}
- Stale threshold: {stale_days} days
- Minimum importance for stale review: {stale_min_importance}
- Minimum degree for consolidation: {consolidation_min_degree}

Scheduling note: the timezone above controls this prompt/report. The Hermes scheduler interprets the cron expression runtime using the Hermes runtime configuration.

## Execute
Run exactly this read-only command:

```bash
{weekly_command}
```

WeeklyReview must not modify Vault notes, graph artifacts, chunks, embeddings, or viewer files. `--dry-run` is only a label/intent flag here; WeeklyReview remains read-only.

## Response format
Deliver the command output as a Markdown report. The report must include Summary/Graph Stats, Duplicates, stale High-Importance Notes, Broken References, Consolidation Proposals, Orphan Nodes, Contradictions, and Duration/Error.
