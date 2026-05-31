from __future__ import annotations

import sqlite3
from pathlib import Path

from cortex.config import RecentContextConfig
from cortex.recent_context import build_recent_context, normalize_title, resolve_state_db_path


def _make_state_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        """
        create table sessions (
            id text primary key,
            source text not null,
            started_at real not null,
            ended_at real,
            message_count integer default 0,
            tool_call_count integer default 0,
            title text
        )
        """
    )
    rows = [
        ("s11", "tui", 100.0, 1100.0, "Memory Cutover Test Evaluation #11"),
        ("s10", "tui", 90.0, 1090.0, "Memory Cutover Test Evaluation #10"),
        ("s9", "cli", 80.0, None, "Memory Cutover Test Evaluation #9"),
        ("empty", "tui", 70.0, 1070.0, ""),
        ("untitled", "tui", 60.0, 1060.0, "Untitled"),
        ("cron", "cron", 50.0, 1050.0, "Nightly Promotion Noise #1"),
        ("api", "api_server", 40.0, 1040.0, "API Server Noise #1"),
        ("other", "signal", 30.0, 1030.0, "Gateway Crash During Cortex Fix #7"),
        ("old", "tui", -90000.0, -90000.0, "Too Old #1"),
    ]
    con.executemany(
        "insert into sessions (id, source, started_at, ended_at, title) values (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def test_normalize_repeated_title_series() -> None:
    assert normalize_title("Memory Cutover Test Evaluation #11") == "Memory Cutover Test Evaluation"
    assert normalize_title("  Gateway   Crash During Cortex Fix #7  ") == "Gateway Crash During Cortex Fix"


def test_build_recent_context_groups_filters_open_and_budget(monkeypatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr("cortex.recent_context.time.time", lambda: 1200.0)
    cfg = RecentContextConfig(
        enabled=True,
        state_db_path=db,
        lookback_days=1,
        max_groups=5,
        budget=200,
    )

    result = build_recent_context(cfg)

    assert result.diagnostics.skipped_reason == ""
    assert result.diagnostics.examined_sessions == 8  # old row outside numeric lookback
    assert result.diagnostics.filtered_counts["title_noise"] == 2
    assert result.diagnostics.filtered_counts["source:cron"] == 1
    assert result.diagnostics.filtered_counts["source:api_server"] == 1
    assert result.diagnostics.groups_produced == 2
    assert result.topics[0].title == "Memory Cutover Test Evaluation"
    assert result.topics[0].count == 3
    assert result.topics[0].source_counts == {"cli": 1, "tui": 2}
    assert result.topics[0].open_count == 1
    assert "Memory Cutover Test Evaluation — 3 session(s)" in result.text
    assert "open=1" in result.text
    assert "transcript/message bodies" in result.text
    assert "Nightly Promotion Noise" not in result.text


def test_recent_context_source_policy_can_opt_into_api_server(monkeypatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr("cortex.recent_context.time.time", lambda: 1200.0)
    cfg = RecentContextConfig(
        enabled=True,
        state_db_path=db,
        include_sources=["api_server"],
        exclude_sources=[],
        lookback_days=1,
        max_groups=5,
        budget=200,
    )

    result = build_recent_context(cfg)

    assert [t.title for t in result.topics] == ["API Server Noise"]
    assert result.topics[0].source_counts == {"api_server": 1}


def test_recent_context_budget_truncates_cleanly(monkeypatch, tmp_path: Path) -> None:
    db = _make_state_db(tmp_path / "state.db")
    monkeypatch.setattr("cortex.recent_context.time.time", lambda: 1200.0)
    cfg = RecentContextConfig(enabled=True, state_db_path=db, budget=12, lookback_days=1)

    result = build_recent_context(cfg)

    assert result.diagnostics.budget_used <= 12 or "truncated" in result.text
    assert result.text.startswith("## Recent Hermes Topics")


def test_resolve_state_db_uses_root_hermes_home_from_profile(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "backend-eng"
    root.mkdir(parents=True)
    profile_home.mkdir(parents=True)
    db = root / "state.db"
    db.write_text("sqlite-ish")
    monkeypatch.setattr("cortex.recent_context._hermes_home", lambda: profile_home)

    resolved, source = resolve_state_db_path(None)

    assert resolved == db
    assert source == "root_hermes_home_from_profile"
