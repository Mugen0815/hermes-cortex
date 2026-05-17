"""Hermes directory plugin entry point for hermes-cortex.

Hermes loads this repository root as ``~/.hermes/plugins/cortex`` and calls
``register(ctx)`` from here.  The actual registration implementation lives in
``plugin_runtime.py`` next to this file; the search/indexing engine lives in the
internal ``cortex`` package.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PLUGIN_DIR = Path(__file__).resolve().parent
_INNER_PACKAGE_DIR = _PLUGIN_DIR / "cortex"

if __name__ == "cortex":
    # Pytest and ad-hoc Python invocations executed from a plugin checkout named
    # ``cortex`` can import this root ``__init__.py`` as the top-level package.
    # In that mode, behave like the bundled inner package so ``import
    # cortex.cli`` still resolves to ``./cortex/cli.py`` instead of treating the
    # plugin root as the package.  Hermes directory-plugin loading uses a
    # namespaced module (``hermes_plugins.cortex``), so it takes the branch below.
    __file__ = str(_INNER_PACKAGE_DIR / "__init__.py")
    __path__ = [str(_INNER_PACKAGE_DIR), str(_PLUGIN_DIR)]  # type: ignore[name-defined]
    __package__ = "cortex"
    with open(__file__, encoding="utf-8") as _f:
        exec(compile(_f.read(), __file__, "exec"), globals())
else:
    # Hermes imports directory plugins as ``hermes_plugins.<slug>`` without
    # adding the plugin directory to sys.path.  The core package still
    # intentionally uses absolute ``cortex.*`` imports so the CLI package works
    # when pip-installed.  Bind that top-level package name to this checkout's
    # bundled package for the directory-plugin runtime.  This is an in-process
    # module alias, not a sys.path hack.
    try:
        from . import cortex as _cortex_package
    except ImportError:  # pytest imports repo-root __init__.py as a top-level module
        import importlib
        _cortex_package = importlib.import_module("cortex")

    _existing = sys.modules.get("cortex")
    _existing_file = Path(getattr(_existing, "__file__", "")).resolve() if _existing else None
    if _existing is None or _PLUGIN_DIR not in _existing_file.parents:
        sys.modules["cortex"] = _cortex_package


    def register(ctx: Any) -> None:
        """Register Cortex tools/hooks with Hermes' directory-plugin manager."""
        from .plugin_runtime import register as _register

        return _register(ctx)
