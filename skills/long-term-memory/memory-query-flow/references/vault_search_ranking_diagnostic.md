# vault_search Ranking-Diagnose: Warum eine existierende Note nicht gefunden wird

## Problem

vault_search meldet keine Treffer für eine **bekannt existierende** Vault-Note.

Beispiel: `vault_search("Project hermes-cortex", top_k=10)` findet die Note nicht,
obwohl sie auf der Festplatte existiert und 14 Chunks im Index hat.

## Ursache

BM25 bevorzugt **kurze, präzise Chunks** massiv. Die "Links"-Sektionen in anderen Notes
enthalten `"- [[Project - hermes-cortex]]"` — das sind ~30 Zeichen, beide Suchbegriffe
dicht beieinander, perfekter BM25-Score. Die eigentliche Project-Note hat 294 Zeilen
technischen Text — BM25 "verdünnt" darüber.

Bei top_k=10 gewinnen die kurzen Wikilink-Chunks, die Ziel-Notiz fliegt raus.

## 4-Stufen-Diagnose

### Stufe 1 — Höheres top_k

```python
vault_search("Project hermes-cortex", top_k=30)
```

Trotzdem nicht gefunden? → Stufe 2.

### Stufe 2 — Dateisystem-Ebene

```python
search_files(pattern="*hermes-cortex*", target="files")
# → ["/home/.../vault/30_projects/Project - hermes-cortex.md"]
```

Liefert den Dateipfad, **umgeht den Index komplett**. Der schnellste Weg um zu
prüfen ob eine Datei existiert.

### Stufe 3 — Index-Prüfung

```bash
grep '"file": "30_projects/Project - hermes-cortex.md"' ~/.hermes/cortex/chunks.jsonl | wc -l
# → 14 (Chunks vorhanden, Datei ist indiziert)
```

Wenn 0: Datei wurde beim letzten `cortex index` übersprungen (Frontmatter-Probleme,
Ausschluss-Regel, zu große Datei?).

### Stufe 4 — Akzeptieren + Direktzugriff

```python
vault_read_note(file="30_projects/Project - hermes-cortex.md")
```

Wenn die Datei indiziert ist (Stufe 3 bestätigt), aber nicht rankt: **Limitation
akzeptieren**. BM25 + Embedding-Vector können eine lange Note nicht zuverlässig
finden, wenn kurze Wikilink-Chunks dominieren. Direktzugriff ist der Weg.

## Ranking-Detail

Ausgabe von `cortex search "Project hermes-cortex" --top-k 30`:

```
   1. [0.0237] Fact - Cortex Hermes Plugin Entry-Point Integration.md :: Links
      (bm25#8, vec#4)           ← "- [[Project - hermes-cortex]]" (kurz!)
   2. [0.0221] Fact - Cortex Graph Diagnostics... :: Links
      (bm25#11, vec#10)         ← "- [[Project - hermes-cortex]]" (kurz!)
   ...
   7. [0.0183] Project - Hermes VM.md :: 4. Cortex / 4.1 Projekt
      (bm25#33, vec#19)
   ...
   Project - hermes-cortex.md : NICHT IN TOP-30
```

Die Scores liegen alle dicht beieinander (0.018–0.024). Die kurzen Wikilink-Chunks
gewinnen knapp, weil BM25 dort perfekt matched.

## Prävention

- **Immer `top_k=20+`** bei Suche nach einer bestimmten Notiz
- Bei Namenssuche: `search_files(target="files")` als ersten Schritt, nicht vault_search
- vault_search ist gut für **semantische Suche** — "was weißt du über X?"
- vault_search ist **schlecht** für "existiert Datei Y?" — dafür Dateisystem nehmen

## Links

- `memory-query-flow` Skill → Abschnitt "Failure #3 — vault_search false negative"
- `cortex.config._CONFIG_SEARCH_PATHS` in `src/cortex/config.py`
- `cortex.plugin._resolve_state()` in `src/cortex/plugin.py`
