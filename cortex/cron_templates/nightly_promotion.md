Analysiere Hermes-Sessions aus den letzten {lookback_days} Tag(en) ({timezone}) und extrahiere dauerhaft relevantes Wissen.

Die Pipeline läuft in 3 Stufen:
1. **Du** analysierst Sessions und entscheidest zuerst den Zielordner.
2. High-confidence Wissen schreibst du direkt in kanonische Vault-Ordner — ohne `promote: true`.
3. `00_inbox/` nutzt du nur für unsichere Fälle, die menschliches Review brauchen. Danach läuft Cortex Lifecycle/Maintenance.

## Vault
Pfad: {vault_path}
Cortex CLI: {cortex_bin}

## Cron-Konfiguration
- Lookback: letzte {lookback_days} Tag(e)
- Lookback-/Prompt-Zeitzone: {timezone}
- SessionDB primär: `{state_db_path}`
- Legacy-Fallback aktiviert: {legacy_fallback_enabled}
- Legacy-Session-Globs:
{session_globs_block}

Hinweis zur Zeitplanung: Die obige Zeitzone steuert diesen Prompt/Lookback. Die Ausführungszeit des Cron-Ausdrucks wird vom Hermes-Scheduler anhand der Hermes-Runtime-Konfiguration interpretiert.

## Zielordner zuerst wählen
| Inhalt | Ziel |
|---|---|
| Stabile Systemfakten, Tool-Erkenntnisse, Details | `10_facts/` |
| Entscheidungen mit Begründung | `20_decisions/` |
| Aktive Projektkontexte oder Projektstatus | `30_projects/` |
| Wiederholbare Abläufe, Troubleshooting, Operator-Schritte | `40_runbooks/` |
| Unsicher / widersprüchlich / braucht Human Review | `00_inbox/` |

## Kanonische Notes schreiben (Standardfall)
Wenn die Erkenntnis dauerhaft relevant und mit hoher Sicherheit einordenbar ist, schreibe oder ergänze direkt die passende `.md` Note in `10_facts/`, `20_decisions/`, `30_projects/` oder `40_runbooks/`.

Frontmatter-Beispiel für kanonische Notes:

```yaml
---
type: fact|decision|runbook|project
status: active
title: "Beschreibender Titel"
created: YYYY-MM-DD
updated: YYYY-MM-DD
tags: [tag1, tag2]
aliases: [alternativer_name]
source: session
source_sessions: [session_id_1]
confidence: medium|high
importance: 1|2|3|4|5
stability: stable|evolving
---
```

Wichtig: Kanonische Notes bekommen kein `promote: true`, kein `cortex_promote: true` und kein `promote_type`.

## Inbox-Kandidaten schreiben (nur Reviewfälle)
Nur wenn eine Erkenntnis unsicher, konfliktverdächtig, duplikat-sensitiv oder menschlich zu entscheiden ist, schreibe sie nach `00_inbox/` mit Review-Metadaten:

```yaml
---
type: fact|decision|runbook|project
status: draft
review_status: pending
review_reason: "Warum diese Note menschliches Review braucht"
promote: true
promote_type: fact|decision|runbook|project
title: "Beschreibender Titel"
created: YYYY-MM-DD
tags: [tag1, tag2]
aliases: [alternativer_name]
source: session
source_sessions: [session_id_1]
confidence: medium
importance: 1|2|3|4|5
stability: evolving
---
```

Verwende niemals `status: review`; bekannte Cortex-Statuswerte sind `active`, `draft`, `archived`, `deprecated`, `stale`, `superseded`.
Verwende niemals `status: active` für live Inbox-Kandidaten; live Inbox-Kandidaten sind `status: draft` + `review_status: pending`.

## Schritte

1. **Sessions deterministisch laden:** Führe zuerst exakt diesen Befehl aus und nutze die JSON-Ausgabe als Session-Eingabe für die Analyse:
   ```bash
   {session_source_command}
   ```
   - Primär wird Hermes SessionDB (`state.db`) read-only gelesen.
   - Legacy JSON/JSONL-Dateien sind nur Fallback, wenn `state.db` fehlt, unlesbar/schema-inkompatibel ist oder keine Sessions im Lookback enthält.
   - Ignoriere `request_dump_*.json`; diese Dateien werden vom Loader gezählt, aber nie geparst.
   - Nutze `diagnostics` im Loader-JSON für den finalen Report.

2. **Analysiere** jede Session aus `sessions[]` und extrahiere dauerhaft relevantes Wissen.

3. **Prüfe auf Duplikate:** Durchsuche das gesamte Vault (`find {vault_path} -name '*.md'`) nach existierenden Notes zum selben Thema. Falls bereits vorhanden: überspringe oder ergänze die existierende kanonische Note ohne Promotion-Flags.

4. **Schreibe Notes:**
   - High-confidence: direkt in den passenden kanonischen Zielordner.
   - Unsicher / Review nötig: nach `00_inbox/` mit `status: draft`, `review_status: pending`, `review_reason`, `promote: true`, `promote_type`.

5. **Nach dem Schreiben:** Führe die Cortex-Pipeline aus:
   ```bash
   {lifecycle_commands}
   ```

6. **Ignoriere:** temporären Task-Fortschritt, erledigte TODOs, Chat-Noise ohne Dauerwert.

7. **Finale Zusammenfassung** als finale Antwort (wird automatisch ausgeliefert):
```
🧠 Nightly Knowledge Promotion

Sessions analysiert: N
Quelle: backend=<state_db|legacy_files>, fallback=<true|false>, reason=<fallback_reason>, ignored_request_dump=<N>
Kanonisch geschrieben: N | Notes in 00_inbox: N | aktualisiert: N | Duplikate übersprungen: N

Neu/aktualisiert kanonisch:
- [Folder/Type] Titel
- ...

Neu in 00_inbox (Review nötig):
- [Folder/Type] Titel — Grund
- ...

Nichts Dauerhaftes gefunden: (falls zutreffend)
```

Falls keine Sessions im Lookback-Zeitraum gefunden wurden: antworte mit "🧠 Nightly Promotion: Keine Sessions im Lookback-Zeitraum gefunden."
Falls nur Chat-Noise ohne Dauerwert: antworte mit "[SILENT]"
