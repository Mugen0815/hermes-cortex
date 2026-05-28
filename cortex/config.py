"""Configuration loader for hermes-cortex.

Reads a YAML config file, expands ~ in paths, and exposes a typed Config object.

Usage:
    from cortex.config import load_config
    cfg = load_config()  # auto-discovers config.yaml
    print(cfg.vault.path)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


# ---- Profile-aware Hermes Home ----
# get_hermes_home() is Hermes' canonical path resolver — it respects
# HERMES_HOME env var (set by gateway/kanban subprocess spawners) and
# falls back to ~/.hermes.  We import it optionally so cortex works
# both as a Hermes plugin (where it's available) and as a standalone CLI.
_HERMES_HOME: "Path | None" = None
try:
    from hermes_constants import get_hermes_home as _get_hermes_home
    _HERMES_HOME = _get_hermes_home()
except ImportError:
    _HERMES_HOME = None

_log = logging.getLogger("cortex.config")


def _hermes_home() -> Path:
    """Return the profile-aware Hermes home directory.

    When running inside a Hermes subprocess (HERMES_HOME set by gateway/
    kanban dispatcher), this returns the profile-specific path.  Falls
    back to ~/.hermes for standalone CLI use.
    """
    global _HERMES_HOME
    if _HERMES_HOME is not None:
        return _HERMES_HOME
    # Second chance: re-import in case HERMES_HOME was set after module load
    try:
        from hermes_constants import get_hermes_home
        _HERMES_HOME = get_hermes_home()
        return _HERMES_HOME
    except ImportError:
        _HERMES_HOME = Path.home() / ".hermes"
        _log.debug("hermes_constants not available; using %s", _HERMES_HOME)
        return _HERMES_HOME


def _to_bool(value: Any, default: bool = False) -> bool:
    """Sicherer bool-Konverter für YAML-Konfig-Werte.

    yaml.safe_load liefert echte Bools (True/False) – aber wenn jemand
    den Wert als String schreibt (``"False"`` → ``bool("False")`` wäre
    fälschlich ``True``) fangen wir das ab.

    Akzeptierte String-Werte (case-insensitive):
      Wahr:  ``"true"``, ``"yes"``, ``"1"``, ``"on"``
      Falsch: ``"false"``, ``"no"``, ``"0"``, ``"off"``, ``""``
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "1", "on"):
            return True
        if v in ("false", "no", "0", "off", ""):
            return False
        return bool(v)  # Fallback: non-empty string → True
    if value is None:
        return default
    return bool(value)


# ---- Default search order for config.yaml ----
_CONFIG_SEARCH_PATHS = [
    Path.cwd() / "config.yaml",
    _hermes_home() / "cortex" / "config.yaml",
    # Fallback: wenn _hermes_home() auf ein Profil zeigt (Worker),
    # nutze die Default-Config (~/.hermes/cortex/config.yaml)
    Path.home() / ".hermes" / "cortex" / "config.yaml",
    Path.home() / ".config" / "hermes-cortex" / "config.yaml",
]


def _expand(p: Optional[str]) -> Optional[Path]:
    """Expand ~ and env vars; return Path or None."""
    if p is None:
        return None
    return Path(os.path.expandvars(os.path.expanduser(str(p)))).resolve()


@dataclass
class VaultConfig:
    path: Path
    include_folders: list[str] = field(default_factory=list)
    exclude_folders: list[str] = field(default_factory=list)


@dataclass
class HermesMemoryConfig:
    memory_path: Optional[Path] = None
    user_path: Optional[Path] = None
    soul_path: Optional[Path] = None

    def available(self) -> dict[str, Path]:
        """Return only paths whose files actually exist."""
        out = {}
        for name, p in [("memory", self.memory_path), ("user", self.user_path), ("soul", self.soul_path)]:
            if p and p.exists():
                out[name] = p
        return out


@dataclass
class IndexConfig:
    chunks_path: Path
    chroma_path: Path
    collection: str = "cortex-vault"


