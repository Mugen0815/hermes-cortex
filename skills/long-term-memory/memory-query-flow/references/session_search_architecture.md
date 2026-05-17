# session_search Architecture

> Discovered while debugging why a known session wasn't found by session_search (May 2026).

## How it works

`session_search` does **not** read JSON/JSONL files directly. It queries a **SQLite database with FTS5 full-text search**.

### Database

- **Path:** `~/.hermes/state.db` (~74 MB as of May 2026)
- **Class:** `SessionDB` in `hermes_state.py`
- **Tables:** `sessions`, `messages`, `messages_fts` (FTS5 virtual table), `messages_fts_trigram`
- **Triggers:** FTS5 index is auto-populated via `AFTER INSERT` triggers on `messages`

### Write path

All session messages are INSERTed into the `messages` table at runtime. The FTS5 triggers automatically update the full-text index. This is the **only** data path to session_search — there is no batch import from `.json`/`.jsonl` files.

### Known gap

The `sessions/` directory contains ~268+ `.json` session files, but `state.db` only has ~185 sessions indexed. This means:

- **Not all sessions that produce a `.json` log file end up in `state.db`**
- This can happen if the session ran in a context where the DB wasn't written to (early termination, different Hermes instance, UI mode that logs to file but not DB)
- `sessions.json` is a registry/index of **active/recent sessions only** — not a full-text index

### Checking the DB directly

```python
python3 -c "
import sqlite3
db = sqlite3.connect(os.path.expanduser('~/.hermes/state.db'))

# Check if a specific session is in the DB
row = db.execute('SELECT id, started_at, source FROM sessions WHERE id=?', ('SESSION_ID',)).fetchone()

# List recent sessions
rows = db.execute('SELECT id, started_at, source FROM sessions ORDER BY started_at DESC LIMIT 5').fetchall()

# Count by source
rows = db.execute('SELECT source, COUNT(*) FROM sessions GROUP BY source').fetchall()
"
```

### Schema

```
sessions:  id, started_at, source, model, platform, meta
messages:  id, session_id (FK), role, content, timestamp, tool_name
messages_fts:  rowid=FK to messages, content (FTS5 unicode61 tokenizer)
messages_fts_trigram:  rowid=FK to messages, content (FTS5 trigram tokenizer for CJK)
```

### Write contention

- WAL journal mode + `BEGIN IMMEDIATE` for writes
- Retry with random jitter (20-150ms) on lock contention
- CHECKPOINT every 50 writes
