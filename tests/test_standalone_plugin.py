"""Smoke tests for the standalone Hermes directory plugin layout."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from datetime import date
from pathlib import Path

import plugin_runtime


def test_plugin_runtime_wrap_serializes_frontmatter_dates() -> None:
    out = plugin_runtime._wrap(lambda: {"frontmatter": {"created": date(2026, 5, 12)}})
    payload = json.loads(out)
    assert payload["success"] is True
    assert payload["frontmatter"]["created"] == "2026-05-12"


def test_repo_root_loads_as_hermes_directory_plugin() -> None:
    repo = Path(__file__).resolve().parents[1]
    script = textwrap.dedent(
        f"""
        import importlib.util
        import sys
        import types
        from pathlib import Path

        repo = Path({str(repo)!r})
        # Simulate Hermes PluginManager directory loading without relying on
        # the repo being importable through cwd/PYTHONPATH.
        sys.path = [p for p in sys.path if p and Path(p).resolve() != repo]
        for name in list(sys.modules):
            if name == "cortex" or name.startswith("cortex."):
                del sys.modules[name]

        parent = types.ModuleType("hermes_plugins")
        parent.__path__ = []
        parent.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = parent

        spec = importlib.util.spec_from_file_location(
            "hermes_plugins.cortex",
            repo / "__init__.py",
            submodule_search_locations=[str(repo)],
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "hermes_plugins.cortex"
        module.__path__ = [str(repo)]
        sys.modules["hermes_plugins.cortex"] = module
        spec.loader.exec_module(module)

        class Ctx:
            def __init__(self):
                self.tools = []
                self.hooks = []
                self.cli_commands = []
            def register_tool(self, **kwargs):
                self.tools.append(kwargs["name"])
            def register_hook(self, name, callback):
                self.hooks.append(name)
            def register_cli_command(self, **kwargs):
                self.cli_commands.append(kwargs["name"])

        ctx = Ctx()
        module.register(ctx)
        assert ctx.tools == ["vault_search", "vault_read_note", "vault_build_context"]
        assert "on_session_start" in ctx.hooks
        assert "pre_llm_call" in ctx.hooks
        assert ctx.cli_commands == ["cortex"]
        import cortex
        assert Path(cortex.__file__).resolve().is_relative_to(repo)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd="/tmp",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_runtime_plugin_cli_uses_full_cortex_parser_surface() -> None:
    """The Hermes directory plugin must expose the same top-level CLI as cortex.cli."""
    parser = argparse.ArgumentParser(prog="hermes cortex")

    plugin_runtime._setup_cortex_cli(parser)

    subparser_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(subparser_action.choices) == {
        "init",
        "index",
        "embed",
        "search",
        "search-eval",
        "validate-frontmatter",
        "context",
        "graph",
        "config",
        "status",
        "lifecycle",
        "cron",
        "reset",
    }

    # Regression guard for the active review finding: this used to exist in
    # cortex.cli but not in the Hermes runtime command surface.
    assert "search-eval" in subparser_action.choices