@dataclass
class EmbeddingsConfig:
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    device: str = "cpu"


@dataclass
class SearchConfig:
    top_k: int = 10
    bm25_weight: float = 0.5
    vector_weight: float = 0.5
    rrf_k: int = 60
    # Per-channel candidate pool size = top_k * fetch_multiplier. Larger pools
    # give RRF more headroom to surface chunks that aren't top in either channel
    # alone but rank well in both. 6 is a sane default (e.g. 60 candidates per
    # channel for top_k=10) with negligible cost on small vaults.
    fetch_multiplier: int = 6
    wikilink_traversal: int = 1
    # Graph channel weight in the RRF fusion. The graph channel collects
    # wikilink-linked chunks of the top-N base hits (BM25 ∪ Vector after
    # filters/boosts would apply) and contributes them as a 3rd RRF
    # channel with this weight. Default is intentionally small so the
    # graph nudges ranking without dominating it. Set to 0.0 to disable
    # the channel entirely (equivalent to wikilink_traversal=0).
    graph_weight: float = 0.2
    # Recency boost: multiplicative, half-life decay.
    # final_score *= 1 + recency_max_boost * exp(-ln(2) * age_days / recency_half_life_days)
    # Source date: last_verified if present, else modified_date.
    recency_boost: bool = True
    recency_max_boost: float = 0.20         # +20% for a brand-new note
    recency_half_life_days: float = 90.0    # 90d → 50%, 180d → 25%, 365d → ~6%
    # Importance boost: multiplicative, linear in normalized importance.
    # importance is 1..5; we use (importance - 1) / 4 in [0, 1].
    # Missing importance → neutral (factor = 0), NOT default 3.0.
    importance_boost: bool = True
    importance_max_boost: float = 0.30      # +30% for importance=5
    # Cap the combined recency * importance multiplier. This keeps metadata
    # boosts as a small nudge instead of letting them dominate relevance.
    max_boost_multiplier: float = 1.20
    # Penalize obvious link/related/reference chunks for normal content
    # queries. Explicit link/relation queries skip this penalty.
    link_chunk_penalty: float = 0.75


@dataclass
class ContextBuilderConfig:
    token_budget: int = 4000
    cite_sources: bool = True
    include_hermes_memory: bool = False


@dataclass
class CronNightlyPromotionConfig:
    enabled: bool = False
    name: str = "hermes-cortex-nightly-promotion"
    schedule: str = "0 2 * * *"
    timezone: str = "Europe/Berlin"
    deliver: str = "origin"
    enabled_toolsets: list[str] = field(default_factory=lambda: ["file", "terminal"])
    lookback_days: int = 1
    state_db_path: str = "~/.hermes/state.db"
    legacy_fallback_enabled: bool = True
    session_globs: list[str] = field(default_factory=lambda: [
        "~/.hermes/sessions/*.jsonl",
        "~/.hermes/sessions/session_*.json",
    ])
    dry_run_first: bool = True


@dataclass
class CronWeeklyReviewConfig:
    enabled: bool = False
    name: str = "hermes-cortex-weekly-review"
    schedule: str = "0 8 * * 1"
    timezone: str = "Europe/Berlin"
    deliver: str = "origin"
    output_format: str = "markdown"
    dry_run: bool = True
    stale_days: int = 180
    stale_min_importance: float = 4.0
    consolidation_min_degree: int = 3


@dataclass
class CronConfig:
    nightly_promotion: CronNightlyPromotionConfig = field(default_factory=CronNightlyPromotionConfig)
    weekly_review: CronWeeklyReviewConfig = field(default_factory=CronWeeklyReviewConfig)


@dataclass
class HooksConfig:
    """Lifecycle hooks for Hermes plugin integration.

    The two runtime hooks are configured independently:
    - ``cache_warm`` controls ``on_session_start`` searcher cache warming.
    - ``context_injection`` controls ``pre_llm_call`` vault context and optional
      memory-query-flow skill injection.
    """
    cache_warm_enabled: bool = False
    context_injection_enabled: bool = False
    context_injection_budget: int = 1000   # token budget for injected vault context
    context_injection_query: str = ""      # empty → auto-derive from user message
    load_skill: bool = False               # auto-load memory-query-flow skill
    skill_path: str = ""                   # empty → default profile skill path


