"""Shared helpers for the cortex CLI — config loading, filter flags, JSON output.

Extracted from ``cli.py`` to keep the main entry point manageable.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from cortex.filters import SearchFilters


def resolve_config(config_path: Optional[str]) -> Any:
    """Load config from *config_path* if given, else use standard lookup."""
    from cortex.config import load_config

    return load_config(config_path) if config_path else load_config()


def csv_list(s: str | None) -> list[str] | None:
    """Parse a CSV string ``"a,b,c"`` into ``["a", "b", "c"]``.

    Empty entries are dropped and ``None`` / ``""`` both return ``None`` so
    SearchFilters fields stay unset.
    """
    if not s:
        return None
    out = [p.strip() for p in s.split(",")]
    out = [p for p in out if p]
    return out or None


def build_filters_from_args(args: Any) -> SearchFilters:
    """Build a SearchFilters from the standard CLI filter flags."""
    return SearchFilters(
        type=csv_list(getattr(args, "type", None)),
        status=csv_list(getattr(args, "status", None)),
        domain=csv_list(getattr(args, "domain", None)),
        project=csv_list(getattr(args, "project", None)),
        folders=csv_list(getattr(args, "folder", None)),
        importance_min=getattr(args, "importance_min", None),
        importance_max=getattr(args, "importance_max", None),
        confidence_min=getattr(args, "confidence_min", None),
        confidence_max=getattr(args, "confidence_max", None),
        modified_after=getattr(args, "modified_after", None),
        modified_before=getattr(args, "modified_before", None),
        tags_any=csv_list(getattr(args, "tags_any", None)),
        tags_all=csv_list(getattr(args, "tags_all", None)),
        wikilinks_any=csv_list(getattr(args, "wikilinks_any", None)),
        wikilinks_all=csv_list(getattr(args, "wikilinks_all", None)),
    )


def add_filter_flags(p: Any) -> None:
    """Register the standard filter flags on a subparser."""
    from argparse import ArgumentParser

    if not isinstance(p, ArgumentParser):
        return
    p.add_argument("--type", type=str, help="Filter by type (CSV; OR within field)")
    p.add_argument("--status", type=str, help="Filter by status (CSV)")
    p.add_argument("--domain", type=str, help="Filter by domain (CSV)")
    p.add_argument("--project", type=str, help="Filter by project (CSV)")
    p.add_argument("--folder", type=str, help="Filter by top-level folder (CSV)")
    p.add_argument("--tags-any", type=str, help="Match any of these tags (CSV)")
    p.add_argument("--tags-all", type=str, help="Match all of these tags (CSV)")
    p.add_argument("--wikilinks-any", type=str, help="Match any of these wikilink targets (CSV)")
    p.add_argument("--wikilinks-all", type=str, help="Match all of these wikilink targets (CSV)")
    p.add_argument("--importance-min", type=float, help="Minimum importance (1..5)")
    p.add_argument("--importance-max", type=float, help="Maximum importance (1..5)")
    p.add_argument("--confidence-min", type=float, help="Minimum confidence (0..1)")
    p.add_argument("--confidence-max", type=float, help="Maximum confidence (0..1)")
    p.add_argument("--modified-after", type=str, help="ISO date YYYY-MM-DD (inclusive)")
    p.add_argument("--modified-before", type=str, help="ISO date YYYY-MM-DD (inclusive)")


def print_json(payload: Any) -> None:
    """Print *payload* as compact indented JSON to stdout."""
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def print_error(msg: str, exit_code: int = 1) -> int:
    """Print *msg* to stderr and return *exit_code*."""
    print(msg, file=sys.stderr)
    return exit_code
