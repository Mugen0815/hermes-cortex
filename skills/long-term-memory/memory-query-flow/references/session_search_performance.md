# session_search Performance & Tuning

> Diagnosed 2026-05-07: User reported session_search getting slower over time.

## Architecture (Latency Flow)

```
User query → FTS5 DB search (~0.1s) → Load sessions from DB (~0.05s)
  → Format & truncate text (up to 100K chars/session)
  → SUMMARIZE each session via LLM ← THIS IS THE BOTTLENECK
  → Return results
```

## Bottleneck: LLM Summarization, NOT the DB

- **FTS5 query:** ~0.1s on 202 sessions / 7.322 messages / 85MB state.db
- **LLM calls:** 3 parallel `async_call_llm()` calls to the configured `auxiliary.session_search` model
- **Each call sends up to 100.000 chars** of conversation transcript for summarization
- **Timeout:** 360s (configurable via `auxiliary.session_search.timeout`)
- **Total latency dominated by the SLOWEST of the parallel LLM calls** (typically 5-15s each on OpenRouter)

## Why This Matters

| Claim | Reality |
|-------|---------|
| "N100 is too slow for the DB" | DB query is ~0.1s — CPU is irrelevant |
| "More sessions = slower search" | FTS5 scales well; but more matching sessions = more LLM calls |
| "Session uses a DB, should be instant" | DB part IS instant; LLM summarization is the hidden cost |

## Config Tuning

### In `~/.hermes/config.yaml`:

```yaml
auxiliary:
  session_search:
    # CHEAPER/FASTER MODEL = faster summarization
    # From: deepseek/deepseek-v4-flash (what the main chat uses)
    # To e.g.: google/gemini-2.0-flash-lite, cohere/command-r7
    provider: openrouter
    model: google/gemini-2.0-flash-lite  # Much faster for text summarization
    timeout: 360
    max_concurrency: 3  # Max parallel LLM calls [1-5]
```

### What each knob does:

| Setting | Effect |
|---------|--------|
| `model` | **Biggest lever** — cheaper/faster model = dramatically less latency |
| `provider` | **Direct API (DeepSeek) vs Relay (OpenRouter)** — OpenRouter addiert mindestens einen HTTP-Hop und ggf. Queue-Zeit. Bei 3 parallelen Calls à ~100K Tokens: bis zu 5-15s extra pro Call. |
| `max_concurrency: 1` | Sequential summarization, predictable but slower with 3+ matches |
| `max_concurrency: 5` | More parallelism, but still bound by the slowest call |
| `timeout` | How long to wait before giving up (default: 360s) |

**Provider-Wechsel-Trap:** Wenn du den Main-Provider wechselst (z.B. OpenRouter → DeepSeek), läuft session_search **weiter über den alten Provider**, weil `auxiliary.session_search` seine eigene `provider`/`model`/`base_url`/`api_key`-Konfiguration hat. Siehe `hermes-agent/references/config-traps.md` → Abschnitt "auxiliary.* — Task-Config lebt nicht vom Main-Model".

### Two Modes

| Mode | Trigger | LLM Call? | Latency |
|------|---------|-----------|---------|
| **Recent sessions** | Empty query (`session_search(query="")`) | **No** | ~0.1s |
| **Search** | Non-empty query | **Yes** (1-3 calls) | 5-45s depending on model |

The "recent sessions" mode skips LLM entirely and just returns DB metadata. If the user only needs context, suggest an empty query first.

## Source Code Constants

In `~/hermes/hermes-agent/tools/session_search_tool.py`:

```python
MAX_SESSION_CHARS = 100_000       # Line 28 — max transcript chars per session
MAX_SUMMARY_TOKENS = 10000        # Line 29 — max tokens for summarization
_HIDDEN_SESSION_SOURCES = ("tool",)  # Line 265 — exclude subagent/tool sessions
```

**Tuning tip:** Lowering `MAX_SESSION_CHARS` reduces LLM context → faster summarization at the cost of less complete summaries.

## Checking Current Config

```bash
grep -A 20 "session_search" ~/.hermes/config.yaml
```

## Quick DB Performance Test

```python
python3 -c "
import sqlite3, os, time
db = os.path.expanduser('~/.hermes/state.db')
conn = sqlite3.connect(db)

# Count sessions and messages
print(f'sessions: {conn.execute(\"SELECT COUNT(*) FROM sessions;\").fetchone()[0]}')
print(f'messages: {conn.execute(\"SELECT COUNT(*) FROM messages;\").fetchone()[0]}')

# Test FTS query speed
q = '''
SELECT DISTINCT s.id, s.title, s.source, s.started_at
FROM messages_fts f
JOIN messages m ON m.rowid = f.rowid
JOIN sessions s ON s.id = m.session_id
WHERE messages_fts MATCH ?
LIMIT 5
'''
t0 = time.time()
results = conn.execute(q, ('test',)).fetchall()
print(f'FTS query: {time.time()-t0:.3f}s')
conn.close()
"
```
