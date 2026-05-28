"""Tests for cortex.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.config import (
    Config,
    ConfigError,
    find_config,
    load_config,
)


EXAMPLE_CONFIG = """
vault:
  path: {vault}
  include_folders: [10_facts, 20_decisions]
  exclude_folders: [00_inbox]

hermes_memory:
  memory_path: {memory}
  user_path: {user}
  soul_path: {soul}

index:
  chunks_path: {chunks}
  chroma_path: {chroma}
  collection: test-coll

embeddings:
  model: my-model
  device: cuda

search:
  top_k: 5
  bm25_weight: 0.3
  vector_weight: 0.7
  rrf_k: 30
  wikilink_traversal: 2
  recency_boost: false
  importance_boost: false

context_builder:
  token_budget: 2000
  cite_sources: false
  include_hermes_memory: false

hooks:
  cache_warm:
    enabled: false
  context_injection:
    enabled: true
    budget: 1234
    query: "test query"
    load_skill: false
    skill_path: /tmp/memory-query-flow/SKILL.md

cron:
  nightly_promotion:
    enabled: true
    name: custom-nightly
    schedule: "5 3 * * *"
    timezone: Europe/Berlin
    deliver: origin
    enabled_toolsets: [file, terminal]
    lookback_days: 2
    state_db_path: /tmp/hermes-state.db
    legacy_fallback_enabled: false
    session_globs:
      - ~/.hermes/sessions/*.jsonl
      - ~/.hermes/sessions/session_*.json
    dry_run_first: false
  weekly_review:
    enabled: true
    name: custom-weekly
    schedule: "30 8 * * 1"
    timezone: UTC
    deliver: origin
    output_format: markdown
    dry_run: false
    stale_days: 90
    stale_min_importance: 3.5
    consolidation_min_degree: 5

logging:
  level: DEBUG
"""


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    memory = tmp_path / "MEMORY.md"
    memory.write_text("hello")
    user = tmp_path / "USER.md"
    user.write_text("user")
    soul = tmp_path / "SOUL.md"  # we'll make this missing on purpose
    chunks = tmp_path / "chunks.jsonl"
    chroma = tmp_path / "chroma"

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        EXAMPLE_CONFIG.format(
            vault=vault,
            memory=memory,
            user=user,
            soul=soul,
            chunks=chunks,
            chroma=chroma,
        )
    )
    return cfg_path


def test_load_full_config(cfg_file: Path) -> None:
    cfg = load_config(cfg_file)
    assert isinstance(cfg, Config)
    assert cfg.vault.path.name == "vault"
    assert cfg.vault.include_folders == ["10_facts", "20_decisions"]
    assert cfg.vault.exclude_folders == ["00_inbox"]
    assert cfg.index.collection == "test-coll"
    assert cfg.embeddings.model == "my-model"
    assert cfg.embeddings.device == "cuda"
    assert cfg.search.top_k == 5
    assert cfg.search.wikilink_traversal == 2
    assert cfg.search.recency_boost is False
    assert cfg.context_builder.token_budget == 2000
    assert cfg.context_builder.include_hermes_memory is False
    assert cfg.hooks.cache_warm_enabled is False
    assert cfg.hooks.context_injection_enabled is True
    assert cfg.hooks.context_injection_budget == 1234
    assert cfg.hooks.context_injection_query == "test query"
    assert cfg.hooks.load_skill is False
    assert cfg.hooks.skill_path == "/tmp/memory-query-flow/SKILL.md"
    assert cfg.cron.nightly_promotion.name == "custom-nightly"
    assert cfg.cron.nightly_promotion.schedule == "5 3 * * *"
    assert cfg.cron.nightly_promotion.deliver == "origin"
    assert cfg.cron.nightly_promotion.enabled_toolsets == ["file", "terminal"]
    assert cfg.cron.nightly_promotion.lookback_days == 2
    assert cfg.cron.nightly_promotion.state_db_path == "/tmp/hermes-state.db"
    assert cfg.cron.nightly_promotion.legacy_fallback_enabled is False
    assert cfg.cron.nightly_promotion.dry_run_first is False
    assert cfg.cron.weekly_review.name == "custom-weekly"
    assert cfg.cron.weekly_review.schedule == "30 8 * * 1"
    assert cfg.cron.weekly_review.timezone == "UTC"
    assert cfg.cron.weekly_review.deliver == "origin"
    assert cfg.cron.weekly_review.output_format == "markdown"
    assert cfg.cron.weekly_review.dry_run is False
    assert cfg.cron.weekly_review.stale_days == 90
    assert cfg.cron.weekly_review.stale_min_importance == 3.5
    assert cfg.cron.weekly_review.consolidation_min_degree == 5
    assert cfg.log_level == "DEBUG"
    assert cfg.source_path == cfg_file.resolve()


def test_hermes_memory_available_skips_missing(cfg_file: Path) -> None:
    cfg = load_config(cfg_file)
    available = cfg.hermes_memory.available()
    # MEMORY.md and USER.md exist, SOUL.md does not (we never wrote it)
    assert "memory" in available
    assert "user" in available
    assert "soul" not in available


def test_missing_vault_path_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("vault: {}\nindex: {chunks_path: a, chroma_path: b}\n")
    with pytest.raises(ConfigError, match="vault.path is required"):
        load_config(bad)


def test_missing_index_paths_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("vault: {path: /tmp}\nindex: {}\n")
    with pytest.raises(ConfigError, match="chunks_path and index.chroma_path"):
        load_config(bad)


def test_load_nonexistent_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_defaults_when_optional_sections_omitted(tmp_path: Path) -> None:
    minimal = tmp_path / "min.yaml"
    minimal.write_text(
        "vault: {path: /tmp}\n"
        f"index: {{chunks_path: {tmp_path}/c.jsonl, chroma_path: {tmp_path}/chroma}}\n"
    )
    cfg = load_config(minimal)
    assert cfg.embeddings.model == "sentence-transformers/all-MiniLM-L6-v2"
    assert cfg.search.top_k == 10
    assert cfg.context_builder.token_budget == 4000
    assert cfg.context_builder.include_hermes_memory is False
    assert cfg.hermes_memory.memory_path is None
    assert cfg.hooks.cache_warm_enabled is False
    assert cfg.hooks.context_injection_enabled is False
    assert cfg.hooks.context_injection_budget == 1000
    assert cfg.hooks.context_injection_query == ""
    assert cfg.hooks.load_skill is False
    assert cfg.cron.nightly_promotion.enabled is False
    assert cfg.cron.nightly_promotion.name == "hermes-cortex-nightly-promotion"
    assert cfg.cron.nightly_promotion.schedule == "0 2 * * *"
    assert cfg.cron.nightly_promotion.timezone == "Europe/Berlin"
    assert cfg.cron.nightly_promotion.deliver == "origin"
    assert cfg.cron.nightly_promotion.enabled_toolsets == ["file", "terminal"]
    assert cfg.cron.nightly_promotion.lookback_days == 1
    assert cfg.cron.nightly_promotion.state_db_path == "~/.hermes/state.db"
    assert cfg.cron.nightly_promotion.legacy_fallback_enabled is True
    assert cfg.cron.nightly_promotion.session_globs == [
        "~/.hermes/sessions/*.jsonl",
        "~/.hermes/sessions/session_*.json",
    ]
    assert cfg.cron.nightly_promotion.dry_run_first is True
    assert cfg.cron.weekly_review.enabled is False
    assert cfg.cron.weekly_review.name == "hermes-cortex-weekly-review"
    assert cfg.cron.weekly_review.schedule == "0 8 * * 1"
    assert cfg.cron.weekly_review.timezone == "Europe/Berlin"
    assert cfg.cron.weekly_review.deliver == "origin"
    assert cfg.cron.weekly_review.output_format == "markdown"
    assert cfg.cron.weekly_review.dry_run is True
    assert cfg.cron.weekly_review.stale_days == 180
    assert cfg.cron.weekly_review.stale_min_importance == 4.0
    assert cfg.cron.weekly_review.consolidation_min_degree == 3


def test_find_config_via_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg_file: Path) -> None:
    monkeypatch.setenv("CORTEX_CONFIG", str(cfg_file))
    found = find_config()
    assert found == cfg_file.resolve()


def test_find_config_env_missing_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CORTEX_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="non-existent"):
        find_config()


# ---- search-config validation ----------------------------------------------


def _minimal_cfg(
    tmp_path: Path,
    search_overrides: str = "",
    cron_overrides: str = "",
) -> Path:
    """Write a minimal config.yaml with optional search/cron section overrides."""
    p = tmp_path / "c.yaml"
    p.write_text(
        f"vault: {{path: {tmp_path}}}\n"
        f"index: {{chunks_path: {tmp_path}/c.jsonl, chroma_path: {tmp_path}/chroma}}\n"
        + (f"search:\n{search_overrides}\n" if search_overrides else "")
        + (
            "cron:\n  nightly_promotion:\n"
            f"{cron_overrides}\n"
            if cron_overrides
            else ""
        )
    )
    return p


def test_search_config_rejects_zero_rrf_k(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  rrf_k: 0\n")
    with pytest.raises(ConfigError, match="rrf_k"):
        load_config(p)


def test_search_config_rejects_negative_weights(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  bm25_weight: -0.1\n  vector_weight: 0.5\n")
    with pytest.raises(ConfigError, match="bm25_weight"):
        load_config(p)


def test_search_config_rejects_both_weights_zero(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  bm25_weight: 0\n  vector_weight: 0\n")
    with pytest.raises(ConfigError, match="cannot both be 0"):
        load_config(p)


def test_search_config_rejects_zero_top_k(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  top_k: 0\n")
    with pytest.raises(ConfigError, match="top_k"):
        load_config(p)


def test_search_config_rejects_silly_traversal(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  wikilink_traversal: 42\n")
    with pytest.raises(ConfigError, match="wikilink_traversal"):
        load_config(p)


def test_search_config_accepts_sane_defaults(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path)
    cfg = load_config(p)
    assert cfg.search.top_k == 10
    assert cfg.search.rrf_k == 60
    assert cfg.search.bm25_weight == 0.5
    assert cfg.search.vector_weight == 0.5
    assert cfg.search.fetch_multiplier == 6
    # Boost defaults
    assert cfg.search.recency_boost is True
    assert cfg.search.recency_max_boost == 0.20
    assert cfg.search.recency_half_life_days == 90.0
    assert cfg.search.importance_boost is True
    assert cfg.search.importance_max_boost == 0.30


def test_search_config_rejects_negative_recency_boost(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  recency_max_boost: -0.1\n")
    with pytest.raises(ConfigError, match="recency_max_boost"):
        load_config(p)


def test_search_config_rejects_zero_half_life(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  recency_half_life_days: 0\n")
    with pytest.raises(ConfigError, match="recency_half_life_days"):
        load_config(p)


def test_search_config_rejects_implausible_recency_boost(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  recency_max_boost: 9.0\n")
    with pytest.raises(ConfigError, match="recency_max_boost"):
        load_config(p)


def test_search_config_rejects_negative_importance_boost(tmp_path: Path) -> None:
    p = _minimal_cfg(tmp_path, "  importance_max_boost: -0.5\n")
    with pytest.raises(ConfigError, match="importance_max_boost"):
        load_config(p)


def test_search_config_accepts_custom_boost_values(tmp_path: Path) -> None:
    p = _minimal_cfg(
        tmp_path,
        "  recency_max_boost: 0.5\n"
        "  recency_half_life_days: 30\n"
        "  importance_max_boost: 0.4\n",
    )
    cfg = load_config(p)
    assert cfg.search.recency_max_boost == 0.5
    assert cfg.search.recency_half_life_days == 30.0
    assert cfg.search.importance_max_boost == 0.4


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ("    name: ''\n", "name"),
        ("    schedule: not-a-cron\n", "schedule"),
        ("    timezone: Mars/Olympus\n", "timezone"),
        ("    deliver: ''\n", "deliver"),
        ("    enabled_toolsets: []\n", "enabled_toolsets"),
        ("    lookback_days: 0\n", "lookback_days"),
        ("    session_globs: []\n", "session_globs"),
    ],
)
def test_cron_config_rejects_invalid_values(tmp_path: Path, overrides: str, match: str) -> None:
    p = _minimal_cfg(tmp_path, cron_overrides=overrides)
    with pytest.raises(ConfigError, match=match):
        load_config(p)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ("    name: ''\n", "name"),
        ("    schedule: not-a-cron\n", "schedule"),
        ("    timezone: Mars/Olympus\n", "timezone"),
        ("    deliver: ''\n", "deliver"),
        ("    output_format: json\n", "output_format"),
        ("    stale_days: 0\n", "stale_days"),
        ("    stale_min_importance: -1\n", "stale_min_importance"),
        ("    consolidation_min_degree: 0\n", "consolidation_min_degree"),
    ],
)
def test_weekly_cron_config_rejects_invalid_values(tmp_path: Path, overrides: str, match: str) -> None:
    p = tmp_path / "weekly-bad.yaml"
    p.write_text(
        f"vault: {{path: {tmp_path}}}\n"
        f"index: {{chunks_path: {tmp_path}/c.jsonl, chroma_path: {tmp_path}/chroma}}\n"
        "cron:\n  weekly_review:\n"
        f"{overrides}\n"
    )
    with pytest.raises(ConfigError, match=match):
        load_config(p)
