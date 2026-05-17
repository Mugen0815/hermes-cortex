"""Tests for cortex.boosts — recency half-life & importance multiplicative boosts."""

from __future__ import annotations

import math
from datetime import date

import pytest

from cortex.boosts import (
    apply_boosts,
    apply_boosts_detailed,
    chunk_quality_factor,
    importance_factor,
    importance_value,
    query_explicitly_asks_for_links,
    recency_factor,
    source_date_for_recency,
)
from cortex.config import SearchConfig


def _cfg(**kw) -> SearchConfig:
    return SearchConfig(**kw)


# ---- Source date selection -------------------------------------------------


def test_source_date_prefers_last_verified():
    chunk = {
        "fm_normalized": {"last_verified": "2026-01-15"},
        "modified_date": "2026-04-01",
    }
    assert source_date_for_recency(chunk) == date(2026, 1, 15)


def test_source_date_falls_back_to_modified_date():
    chunk = {
        "fm_normalized": {"last_verified": ""},
        "modified_date": "2026-04-01",
    }
    assert source_date_for_recency(chunk) == date(2026, 4, 1)


def test_source_date_falls_back_to_modified_full():
    chunk = {"modified": "2026-04-01T12:30:00"}
    assert source_date_for_recency(chunk) == date(2026, 4, 1)


def test_source_date_none_when_nothing_parseable():
    assert source_date_for_recency({}) is None
    assert source_date_for_recency({"modified_date": "garbage"}) is None


# ---- Recency factor: HALF-LIFE PRECISION ----------------------------------


def test_recency_at_zero_age_is_max_boost():
    cfg = _cfg(recency_max_boost=0.20, recency_half_life_days=90)
    chunk = {"modified_date": "2026-04-15"}
    f = recency_factor(chunk, cfg, now=date(2026, 4, 15))
    assert f == cfg.recency_max_boost


def test_recency_at_half_life_is_exactly_half_max_boost():
    """The whole point of using ln(2) — at age=HL, factor = max_boost / 2."""
    cfg = _cfg(recency_max_boost=0.20, recency_half_life_days=90)
    chunk = {"modified_date": "2026-01-15"}  # 90 days before "now"
    f = recency_factor(chunk, cfg, now=date(2026, 4, 15))
    assert math.isclose(f, cfg.recency_max_boost / 2, rel_tol=1e-9)


def test_recency_at_two_half_lives_is_quarter_max_boost():
    cfg = _cfg(recency_max_boost=0.20, recency_half_life_days=90)
    chunk = {"modified_date": "2025-10-17"}  # 180 days before "now"
    f = recency_factor(chunk, cfg, now=date(2026, 4, 15))
    assert math.isclose(f, cfg.recency_max_boost / 4, rel_tol=1e-9)


def test_recency_future_date_clamped_to_zero_age():
    """Source date in the future → treat as age=0, full boost (no negative ages)."""
    cfg = _cfg(recency_max_boost=0.20, recency_half_life_days=90)
    chunk = {"modified_date": "2030-01-01"}
    f = recency_factor(chunk, cfg, now=date(2026, 4, 15))
    assert f == cfg.recency_max_boost


def test_recency_disabled_returns_zero():
    cfg = _cfg(recency_boost=False, recency_max_boost=0.20)
    chunk = {"modified_date": "2026-04-15"}
    assert recency_factor(chunk, cfg, now=date(2026, 4, 15)) == 0.0


def test_recency_missing_date_returns_zero():
    cfg = _cfg(recency_max_boost=0.20)
    assert recency_factor({}, cfg, now=date(2026, 4, 15)) == 0.0


# ---- Importance factor -----------------------------------------------------


def test_importance_value_uses_raw_fm():
    chunk = {"frontmatter": {"importance": 4}, "fm_normalized": {"importance": 4.0}}
    assert importance_value(chunk) == 4.0


def test_importance_value_missing_when_only_normalized_default():
    """fm_normalized.importance == 3.0 with NO raw → treat as missing."""
    chunk = {"frontmatter": {}, "fm_normalized": {"importance": 3.0}}
    assert importance_value(chunk) is None


def test_importance_value_clamps_to_1_5():
    assert importance_value({"frontmatter": {"importance": 0}}) == 1.0
    assert importance_value({"frontmatter": {"importance": 99}}) == 5.0


def test_importance_factor_at_max():
    cfg = _cfg(importance_max_boost=0.30)
    chunk = {"frontmatter": {"importance": 5}}
    assert importance_factor(chunk, cfg) == 0.30


def test_importance_factor_at_min_is_zero():
    cfg = _cfg(importance_max_boost=0.30)
    chunk = {"frontmatter": {"importance": 1}}
    assert importance_factor(chunk, cfg) == 0.0


def test_importance_factor_at_3_is_half_max():
    cfg = _cfg(importance_max_boost=0.30)
    chunk = {"frontmatter": {"importance": 3}}
    assert math.isclose(importance_factor(chunk, cfg), 0.15, rel_tol=1e-9)


def test_importance_factor_missing_is_neutral_zero():
    """Missing importance → 0 boost, NOT default-3.0 phantom +15%."""
    cfg = _cfg(importance_max_boost=0.30)
    chunk = {"frontmatter": {}, "fm_normalized": {"importance": 3.0}}
    assert importance_factor(chunk, cfg) == 0.0


