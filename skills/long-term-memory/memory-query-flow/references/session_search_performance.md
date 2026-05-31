# session_search Performance & Tuning

This public reference describes the common latency model without embedding local
machine metrics, session counts, provider choices, or private config values. Keep
real measurements in private operator notes.

## Architecture: latency flow

```text
User query
  → SQLite/FTS session lookup
  → Load matched messages
  → Format/snippet or summarize result windows
  → Return ranked sessions
```

The database lookup is usually not the expensive part. User-visible latency often
comes from summarizing or formatting large matched conversations, especially when
multiple sessions are processed in parallel by an auxiliary LLM.

## Common bottlenecks

| Symptom | Likely cause | First checks |
|---|---|---|
| Search is slow only for broad queries | Too many large matched sessions need summaries | Narrow the query or reduce result count |
| Empty/recent browse is fast, non-empty query is slow | Query path invokes summarization | Check auxiliary/session-search model routing |
| A known session is absent | Session was not written to the active DB | See `session_search_architecture.md` |
| Latency changes after model/provider edits | Auxiliary routing differs from the main chat model | Inspect the active Hermes profile config |

## Tuning levers

Exact config keys are owned by Hermes Agent and may change; verify against the
active Hermes docs/config before editing. Typical levers are:

| Lever | Effect |
|---|---|
| Auxiliary model/provider | Biggest latency/cost lever when summaries are generated |
| Max matched sessions / result limit | Fewer sessions means less summarization work |
| Per-session transcript/window size | Smaller windows are faster but less complete |
| Concurrency | More parallelism can help until provider rate limits or slowest-call latency dominates |
| Timeout | Controls failure behavior, not underlying work cost |

## Practical triage

1. Prefer Vault/Cortex lookup for durable facts; use `session_search` for raw
   conversational history.
2. Use exact phrases or narrower terms before increasing result count.
3. Compare browse/recent mode with full-text search mode to isolate DB vs summary
   cost.
4. Inspect active Hermes config for auxiliary `session_search` routing instead of
   assuming it follows the main chat model.
5. If you need hard numbers, measure locally and store them in private runtime
   notes, not this public repo.

## Quick DB timing pattern

```python
import os
import sqlite3
import time

path = os.path.expanduser('~/.hermes/state.db')
conn = sqlite3.connect(path)
query = '''
SELECT DISTINCT s.id, s.title, s.source, s.started_at
FROM messages_fts f
JOIN messages m ON m.rowid = f.rowid
JOIN sessions s ON s.id = m.session_id
WHERE messages_fts MATCH ?
LIMIT 5
'''
start = time.time()
rows = conn.execute(query, ('example',)).fetchall()
print(f'FTS query: {time.time() - start:.3f}s; rows={len(rows)}')
conn.close()
```
