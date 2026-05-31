Führe den read-only Cortex WeeklyReview aus und liefere den Bericht als Markdown.

## Vault
Pfad: {vault_path}
Cortex CLI: {cortex_bin}

## Cron-Konfiguration
- Review-/Prompt-Zeitzone: {timezone}
- Output-Format: {output_format}
- Dry-run Label: {dry_run}
- Stale-Schwellwert: {stale_days} Tage
- Mindest-Importance für stale Review: {stale_min_importance}
- Konsolidierungs-Mindestgrad: {consolidation_min_degree}

Hinweis zur Zeitplanung: Die obige Zeitzone steuert diesen Prompt/Report. Die Ausführungszeit des Cron-Ausdrucks wird vom Hermes-Scheduler anhand der Hermes-Runtime-Konfiguration interpretiert.

## Ausführen
Führe exakt diesen read-only Befehl aus:

```bash
{weekly_command}
```

WeeklyReview darf keine Vault-Notes, Graph-Artefakte, Chunks, Embeddings oder Viewer-Dateien verändern. `--dry-run` ist hier nur ein Label/Intent-Flag; der WeeklyReview bleibt immer read-only.

## Antwortformat
Liefere die Ausgabe des Befehls als Markdown-Bericht. Der Bericht muss Summary/Graph Stats, Duplicates, stale High-Importance Notes, Broken References, Consolidation Proposals, Orphan Nodes, Contradictions sowie Duration/Error enthalten.