def test_importance_disabled_returns_zero():
    cfg = _cfg(importance_boost=False, importance_max_boost=0.30)
    chunk = {"frontmatter": {"importance": 5}}
    assert importance_factor(chunk, cfg) == 0.0


# ---- Combined boost --------------------------------------------------------


def test_apply_boosts_combined_max_is_capped():
    """Default max_boost_multiplier=1.20 caps legacy raw 1.20 * 1.30 = 1.56."""
    cfg = _cfg(recency_max_boost=0.20, importance_max_boost=0.30, recency_half_life_days=90)
    chunk = {
        "frontmatter": {"importance": 5},
        "modified_date": "2026-04-15",
    }
    rrf = 1.0
    final, rf, if_ = apply_boosts(rrf, chunk, cfg, now=date(2026, 4, 15))
    assert math.isclose(final, 1.20, rel_tol=1e-9)
    assert math.isclose(rf, 0.20, rel_tol=1e-9)
    assert math.isclose(if_, 0.30, rel_tol=1e-9)


def test_apply_boosts_detailed_exposes_raw_and_capped_boost():
    cfg = _cfg(
        recency_max_boost=0.20,
        importance_max_boost=0.30,
        recency_half_life_days=90,
        max_boost_multiplier=1.25,
    )
    chunk = {"frontmatter": {"importance": 5}, "modified_date": "2026-04-15"}
    applied = apply_boosts_detailed(1.0, chunk, cfg, now=date(2026, 4, 15))
    assert applied.raw_boost_multiplier == pytest.approx(1.20 * 1.30)
    assert applied.boost_multiplier == pytest.approx(1.25)
    assert applied.boost_capped is True
    assert applied.final_score == pytest.approx(1.25)


def test_apply_boosts_detailed_allows_uncapped_when_below_cap():
    cfg = _cfg(
        recency_max_boost=0.10,
        importance_max_boost=0.10,
        max_boost_multiplier=1.25,
    )
    chunk = {"frontmatter": {"importance": 5}, "modified_date": "2026-04-15"}
    applied = apply_boosts_detailed(1.0, chunk, cfg, now=date(2026, 4, 15))
    assert applied.raw_boost_multiplier == pytest.approx(1.10 * 1.10)
    assert applied.boost_multiplier == pytest.approx(1.10 * 1.10)
    assert applied.boost_capped is False


def test_apply_boosts_no_metadata_neutral():
    cfg = _cfg()
    final, rf, if_ = apply_boosts(0.5, {}, cfg, now=date(2026, 4, 15))
    assert final == 0.5
    assert rf == 0.0 and if_ == 0.0


def test_apply_boosts_recency_only_at_half_life():
    cfg = _cfg(recency_max_boost=0.20, importance_max_boost=0.30, recency_half_life_days=90)
    chunk = {"modified_date": "2026-01-15"}  # exactly 90 days
    final, rf, if_ = apply_boosts(1.0, chunk, cfg, now=date(2026, 4, 15))
    assert math.isclose(rf, 0.10, rel_tol=1e-9)
    assert if_ == 0.0
    assert math.isclose(final, 1.10, rel_tol=1e-9)


# ---- Link/related chunk quality --------------------------------------------


def test_chunk_quality_penalizes_link_heading_for_content_query():
    cfg = _cfg(link_chunk_penalty=0.75)
    chunk = {"heading_path": ["Links"], "text": "- [[Project - hermes-cortex]]"}
    factor, reason = chunk_quality_factor(chunk, cfg, query="cortex scoring stabilisieren")
    assert factor == pytest.approx(0.75)
    assert reason == "link_heading"


def test_chunk_quality_penalizes_dense_wikilink_list():
    cfg = _cfg(link_chunk_penalty=0.75)
    chunk = {
        "heading_path": ["Notes"],
        "text": "- [[A]]\n- [[B]]\n- [[C]]",
        "wikilinks": ["A", "B", "C"],
    }
    factor, reason = chunk_quality_factor(chunk, cfg, query="deployment runbook")
    assert factor == pytest.approx(0.75)
    assert reason == "wikilink_bullet_list"


def test_chunk_quality_ignores_note_level_wikilinks_on_normal_content():
    cfg = _cfg(link_chunk_penalty=0.75)
    chunk = {
        "heading_path": ["Session-Formate"],
        "text": (
            "Der Cron-Job durchsucht **beide** Formate:\n"
            "- `*.jsonl` — Signal-Sessions\n"
            "- `session_*.json` — TUI/CLI-Sessions\n"
            "- `request_dump_*.json` werden ignoriert"
        ),
        "wikilinks": ["Runbook - Promote session knowledge", "Project - Example Homebase"],
    }
    factor, reason = chunk_quality_factor(chunk, cfg, query="nightly promotion jsonl tui")
    assert factor == 1.0
    assert reason == "neutral"


def test_chunk_quality_skips_penalty_for_explicit_link_query():
    cfg = _cfg(link_chunk_penalty=0.75)
    chunk = {"heading_path": ["Related"], "text": "- [[A]]\n- [[B]]"}
    factor, reason = chunk_quality_factor(chunk, cfg, query="related links for cortex")
    assert factor == 1.0
    assert reason == "explicit_link_query"


def test_query_explicitly_asks_for_links_handles_german_relation_terms():
    assert query_explicitly_asks_for_links("Welche Verweise hat Cortex?") is True
    assert query_explicitly_asks_for_links("was weißt du über cortex scoring") is False