@dataclass
class Config:
    vault: VaultConfig
    hermes_memory: HermesMemoryConfig
    index: IndexConfig
    embeddings: EmbeddingsConfig
    search: SearchConfig
    context_builder: ContextBuilderConfig
    cron: CronConfig = field(default_factory=CronConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    log_level: str = "INFO"
    source_path: Optional[Path] = None  # where this config was loaded from


class ConfigError(Exception):
    pass


def _as_str_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{field_name} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{field_name} must contain only non-empty strings")
        out.append(item.strip())
    return out


def _validate_cron_nightly_promotion(c: "CronNightlyPromotionConfig", cfg_path: Path) -> None:
    errors: list[str] = []
    if not c.name.strip():
        errors.append("cron.nightly_promotion.name must be a non-empty string")
    if not c.schedule.strip():
        errors.append("cron.nightly_promotion.schedule must be a non-empty string")
    elif len(c.schedule.split()) not in (5, 6):
        errors.append(
            "cron.nightly_promotion.schedule must be a 5- or 6-field cron expression "
            f"(got {c.schedule!r})"
        )
    if not c.timezone.strip():
        errors.append("cron.nightly_promotion.timezone must be a non-empty IANA timezone")
    else:
        try:
            ZoneInfo(c.timezone)
        except ZoneInfoNotFoundError:
            errors.append(f"cron.nightly_promotion.timezone is not a valid IANA timezone: {c.timezone}")
    if not c.deliver.strip():
        errors.append("cron.nightly_promotion.deliver must be a non-empty delivery target")
    if c.lookback_days < 1:
        errors.append(
            f"cron.nightly_promotion.lookback_days must be >= 1 (got {c.lookback_days})"
        )
    if not c.state_db_path.strip():
        errors.append("cron.nightly_promotion.state_db_path must be a non-empty path")
    if not c.enabled_toolsets:
        errors.append("cron.nightly_promotion.enabled_toolsets must contain at least one toolset")
    if not c.session_globs:
        errors.append("cron.nightly_promotion.session_globs must contain at least one glob")
    if errors:
        raise ConfigError(f"{cfg_path}: invalid cron.nightly_promotion config:\n  - " + "\n  - ".join(errors))


def _validate_cron_weekly_review(c: "CronWeeklyReviewConfig", cfg_path: Path) -> None:
    errors: list[str] = []
    if not c.name.strip():
        errors.append("cron.weekly_review.name must be a non-empty string")
    if not c.schedule.strip():
        errors.append("cron.weekly_review.schedule must be a non-empty string")
    elif len(c.schedule.split()) not in (5, 6):
        errors.append(
            "cron.weekly_review.schedule must be a 5- or 6-field cron expression "
            f"(got {c.schedule!r})"
        )
    if not c.timezone.strip():
        errors.append("cron.weekly_review.timezone must be a non-empty IANA timezone")
    else:
        try:
            ZoneInfo(c.timezone)
        except ZoneInfoNotFoundError:
            errors.append(f"cron.weekly_review.timezone is not a valid IANA timezone: {c.timezone}")
    if not c.deliver.strip():
        errors.append("cron.weekly_review.deliver must be a non-empty delivery target")
    if c.output_format != "markdown":
        errors.append(f"cron.weekly_review.output_format must be 'markdown' (got {c.output_format!r})")
    if c.stale_days < 1:
        errors.append(f"cron.weekly_review.stale_days must be >= 1 (got {c.stale_days})")
    if c.stale_min_importance < 0:
        errors.append(
            f"cron.weekly_review.stale_min_importance must be >= 0 (got {c.stale_min_importance})"
        )
    if c.consolidation_min_degree < 1:
        errors.append(
            "cron.weekly_review.consolidation_min_degree must be >= 1 "
            f"(got {c.consolidation_min_degree})"
        )
    if errors:
        raise ConfigError(f"{cfg_path}: invalid cron.weekly_review config:\n  - " + "\n  - ".join(errors))


def _validate_search(s: "SearchConfig", cfg_path: Path) -> None:
    """Sanity-check search weights/ranges. Loud failure on invalid values.

    Phase 3 will use these directly in scoring; bad inputs would silently
    produce nonsense (e.g. negative weights, rrf_k=0 → division by zero).
    """
    errors: list[str] = []
    if s.top_k <= 0:
        errors.append(f"search.top_k must be > 0 (got {s.top_k})")
    if s.bm25_weight < 0:
        errors.append(f"search.bm25_weight must be >= 0 (got {s.bm25_weight})")
    if s.vector_weight < 0:
        errors.append(f"search.vector_weight must be >= 0 (got {s.vector_weight})")
    if s.bm25_weight == 0 and s.vector_weight == 0:
        errors.append("search.bm25_weight and search.vector_weight cannot both be 0")
    total = s.bm25_weight + s.vector_weight
    # We don't force exact 1.0 — RRF is the primary fusion; weights are a
    # tunable post-fusion knob. But complain loudly on suspicious values.
    if total > 0 and not (0.5 <= total <= 2.0):
        errors.append(
            f"search.bm25_weight + search.vector_weight = {total:.2f} is outside the "
            f"sane range 0.5..2.0; weights are typically near 1.0 in sum"
        )
    if s.rrf_k <= 0:
        errors.append(f"search.rrf_k must be > 0 (got {s.rrf_k}); 60 is the standard default")
    if s.wikilink_traversal < 0:
        errors.append(f"search.wikilink_traversal must be >= 0 (got {s.wikilink_traversal})")
    if s.wikilink_traversal > 3:
        errors.append(
            f"search.wikilink_traversal={s.wikilink_traversal} is impractically large; "
            f"keep it at 1 or 2 for graph expansion"
        )
    if s.graph_weight < 0:
        errors.append(
            f"search.graph_weight must be >= 0 (got {s.graph_weight})"
        )
    if s.graph_weight > 5.0:
        errors.append(
            f"search.graph_weight={s.graph_weight} is implausibly large; "
            f"typical values are 0.0..0.5"
        )
    if s.fetch_multiplier <= 0:
        errors.append(
            f"search.fetch_multiplier must be > 0 (got {s.fetch_multiplier}); "
            f"6 is the standard default"
        )
    if s.fetch_multiplier > 100:
        errors.append(
            f"search.fetch_multiplier={s.fetch_multiplier} is impractically large; "
            f"per-channel pool size = top_k * fetch_multiplier"
        )
    if s.recency_max_boost < 0:
        errors.append(
            f"search.recency_max_boost must be >= 0 (got {s.recency_max_boost})"
        )
    if s.recency_max_boost > 5.0:
        errors.append(
            f"search.recency_max_boost={s.recency_max_boost} is implausibly large; "
            f"typical values are 0.1..0.5"
        )
    if s.recency_half_life_days <= 0:
        errors.append(
            f"search.recency_half_life_days must be > 0 (got {s.recency_half_life_days}); "
            f"90 is the default"
        )
    if s.importance_max_boost < 0:
        errors.append(
            f"search.importance_max_boost must be >= 0 (got {s.importance_max_boost})"
        )
    if s.importance_max_boost > 5.0:
        errors.append(
            f"search.importance_max_boost={s.importance_max_boost} is implausibly large; "
            f"typical values are 0.1..0.5"
        )
    if s.max_boost_multiplier < 1.0:
        errors.append(
            f"search.max_boost_multiplier must be >= 1.0 (got {s.max_boost_multiplier})"
        )
    if s.max_boost_multiplier > 2.0:
        errors.append(
            f"search.max_boost_multiplier={s.max_boost_multiplier} is implausibly large; "
            f"typical values are 1.0..1.25"
        )
    if not (0.0 < s.link_chunk_penalty <= 1.0):
        errors.append(
            f"search.link_chunk_penalty must be in (0, 1] (got {s.link_chunk_penalty})"
        )
    if errors:
        raise ConfigError(f"{cfg_path}: invalid search config:\n  - " + "\n  - ".join(errors))


def find_config() -> Optional[Path]:
    """Locate config.yaml using the default search order. Returns None if not found."""
    env = os.environ.get("CORTEX_CONFIG")
    if env:
        p = _expand(env)
        if p and p.exists():
            return p
        raise ConfigError(f"CORTEX_CONFIG points to non-existent file: {env}")
    for candidate in _CONFIG_SEARCH_PATHS:
        if candidate.exists():
            return candidate
    return None


def load_config(path: Optional[str | Path] = None) -> Config:
    """Load configuration. If `path` is None, auto-discover via find_config().

    Raises ConfigError if no config can be located or required fields are missing.
    """
    if path is not None:
        cfg_path = _expand(str(path))
        if not cfg_path or not cfg_path.exists():
            raise ConfigError(f"Config file not found: {path}")
    else:
        cfg_path = find_config()
        if cfg_path is None:
            raise ConfigError(
                "No config.yaml found. Searched: "
                + ", ".join(str(p) for p in _CONFIG_SEARCH_PATHS)
                + ". Set CORTEX_CONFIG or copy config.example.yaml to one of these locations."
            )

    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # ---- vault ----
    v = raw.get("vault") or {}
    if "path" not in v:
        raise ConfigError(f"{cfg_path}: vault.path is required")
    vault = VaultConfig(
        path=_expand(v["path"]),
        include_folders=list(v.get("include_folders") or []),
        exclude_folders=list(v.get("exclude_folders") or []),
    )

    # ---- hermes_memory (all optional) ----
    hm = raw.get("hermes_memory") or {}
    hermes_memory = HermesMemoryConfig(
        memory_path=_expand(hm.get("memory_path")),
        user_path=_expand(hm.get("user_path")),
        soul_path=_expand(hm.get("soul_path")),
    )

    # ---- index ----
    idx = raw.get("index") or {}
    if "chunks_path" not in idx or "chroma_path" not in idx:
        raise ConfigError(f"{cfg_path}: index.chunks_path and index.chroma_path are required")
    index = IndexConfig(
        chunks_path=_expand(idx["chunks_path"]),
        chroma_path=_expand(idx["chroma_path"]),
        collection=idx.get("collection", "cortex-vault"),
    )

    # ---- embeddings ----
    emb = raw.get("embeddings") or {}
    embeddings = EmbeddingsConfig(
        model=emb.get("model", "sentence-transformers/all-MiniLM-L6-v2"),
        device=emb.get("device", "cpu"),
    )

    # ---- search ----
    s = raw.get("search") or {}
    search = SearchConfig(
        top_k=int(s.get("top_k", 10)),
        bm25_weight=float(s.get("bm25_weight", 0.5)),
        vector_weight=float(s.get("vector_weight", 0.5)),
        rrf_k=int(s.get("rrf_k", 60)),
        fetch_multiplier=int(s.get("fetch_multiplier", 6)),
        wikilink_traversal=int(s.get("wikilink_traversal", 1)),
        graph_weight=float(s.get("graph_weight", 0.2)),
        recency_boost=_to_bool(s.get("recency_boost"), default=True),
        recency_max_boost=float(s.get("recency_max_boost", 0.20)),
        recency_half_life_days=float(s.get("recency_half_life_days", 90.0)),
        importance_boost=_to_bool(s.get("importance_boost"), default=True),
        importance_max_boost=float(s.get("importance_max_boost", 0.30)),
        max_boost_multiplier=float(s.get("max_boost_multiplier", 1.20)),
        link_chunk_penalty=float(s.get("link_chunk_penalty", 0.75)),
    )
    _validate_search(search, cfg_path)

    # ---- context_builder ----
    cb = raw.get("context_builder") or {}
    context_builder = ContextBuilderConfig(
        token_budget=int(cb.get("token_budget", 4000)),
        cite_sources=_to_bool(cb.get("cite_sources"), default=True),
        include_hermes_memory=_to_bool(cb.get("include_hermes_memory"), default=False),
    )

    # ---- cron ----
    cron_raw = raw.get("cron") or {}
    np_raw = cron_raw.get("nightly_promotion") or {}
    default_np = CronNightlyPromotionConfig()
    nightly_promotion = CronNightlyPromotionConfig(
        enabled=_to_bool(np_raw.get("enabled"), default=default_np.enabled),
        name=str(np_raw.get("name", default_np.name)).strip(),
        schedule=str(np_raw.get("schedule", default_np.schedule)).strip(),
        timezone=str(np_raw.get("timezone", default_np.timezone)).strip(),
        deliver=str(np_raw.get("deliver", default_np.deliver)).strip(),
        enabled_toolsets=_as_str_list(
            np_raw.get("enabled_toolsets", default_np.enabled_toolsets),
            "cron.nightly_promotion.enabled_toolsets",
        ),
        lookback_days=int(np_raw.get("lookback_days", default_np.lookback_days)),
        state_db_path=str(np_raw.get("state_db_path", default_np.state_db_path)).strip(),
        legacy_fallback_enabled=_to_bool(
            np_raw.get("legacy_fallback_enabled"),
            default=default_np.legacy_fallback_enabled,
        ),
        session_globs=_as_str_list(
            np_raw.get("session_globs", default_np.session_globs),
            "cron.nightly_promotion.session_globs",
        ),
        dry_run_first=_to_bool(np_raw.get("dry_run_first"), default=default_np.dry_run_first),
    )
    _validate_cron_nightly_promotion(nightly_promotion, cfg_path)

    wr_raw = cron_raw.get("weekly_review") or {}
    default_wr = CronWeeklyReviewConfig()
    weekly_review = CronWeeklyReviewConfig(
        enabled=_to_bool(wr_raw.get("enabled"), default=default_wr.enabled),
        name=str(wr_raw.get("name", default_wr.name)).strip(),
        schedule=str(wr_raw.get("schedule", default_wr.schedule)).strip(),
        timezone=str(wr_raw.get("timezone", default_wr.timezone)).strip(),
        deliver=str(wr_raw.get("deliver", default_wr.deliver)).strip(),
        output_format=str(wr_raw.get("output_format", default_wr.output_format)).strip(),
        dry_run=_to_bool(wr_raw.get("dry_run"), default=default_wr.dry_run),
        stale_days=int(wr_raw.get("stale_days", default_wr.stale_days)),
        stale_min_importance=float(wr_raw.get("stale_min_importance", default_wr.stale_min_importance)),
        consolidation_min_degree=int(
            wr_raw.get("consolidation_min_degree", default_wr.consolidation_min_degree)
        ),
    )
    _validate_cron_weekly_review(weekly_review, cfg_path)
    cron = CronConfig(nightly_promotion=nightly_promotion, weekly_review=weekly_review)

    # ---- hooks ----
    hk = raw.get("hooks") or {}
    cache_warm = hk.get("cache_warm") or {}
    context_injection = hk.get("context_injection") or {}
    hooks = HooksConfig(
        cache_warm_enabled=_to_bool(cache_warm.get("enabled"), default=False),
        context_injection_enabled=_to_bool(context_injection.get("enabled"), default=False),
        context_injection_budget=int(context_injection.get("budget", 1000)),
        context_injection_query=str(context_injection.get("query", "")),
        load_skill=_to_bool(context_injection.get("load_skill"), default=False),
        skill_path=str(context_injection.get("skill_path", "")),
    )

    log_level = (raw.get("logging") or {}).get("level", "INFO")

    return Config(
        vault=vault,
        hermes_memory=hermes_memory,
        index=index,
        embeddings=embeddings,
        search=search,
        context_builder=context_builder,
        cron=cron,
        hooks=hooks,
        log_level=log_level,
        source_path=cfg_path,
    )
