# session_search Architecture

This is a public, stable reference for how Hermes `session_search` works. Keep
local database sizes, session counts, provider choices, and one-off incident data
in private operator notes instead of this tracked repo.

## How it works

`session_search` queries the Hermes session database, not the raw JSON/JSONL log
files directly. The database uses SQLite plus FTS5 full-text indexes for message
lookup.

### Database

- **Typical path:** `~/.hermes/state.db` for the active Hermes home/profile
- **Core tables:** `sessions`, `messages`, and FTS-backed message indexes
- **Write path:** runtime message inserts populate the search indexes through DB
  triggers or equivalent Hermes state-management code

The exact schema belongs to Hermes Agent itself and may change. Treat the table
names above as an integration contract only when verified against the active
Hermes version.

### JSON/JSONL logs vs database

The `sessions/` directory can contain logs that are not present in the database,
and `sessions.json` may be a registry of active/recent sessions rather than a
complete full-text index. If a known conversation is missing from `session_search`,
check whether the active Hermes process wrote it to the DB before assuming search
ranking failed.

### Checking the DB directly

Use this only for diagnostics, and run it against the intended Hermes home/profile:

```python
import os
import sqlite3

path = os.path.expanduser('~/.hermes/state.db')
db = sqlite3.connect(path)

# Check if a specific session is in the DB.
row = db.execute(
    'SELECT id, started_at, source FROM sessions WHERE id=?',
    ('SESSION_ID',),
).fetchone()

# List recent sessions.
rows = db.execute(
    'SELECT id, started_at, source FROM sessions ORDER BY started_at DESC LIMIT 5'
).fetchall()

# Count by source.
rows = db.execute('SELECT source, COUNT(*) FROM sessions GROUP BY source').fetchall()
```

## Diagnostic checklist

- Confirm which Hermes home/profile owns `state.db`.
- Confirm the session exists in `sessions` before debugging search ranking.
- If the session exists but no messages match, inspect the FTS index/write path.
- If the session exists only as a JSON/JSONL log, use log inspection or import tooling
  if Hermes provides it; `session_search` cannot find data that was never indexed.
