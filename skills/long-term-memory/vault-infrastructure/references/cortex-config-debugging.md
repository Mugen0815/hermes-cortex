# Cortex Config & Hooks Debugging

## Config path resolution

The cortex plugin finds config via `_config_path()` → `find_config()`:

```python
def _config_path() -> str | None:
    return os.environ.get("CORTEX_CONFIG") or None  # ONLY env var!
```

If `CORTEX_CONFIG` is unset, `load_config(None)` calls `find_config()`, which
searches in order:

```python
_CONFIG_SEARCH_PATHS = [
    Path.cwd() / "config.yaml",                           # 1. CWD — FRAGILE
    _hermes_home() / "cortex" / "config.yaml",            # 2. profile/default Hermes home
    Path.home() / ".hermes" / "cortex" / "config.yaml",   # 3. default fallback for profiles
    Path.home() / ".config" / "hermes-cortex" / "config.yaml", # 4. fallback
]
```

### Pitfalls

- **CWD wins.** If Hermes starts from a directory that has its own `config.yaml`
  (e.g. the cortex repo root, a project directory), THAT config is used instead
  of `~/.hermes/cortex/config.yaml`. There is no log message saying *which*
  config won.
- **No env-var fallback logging.** `_config_path()` returns `None` silently when
  `CORTEX_CONFIG` is not set. The caller has no way to distinguish "env var
  unset" from "env var explicitly pointing to nothing".

### Quick check

```bash
# Verify which config is actually loaded
python3 -c "
from cortex.config import find_config
p = find_config()
print(f'Active config: {p}')
import yaml; cfg = yaml.safe_load(p.read_text())
hooks = cfg.get('hooks', {})
print(f'Hooks section: {hooks}')
"
```

## Hook lifecycle

Two hooks are registered in `hermes_plugin.py`:

1. **`on_session_start`** — fires ONCE when a *new* session is created.
   - Warms the searcher cache (`reset_cache()` → `_resolve_state()`)
   - Stores hooks config in `_HOOK_CONFIG_CACHE["config"]`

2. **`pre_llm_call`** — fires every turn.
   - Reads from `_HOOK_CONFIG_CACHE` (or falls back to `_load_hooks_config()`)
   - Returns `None` if `enabled: false`
   - Returns vault context string otherwise; context injection is controlled by `hooks.context_injection.enabled`

### Timing dependency

```
on_session_start fires → _HOOK_CONFIG_CACHE["config"] = {...}
pre_llm_call fires     → cfg = _HOOK_CONFIG_CACHE.get("config") or _load_hooks_config()
```

**Once-broken-forever-broken trap:** If `_on_session_start()` raises in the
cache-warming block (lines 181–187 in hermes_plugin.py), the `except` catches
it and logs at DEBUG level only. But `_load_hooks_config()` on line 190 still
runs. If *that* also fails, it returns `{"enabled": False, ...}`. The entire
session then has hooks disabled.

### Verification via logs

```bash
# Check that both hooks fired
grep -E 'cortex\.(plugin_runtime|hermes_plugin):.*(warmed|Injected|hooks config)' ~/.hermes/logs/agent.log

# Expected output (existing session, new turn):
#   cortex.plugin_runtime: Searcher cache warmed for session <id>
#   cortex.plugin_runtime: Injected NNN tokens of vault context (budget=...)

# If "hooks config" is missing or shows enabled=False:
#   → _load_hooks_config() failed; check config.yaml syntax
#   → Or config at wrong path was loaded (see config path section above)
```

## Known issues (as of May 2026)

1. **No `source_path` logging.** `load_config()` stores `source_path` on the
   Config object but nothing logs it. Consider adding a one-liner in
   `_on_session_start` or `_load_hooks_config()`.
2. **`_config_path()` is a boolean trap.** It returns `None | str`. Downstream
   callers (`load_config(None)`) have different semantics than `load_config("")`.
   The function should arguably return `Optional[str]` with `None` meaning
   "autodiscover" and `""` being rejected explicitly.
3. **CWD-first search order.** `Path.cwd() / "config.yaml"` at position 0 in
   `_CONFIG_SEARCH_PATHS` is unexpected for most users. Moving it to position 2
   (after `~/.hermes/cortex/config.yaml`) would be safer.
