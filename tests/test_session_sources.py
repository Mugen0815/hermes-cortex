from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from cortex.session_sources import collect_recent_sessions, load_legacy_session_rows


def _make_state_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES (7);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                title TEXT,
                parent_session_id TEXT,
                end_reason TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL,
                tool_name TEXT,
                tool_call_id TEXT
            );
            """
        )


def _insert_session(conn: sqlite3.Connection, session_id: str, ts: float) -> None:
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, title) VALUES (?, 'cli', ?, ?)",
        (session_id, ts - 60, f"title {session_id}"),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', ?, ?)",
        (session_id, f"durable vault fact from {session_id}", ts),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp, tool_name) VALUES (?, 'tool', ?, ?, 'terminal')",
        (session_id, "large tool payload ignored", ts + 1),
    )


def test_state_db_primary_selects_recent_sessions_without_legacy_files(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _make_state_db(db_path)
    now = datetime.fromisoformat("2026-05-28T12:00:00+00:00")
    with sqlite3.connect(db_path) as conn:
        _insert_session(conn, "recent", now.timestamp() - 120)
        _insert_session(conn, "old", now.timestamp() - (3 * 24 * 3600))

    result = collect_recent_sessions(
        lookback_days=1,
        timezone="UTC",
        state_db_path=db_path,
        session_globs=[str(tmp_path / "session_*.json")],
        now=now,
    )

    assert [s.session_id for s in result.sessions] == ["recent"]
    assert result.sessions[0].source == "state_db"
    assert [m.role for m in result.sessions[0].messages] == ["user"]
    diag = result.diagnostics.as_dict()
    assert diag["source_backend_primary"] == "state_db"
    assert diag["state_db_readable"] is True
    assert diag["state_db_schema_version"] == 7
    assert diag["sessions_seen_by_backend"]["state_db"] == 2
    assert diag["sessions_selected"] == 1
    assert diag["sessions_selected_by_source"] == {"state_db": 1}
    assert diag["messages_scanned"] == 2
    assert diag["fallback_used"] is False
    assert diag["skipped"]["tool_or_non_chat_messages"] == 1


def test_legacy_fallback_supports_json_jsonl_wrapper_and_ignores_request_dump(tmp_path: Path) -> None:
    now = datetime.fromisoformat("2026-05-28T12:00:00+00:00")
    ts = now.timestamp() - 60
    (tmp_path / "session_array.json").write_text(
        json.dumps([{"role": "user", "content": "array durable fact", "timestamp": ts}]),
        encoding="utf-8",
    )
    (tmp_path / "session_wrapper.json").write_text(
        json.dumps({"messages": [{"role": "assistant", "content": "wrapper durable fact", "timestamp": ts}]}),
        encoding="utf-8",
    )
    (tmp_path / "signal.jsonl").write_text(
        json.dumps({"role": "user", "content": "jsonl durable fact", "timestamp": ts}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "request_dump_abc.json").write_text("{not parsed", encoding="utf-8")

    result = collect_recent_sessions(
        lookback_days=1,
        timezone="UTC",
        state_db_path=tmp_path / "missing-state.db",
        session_globs=[str(tmp_path / "*.json"), str(tmp_path / "*.jsonl")],
        now=now,
    )

    assert {s.session_id for s in result.sessions} == {"session_array", "session_wrapper", "signal"}
    diag = result.diagnostics.as_dict()
    assert diag["source_backend_primary"] == "legacy_files"
    assert diag["fallback_used"] is True
    assert diag["fallback_reason"] == "state_db_missing"
    assert diag["ignored_files"]["request_dump"] == 1
    assert diag["sessions_seen_by_backend"]["legacy_json"] == 2
    assert diag["sessions_seen_by_backend"]["legacy_jsonl"] == 1
    assert diag["sessions_selected_by_source"] == {"legacy_json": 2, "legacy_jsonl": 1}


def test_state_db_zero_sessions_can_disable_legacy_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _make_state_db(db_path)
    now = datetime.fromisoformat("2026-05-28T12:00:00+00:00")
    (tmp_path / "session_legacy.json").write_text(
        json.dumps([{"role": "user", "content": "legacy durable fact", "timestamp": now.timestamp()}]),
        encoding="utf-8",
    )

    result = collect_recent_sessions(
        lookback_days=1,
        timezone="UTC",
        state_db_path=db_path,
        session_globs=[str(tmp_path / "session_*.json")],
        legacy_fallback_enabled=False,
        now=now,
    )

    assert result.sessions == []
    diag = result.diagnostics.as_dict()
    assert diag["fallback_used"] is False
    assert diag["fallback_reason"] == "state_db_zero_eligible_sessions"
    assert diag["sessions_seen_by_backend"]["legacy_json"] == 0


def test_load_legacy_session_rows_handles_wrapper_messages(tmp_path: Path) -> None:
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}]}), encoding="utf-8")

    rows, source = load_legacy_session_rows(path)

    assert source == "legacy_json"
    assert rows == [{"role": "user", "content": "hi"}]
