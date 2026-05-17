---
name: cortex-kanban-worker
description: "Vault-gestützte Recherche für Kanban-Worker — nutzt hermes-cortex Tools (vault_search, vault_read_note, vault_build_context) um Wissen aus dem Obsidian Vault in Kanban-Tasks zu integrieren."
version: 1.0.0
tags:
  - kanban
  - cortex
  - vault
  - research
  - worker
---

# Cortex Kanban Worker — Vault-gestützte Task-Bearbeitung

> Dieser Skill wird automatisch geladen, wenn Hermes einen Kanban-Worker mit aktivem Cortex-Plugin startet (`HERMES_KANBAN_TASK` gesetzt).

## Überblick

Als Kanban-Worker mit Cortex-Tools kannst du während der Bearbeitung eines Kanban-Tasks auf das gesamte Obsidian Vault zugreifen — bestehendes Wissen abrufen, recherchieren und Ergebnisse strukturieren.

## Workflow

### 1. Task lesen + Kontext laden

```python
# Immer der erste Schritt: Task-Details abrufen
task = kanban_show()
title = task["title"]
body = task.get("body", "")

# Relevantes Vault-Wissen zum Task-Thema laden
context = vault_build_context(
    query=f"{title} {body}",
    budget=1500,        # ~1500 Tokens Kontext
)
# context.text enthält gerankte Vault-Auszüge
```

### 2. Gezielte Recherche

```python
# Suche nach spezifischen Informationen
results = vault_search(
    query="deine suchfrage aus dem task-kontext",
    top_k=10,
    filters={"type": ["fact", "decision"]},  # optional: auf Fact/Decision-Typen filtern
)
for r in results["results"]:
    note = vault_read_note(file=r["file"])
    print(note["content"])  # vollständiger Notiztext
```

### 3. Ergebnisse festhalten

```python
kanban_complete(
    summary="Recherche abgeschlossen — 3 relevante Vault-Notizen gefunden und ausgewertet",
    metadata={
        "vault_notes_consulted": [
            "10_facts/example.md",
            "30_projects/example.md",
        ],
        "vault_search_queries": ["deine suchfrage"],
        "findings": "kurze zusammenfassung der ergebnisse",
    },
)
```

## Patterns

### Pattern A: Task als Research-Auftrag

```
Task: "Recherchiere X und fasse Ergebnisse zusammen"
  → vault_build_context(query="X", budget=2000)
  → vault_search(query="X", top_k=5)
  → Für jede relevante Note: vault_read_note()
  → Ergebnisse in kanban_complete(metadata={...})
```

### Pattern B: Task mit Vault-Kontext erweitern

```
Task: "Entwickle Feature Y"
  → vault_build_context(query="Feature Y Architektur", budget=2000)
  → vault_search(query="Feature Y ähnliche Projekte")
  → vault_read_note für relevante Architekturentscheidungen
  → Nutze gefundenes Wissen für die Implementierung
```

### Pattern C: Entscheidungshilfe aus dem Vault

```
Task: "Entscheide zwischen Option A und B"
  → vault_search(query="Option A Vergleich", filters={"type": ["decision"]})
  → vault_search(query="Option B Erfahrungen")
  → vault_read_note für gefundene Entscheidungen
  → Ergebnisse in die Task-Begründung einfließen lassen
```

## Filters (für vault_search / vault_build_context)

| Filter | Werte | Wirkung |
|--------|-------|---------|
| `type` | `fact`, `decision`, `project`, `runbook`, `map`, `task` | Nur Notes eines Typs |
| `tags_any` | `["cortex", "hermes"]` | Notes die eines der Tags haben |
| `tags_all` | `["cortex", "kanban"]` | Notes die ALLE Tags haben |
| `domain` | `"development"`, `"infrastructure"` | Notes einer Domain |
| `project` | `"hermes-cortex"` | Notes eines Projekts |
| `importance_min` | `3` | Nur Notes mit importance ≥ 3 |

## Hinweise

- Der Cache wird beim Session-Start vorgewärmt — der erste `vault_search`-Aufruf ist trotzdem etwas langsamer
- `vault_build_context` ist der effizienteste Weg, einen Überblick zu bekommen (ein Aufruf, token-budgetiert)
- `vault_search` + `vault_read_note` sind besser für Deep-Dives in einzelne Notes
- Results enthalten `scores` und `ranks` — das Ranking ist Hybrid (BM25 + Vector + Graph)
