"""Hermes directory-plugin runtime for hermes-cortex.

Hermes loads this repository as a standalone directory plugin from
``~/.hermes/plugins/cortex``.  The root ``__init__.py`` calls ``register(ctx)``
from this module, which registers the vault tools and lifecycle hooks.

Hermes loads this plugin directly from the plugin checkout; no editable install
or separate Python entry point is required.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from cortex.config import _hermes_home
from cortex.text import estimate_tokens

from cortex.plugin import (
    CortexToolError,
    vault_build_context as _vault_build_context,
    vault_read_note as _vault_read_note,
    vault_search as _vault_search,
)

_log = logging.getLogger("cortex.plugin_runtime")


def _config_path() -> str | None:
    """Resolve cortex config path — env var overrides default."""
    return os.environ.get("CORTEX_CONFIG") or None


def _wrap(fn, **kwargs) -> str:
    """Call a ``cortex.plugin`` function and wrap result as JSON string.

    All Hermes tool handlers must return a JSON string. This adapter
    translates ``CortexToolError`` and generic exceptions into the
    ``{\"success\": false, \"error\": ...}`` envelope.
    """
    try:
        result = fn(**kwargs)
    except CortexToolError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"{type(e).__name__}: {e}"},
            ensure_ascii=False,
        )

    payload = {"success": True}
    payload.update(result)
    return json.dumps(payload, ensure_ascii=False, default=str)


# ---- Tool handlers -----------------------------------------------------------


def _vault_search_handler(args: dict[str, Any], **kw) -> str:
    return _wrap(
        _vault_search,
        query=args.get("query", ""),
        top_k=args.get("top_k"),
        filters=args.get("filters"),
        apply_boost=args.get("apply_boost"),
        config_path=_config_path(),
    )


def _vault_read_note_handler(args: dict[str, Any], **kw) -> str:
    return _wrap(
        _vault_read_note,
        file=args.get("file", ""),
        heading_path=args.get("heading_path"),
        config_path=_config_path(),
    )


def _vault_build_context_handler(args: dict[str, Any], **kw) -> str:
    return _wrap(
        _vault_build_context,
        query=args.get("query", ""),
        top_k=args.get("top_k"),
        budget=args.get("budget"),
        filters=args.get("filters"),
        apply_boost=args.get("apply_boost"),
        include_hermes_memory=args.get("include_hermes_memory"),
        config_path=_config_path(),
    )


# ---- Tool schemas ------------------------------------------------------------

VAULT_SEARCH_SCHEMA = {
    "name": "vault_search",
    "description": "Hybrid search over the indexed Obsidian vault.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
            "filters": {"type": "object"},
            "apply_boost": {"type": "boolean"},
        },
        "required": ["query"],
    },
}

VAULT_READ_NOTE_SCHEMA = {
    "name": "vault_read_note",
    "description": (
        "Read the full content of a vault note, optionally limited to "
        "a heading path."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "heading_path": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["file"],
    },
}

VAULT_BUILD_CONTEXT_SCHEMA = {
    "name": "vault_build_context",
    "description": (
        "Search the vault and build a token-budgeted Markdown context blob."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
            "budget": {"type": "integer"},
            "filters": {"type": "object"},
            "apply_boost": {"type": "boolean"},
            "include_hermes_memory": {"type": "boolean"},
        },
        "required": ["query"],
    },
}


# ---- Lifecycle hooks ---------------------------------------------------------

_HOOK_CONFIG_CACHE: dict[str, Any] = {}


def _load_hooks_config() -> dict[str, Any]:
    """Load cortex config and extract lifecycle hook settings."""
    from cortex.config import load_config
    try:
        cfg = load_config(_config_path())
        h = cfg.hooks
        return {
            "hooks": h,
            "cache_warm_enabled": h.cache_warm_enabled,
            "context_injection_enabled": h.context_injection_enabled,
            "budget": h.context_injection_budget,
            "query": h.context_injection_query,
            "load_skill": h.load_skill,
            "skill_path": h.skill_path,
        }
    except Exception as exc:
        _log.warning("Failed to load hooks config: %s", exc)
        return {
            "hooks": None,
            "cache_warm_enabled": False,
            "context_injection_enabled": False,
            "budget": 1000,
            "query": "",
            "load_skill": True,
            "skill_path": "",
        }


def _on_session_start(session_id: str = "", model: str = "", platform: str = "", **kwargs) -> None:
    """Warm the cortex searcher cache when a new Hermes session starts.

    Skips cache warming for Kanban workers (``HERMES_KANBAN_TASK`` set)
    — short-lived sessions where vault_search may never be called.
    Logs kanban context for diagnostics regardless.
    """
    # Store config for pre_llm_call (session-scoped usage)
    cfg = _load_hooks_config()
    _HOOK_CONFIG_CACHE["config"] = cfg

    # ── Kanban-Detection ─────────────────────────────────────────────
    kanban_task = os.environ.get("HERMES_KANBAN_TASK", "")
    kanban_board = os.environ.get("HERMES_KANBAN_BOARD", "")
    if kanban_task:
        _log.info(
            "Kanban worker session — task=%s board=%s platform=%s (cache warm skipped)",
            kanban_task, kanban_board or "default", platform or "?",
        )
        return  # Don't warm cache for short-lived worker sessions
    # ─────────────────────────────────────────────────────────────────

    if not cfg.get("cache_warm_enabled", True):
        _log.debug("Searcher cache warm disabled by hooks.cache_warm.enabled")
        return

    try:
        from cortex.plugin import _resolve_state, reset_cache
        reset_cache()
        _resolve_state(_config_path())
        _log.info("Searcher cache warmed for session %s", session_id[:12])
    except Exception as exc:
        _log.debug("Cache warm skipped: %s", exc)


# ── Standard-Pfad für den memory-query-flow Skill ──────────────
_DEFAULT_SKILL_PATH = str(
    _hermes_home() / "skills" / "long-term-memory" / "memory-query-flow" / "SKILL.md"
)

_SKILL_MARKER_START = "<!-- cortex-hook: memory-query-flow START -->"
_SKILL_MARKER_END = "<!-- cortex-hook: memory-query-flow END -->"


def _load_skill_content(skill_path: str) -> str | None:
    """Read skill SKILL.md and wrap with markers.

    Returns formatted string or None on failure.
    """
    path = skill_path.strip() or _DEFAULT_SKILL_PATH
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        return (
            f"\n\n{_SKILL_MARKER_START}\n"
            f"## Auto-loaded Skill: memory-query-flow\n"
            f"{content}\n"
            f"{_SKILL_MARKER_END}"
        )
    except (FileNotFoundError, OSError, IOError) as exc:
        _log.debug("Skill load skipped (%s): %s", path, exc)
        return None


def _truncate_utf8_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip(), True


def _truncate_token_budget(text: str, budget: int) -> tuple[str, bool]:
    if estimate_tokens(text) <= budget:
        return text, False
    # estimate_tokens is char based, so this keeps the rendered text under the
    # requested approximate token budget without pulling in a tokenizer.
    limit = max(1, budget * 4)
    while limit > 1 and estimate_tokens(text[:limit]) > budget:
        limit -= max(1, limit // 10)
    return text[:limit].rstrip(), True


def _diagnostic(message: str) -> str:
    return f"[cortex hook diagnostic: {message}]"


def _hook_due(when: str, is_first_turn: bool) -> bool:
    """Return whether a semantic pre_llm hook should run for this turn."""
    if when == "first_turn":
        return is_first_turn
    return True


def _render_static_files(hooks: Any) -> list[str]:
    parts: list[str] = []
    tokens_used = 0
    total_budget = int(getattr(hooks.bootstrap_context, "budget", 0) or 0)
    for entry in hooks.ordered_static_files():
        label = entry.label
        path = entry.path
        source = str(path) if path is not None else "(missing path)"
        if path is None or not path.exists() or not path.is_file():
            if entry.optional:
                parts.append(_diagnostic(
                    f"skipped static file {label!r} from {source}: optional file missing"
                ))
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            parts.append(_diagnostic(
                f"skipped static file {label!r} from {source}: read failed: {exc}"
            ))
            continue
        if not text:
            parts.append(_diagnostic(
                f"skipped static file {label!r} from {source}: file is empty"
            ))
            continue

        notes: list[str] = []
        if entry.max_bytes is not None:
            text, truncated = _truncate_utf8_bytes(text, int(entry.max_bytes))
            if truncated:
                notes.append(f"truncated to max_bytes={entry.max_bytes}")
        if entry.budget is not None:
            text, truncated = _truncate_token_budget(text, int(entry.budget))
            if truncated:
                notes.append(f"truncated to budget={entry.budget}")
        if total_budget > 0:
            remaining = total_budget - tokens_used
            if remaining <= 0:
                parts.append(_diagnostic(
                    f"skipped static file {label!r} from {source}: bootstrap budget exhausted"
                ))
                continue
            text, truncated = _truncate_token_budget(text, remaining)
            if truncated:
                notes.append(f"truncated to remaining bootstrap budget={remaining}")
        cost = estimate_tokens(text)
        tokens_used += cost

        meta = f"label={label}; source={source}"
        if entry.max_bytes is not None:
            meta += f"; max_bytes={entry.max_bytes}"
        if entry.budget is not None:
            meta += f"; budget={entry.budget}"
        if notes:
            meta += f"; {'; '.join(notes)}"
        parts.append(f"## Static Context: {label}\nSource: {source}\n{meta}\n\n{text}")
    return parts


def _pre_llm_call_semantic(hooks: Any, user_message: str, is_first_turn: bool) -> str | None:
    parts: list[str] = []

    skill = hooks.skill_context
    if skill.enabled and skill.load_skill and _hook_due(skill.when, is_first_turn):
        skill_text = _load_skill_content(skill.skill_path)
        if skill_text:
            parts.append(skill_text)
        else:
            parts.append(_diagnostic(
                f"skipped skill_context: skill unavailable at {skill.skill_path or _DEFAULT_SKILL_PATH}"
            ))
    elif skill.enabled and skill.load_skill:
        _log.debug("Skill context skipped after first turn")

    bootstrap = hooks.bootstrap_context
    if bootstrap.enabled and _hook_due(bootstrap.when, is_first_turn):
        parts.extend(_render_static_files(hooks))
    elif bootstrap.enabled:
        _log.debug("Bootstrap context skipped after first turn")

    recent = hooks.recent_context
    if recent.enabled and _hook_due(recent.when, is_first_turn):
        try:
            from cortex.recent_context import build_recent_context, render_diagnostics

            result = build_recent_context(recent)
            if result.text:
                parts.append(result.text)
                if recent.diagnostics:
                    parts.append(_diagnostic(f"recent_context: {render_diagnostics(result.diagnostics)}"))
            else:
                parts.append(_diagnostic(f"skipped recent_context: {render_diagnostics(result.diagnostics)}"))
            if recent.query_hint and result.query_hint:
                parts.append(f"[recent_context query_hint]\n{result.query_hint}")
        except Exception as exc:
            parts.append(_diagnostic(f"skipped recent_context: {type(exc).__name__}: {exc}"))
    elif recent.enabled:
        _log.debug("Recent context skipped after first turn")

    dynamic = hooks.dynamic_context
    if dynamic.enabled:
        query = dynamic.query.strip() or user_message.strip()
        if query:
            budget = int(dynamic.budget)
            if budget > 0:
                try:
                    result = _vault_build_context(
                        query=query,
                        budget=budget,
                        config_path=_config_path(),
                    )
                    text = (result or {}).get("text", "").strip()
                    if text:
                        parts.append(f"[Vault context from hermes-cortex]\n{text}")
                        _log.info(
                            "Injected %d tokens of vault context (budget=%d)",
                            (result or {}).get("tokens_used", 0),
                            budget,
                        )
                except Exception as exc:
                    _log.warning("Context injection failed: %s", exc)
                    parts.append(_diagnostic(f"skipped dynamic_context: {exc}"))
        else:
            parts.append(_diagnostic("skipped dynamic_context: empty query"))

    return "\n\n".join(parts) if parts else None


def _pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    is_first_turn: bool = False,
    **kwargs,
) -> str | None:
    """Inject vault context and/or memory-query-flow skill every turn.

    Returns a Markdown string of relevant context, or ``None``
    to skip injection.  Controlled by ``hooks.context_injection`` in
    ``~/.hermes/cortex/config.yaml``.

    • Vault-Context (query/budget) is injected every turn when enabled.
    • Skill-Loading (load_skill) follows the same rhythm, but can be disabled via
      ``hooks.context_injection.load_skill: false``.

    Note: does NOT inject any kanban-specific skills.  Workers
    receive their skills via the Hermes dispatcher (``--skills``)
    or per-task assignment (``kanban_create(skills=[...])``).
    """
    cfg = _HOOK_CONFIG_CACHE.get("config") or _load_hooks_config()
    hooks = cfg.get("hooks")
    if hooks is not None and (
        getattr(hooks, "semantic_context_present", False)
        or not getattr(hooks, "legacy_context_injection_present", False)
    ):
        return _pre_llm_call_semantic(hooks, user_message, is_first_turn)

    if not cfg.get("context_injection_enabled"):
        return None

    parts: list[str] = []

    # 1. Skill-Inhalt: memory-query-flow (jeden Turn)
    if cfg.get("load_skill", True):
        skill_text = _load_skill_content(cfg.get("skill_path", ""))
        if skill_text:
            parts.append(skill_text)

    # 2. Vault-Kontext (jeden Turn — nicht mehr auf first_turn beschränkt)
    # Derive query: explicit from config, or auto from user's message
    query = cfg.get("query", "") or user_message.strip()
    if query:
        budget = int(cfg.get("budget", 1000))
        if budget > 0:
            try:
                result = _vault_build_context(
                    query=query,
                    budget=budget,
                    config_path=_config_path(),
                )
                text = (result or {}).get("text", "").strip()
                if text:
                    parts.append(f"[Vault context from hermes-cortex]\n{text}")
                    _log.info(
                        "Injected %d tokens of vault context (budget=%d)",
                        (result or {}).get("tokens_used", 0),
                        budget,
                    )
            except Exception as exc:
                _log.warning("Context injection failed: %s", exc)

    if not parts:
        return None

    return "\n\n".join(parts)


# ---- Plugin registration -----------------------------------------------------


def _setup_cortex_cli(parser) -> None:
    """Attach the cortex CLI parser to ``hermes cortex``."""
    from cortex.cli import configure_parser

    parser.description = "hermes-cortex vault indexing, embedding, search, and context CLI"
    configure_parser(parser)


def _handle_cortex_cli(args) -> int:
    """Dispatch ``hermes cortex ...`` to the selected cortex subcommand."""
    return args.func(args)


def register(ctx):
    """Called by Hermes PluginManager during ``discover_plugins()``."""
    ctx.register_tool(
        name="vault_search",
        toolset="cortex",
        schema=VAULT_SEARCH_SCHEMA,
        handler=_vault_search_handler,
        check_fn=lambda: True,
        description="Hybrid search over hermes-cortex vault index.",
    )

    ctx.register_tool(
        name="vault_read_note",
        toolset="cortex",
        schema=VAULT_READ_NOTE_SCHEMA,
        handler=_vault_read_note_handler,
        check_fn=lambda: True,
        description="Read a full Obsidian vault note via hermes-cortex.",
    )

    ctx.register_tool(
        name="vault_build_context",
        toolset="cortex",
        schema=VAULT_BUILD_CONTEXT_SCHEMA,
        handler=_vault_build_context_handler,
        check_fn=lambda: True,
        description="Build grounded Markdown context from hermes-cortex.",
    )

    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _pre_llm_call)

    ctx.register_cli_command(
        name="cortex",
        help="Manage hermes-cortex vault index/search/context",
        description="hermes-cortex vault indexing, embedding, search, and context CLI",
        setup_fn=_setup_cortex_cli,
        handler_fn=_handle_cortex_cli,
    )
