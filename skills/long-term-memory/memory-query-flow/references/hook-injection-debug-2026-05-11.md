# Hook-Injection Debug — 2026-05-11

## Problem

Der `_pre_llm_call` Hook in `cortex/hermes_plugin.py` sollte den `memory-query-flow`
Skill automatisch in den System-Prompt injecten. Der Skill war nie sichtbar.

## Diagnose

### 1. Labyrinth-CLI: Vault-Nutzung in Sessions prüfen

```python
from plugin_api import _list_journeys, _crossings_for_session
result = _list_journeys(limit=30)
# Ergebnis: 5 von 6 Sessions hatten 0 vault-Aufrufe
```

### 2. `_load_skill_content()` testen

```python
import plugin_runtime
_load_skill_content = plugin_runtime._load_skill_content
_DEFAULT_SKILL_PATH = plugin_runtime._DEFAULT_SKILL_PATH
result = _load_skill_content('')
# Ergebnis: ✅ Wird korrekt geladen, Marker vorhanden
```

### 3. `_pre_llm_call()` testen

```python
import plugin_runtime
_pre_llm_call = plugin_runtime._pre_llm_call
result = _pre_llm_call(session_id='test', user_message='Test', is_first_turn=True)
# Ergebnis: ✅ 15k chars, Skill + Vault Kontext enthalten
```

### 4. Config und Hermes-Seite prüfen

```python
cfg = _load_hooks_config()
print(cfg)  # enabled=True, load_skill=True (default)
```

### 5. Root Cause

**Zwei unabhängige Probleme:**

**A) `is_first_turn` Guard** — `_pre_llm_call` hatte:
```python
if not is_first_turn:
    return None
```
Der Hook injectierte NUR in Turn 1. In allen weiteren Turns war der Skill-Kontext weg.

**B) Hermes injectiert in User Message, nicht System Prompt** — `run_agent.py` Zeile 11936:
```
# Context is ALWAYS injected into the user message, never the
# system prompt. This preserves the prompt cache prefix
```
Der Skill war also nur in der **User Message des ersten Turns** sichtbar — nicht
im System-Prompt. Ab Turn 2 mit anderem User-Input war der Skill verschwunden.

## Fix

1. **SOUL.md** — Core Retrieval-Regeln von memory-query-flow dort abgelegt
   (Lookup-Order, Source Attribution, top_k-Pitfall, Auto-Use, Post-Write).
   SOUL.md ist Teil des System-Prompts → bleibt für die ganze Session sichtbar.

2. **`is_first_turn` Guard entfernt** — `_pre_llm_call` injectet jetzt jeden Turn
   Vault-Kontext (nicht mehr nur Turn 1).

3. **`load_skill: false`** in `~/.hermes/cortex/config.yaml` — der Skill wird nicht
   mehr automatisch geladen, da die Regeln in SOUL.md sind.

## Dateien

- `~/.hermes/SOUL.md` — Core Regeln
- `~/.hermes/cortex/config.yaml` — `load_skill: false`
- `~/hermes-workspace/hermes-cortex/plugin_runtime.py` — Hook-Fix (Dev/Runtime-Plugin)

## Lessons for Future Debugging

- Wenn ein Hook "nicht funktioniert": direkt den Python-Import und Aufruf der Funktion
  testen, nicht auf Logs oder Config vertrauen
- Hermes `run_agent.py` lesen! Der Kommentar in Zeile 11931-11940 erklärt die
  User-Message-Injection
- Labyrinth-CLI (`_crossings_for_session`) ist das beste Tool um eigenes
  Agent-Verhalten zu analysieren — Turn-Struktur, Tool-Nutzung, Fehlerraten
