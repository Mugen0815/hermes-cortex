"""CLI subcommands for ``cortex graph`` — build, status, diagnostics, export, viewer.

Extracted from ``cli.py`` to keep the main entry point manageable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cortex.cli_helpers import csv_list, print_error, print_json, resolve_config


# ---- dispatch ----------------------------------------------------------------


def cmd_graph_dispatch(args: argparse.Namespace) -> int:
    """Dispatch for ``cortex graph`` without a subcommand — show help."""
    if not getattr(args, "graph_cmd", None):
        print(
            "Usage: cortex graph {build|status|broken|orphans|centrality|"
            "stale|contradictions|export|viewer}",
            file=sys.stderr,
        )
        print("Run `cortex graph build` to generate graph artifacts.", file=sys.stderr)
        return 2
    return args.func(args)


# ---- build -------------------------------------------------------------------


def cmd_graph_build(args: argparse.Namespace) -> int:
    """Build graph artifacts from chunks.jsonl."""
    from cortex.graph_index import build_graph

    cfg = resolve_config(getattr(args, "config", None))
    print(f"Building graph from: {cfg.index.chunks_path}")

    try:
        report = build_graph(cfg, force=getattr(args, "force", False))
    except FileNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    print(report.summary())
    if report.nodes_by_type:
        print("\n  Nodes by type:")
        for ntype, count in sorted(report.nodes_by_type.items()):
            print(f"    {ntype:>10}: {count}")
    if report.edges_by_type:
        print("\n  Edges by type:")
        for etype, count in sorted(report.edges_by_type.items()):
            print(f"    {etype:>12}: {count}")
    if report.broken_by_kind:
        print("\n  Broken references:")
        for kind, count in sorted(report.broken_by_kind.items()):
            print(f"    {kind:>12}: {count}")
    return 0


# ---- status ------------------------------------------------------------------


def cmd_graph_status(args: argparse.Namespace) -> int:
    """Show comprehensive graph status report."""
    from cortex.graph_diagnostics import ArtifactNotFoundError, build_status_report

    cfg = resolve_config(getattr(args, "config", None))
    try:
        report = build_status_report(cfg, stale_days=args.stale_days)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")
    print(report.summary())
    return 0


# ---- broken ------------------------------------------------------------------


def cmd_graph_broken(args: argparse.Namespace) -> int:
    """List broken (unresolved / ambiguous) references."""
    from cortex.graph_diagnostics import ArtifactNotFoundError, GraphArtifacts

    cfg = resolve_config(getattr(args, "config", None))
    try:
        artifacts = GraphArtifacts.load(cfg)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    if not artifacts.broken:
        print("No broken references found.")
        return 0

    if args.json:
        print_json(artifacts.broken)
        return 0

    for b in artifacts.broken:
        kind = b.get("kind", "unknown")
        src = b.get("source_node", "")
        raw = b.get("target_raw", "")
        cands = b.get("candidates", [])
        if kind == "ambiguous":
            print(f"  AMBIGUOUS  {src}  \u2192  [[{raw}]]  candidates: {cands}")
        else:
            print(f"  UNRESOLVED {src}  \u2192  [[{raw}]]")

    print(f"\nTotal: {len(artifacts.broken)} broken reference(s)")
    return 0


# ---- orphans -----------------------------------------------------------------


def cmd_graph_orphans(args: argparse.Namespace) -> int:
    """List orphan nodes (zero edges in or out)."""
    from cortex.graph_diagnostics import ArtifactNotFoundError, GraphArtifacts, find_orphans

    cfg = resolve_config(getattr(args, "config", None))
    try:
        artifacts = GraphArtifacts.load(cfg)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    orphans = find_orphans(artifacts)
    if not orphans:
        print("No orphan nodes found.")
        return 0

    if args.json:
        print_json(orphans)
        return 0

    for o in orphans:
        print(f"  {o.get('type', '?'):>8}  {o['id']:50s}  {o.get('label', '')}")
    print(f"\nTotal: {len(orphans)} orphan node(s)")
    return 0


# ---- centrality ---------------------------------------------------------------


def cmd_graph_centrality(args: argparse.Namespace) -> int:
    """Show centrality rankings."""
    from cortex.graph_diagnostics import (
        ArtifactNotFoundError,
        GraphArtifacts,
        compute_centrality,
    )

    cfg = resolve_config(getattr(args, "config", None))
    try:
        artifacts = GraphArtifacts.load(cfg)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    centrality = compute_centrality(artifacts, include_pagerank=args.pagerank)

    if args.node_type:
        centrality = [e for e in centrality if e.node_type == args.node_type]

    limited = centrality[: args.limit]

    if args.json:
        payload = [
            {
                "node_id": e.node_id,
                "label": e.label,
                "node_type": e.node_type,
                "degree": e.degree,
                "in_degree": e.in_degree,
                "out_degree": e.out_degree,
                **(dict(pagerank=round(e.pagerank, 6)) if args.pagerank else {}),
            }
            for e in limited
        ]
        print_json(payload)
        return 0

    header = (
        f"  {'Rank':>4}  {'Label':30s}  {'Type':>8}  {'Deg':>4}  "
        f"{'In':>4}  {'Out':>4}"
    )
    if args.pagerank:
        header += f"  {'PageRank':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for i, e in enumerate(limited, 1):
        line = (
            f"  {i:>4}  {e.label[:30]:30s}  {e.node_type:>8}  "
            f"{e.degree:>4}  {e.in_degree:>4}  {e.out_degree:>4}"
        )
        if args.pagerank:
            line += f"  {e.pagerank:>10.6f}"
        print(line)

    print(f"\nShowing top {len(limited)} of {len(centrality)} nodes")
    return 0


# ---- stale -------------------------------------------------------------------


def cmd_graph_stale(args: argparse.Namespace) -> int:
    """List stale note candidates."""
    from cortex.graph_diagnostics import ArtifactNotFoundError, GraphArtifacts, find_stale

    cfg = resolve_config(getattr(args, "config", None))
    try:
        artifacts = GraphArtifacts.load(cfg)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    stale = find_stale(artifacts, stale_days=args.stale_days)
    if not stale:
        print(f"No stale notes found (threshold: {args.stale_days} days).")
        return 0

    if args.json:
        print_json(stale)
        return 0

    for s in stale:
        days = s.get("days_since_verified")
        days_str = f"{days}d ago" if days is not None else "no date"
        imp = s.get("importance", "?")
        print(
            f"  imp={imp:<4}  {days_str:>10s}  "
            f"{s.get('file', '') or s['node_id']}"
        )
    print(f"\nTotal: {len(stale)} stale candidate(s) (threshold: {args.stale_days} days)")
    return 0


# ---- contradictions ----------------------------------------------------------


def cmd_graph_contradictions(args: argparse.Namespace) -> int:
    """List contradiction edges."""
    from cortex.graph_diagnostics import (
        ArtifactNotFoundError,
        GraphArtifacts,
        find_contradictions,
    )

    cfg = resolve_config(getattr(args, "config", None))
    try:
        artifacts = GraphArtifacts.load(cfg)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    contradictions = find_contradictions(artifacts)
    if not contradictions:
        print("No contradiction edges found.")
        return 0

    if args.json:
        print_json(contradictions)
        return 0

    for c in contradictions:
        print(f"  {c['source_label']}  \u21d4  {c['target_label']}")
        print(f"    {c['source']}  \u2192  {c['target']}")
    print(f"\nTotal: {len(contradictions)} contradiction(s)")
    return 0


# ---- export ------------------------------------------------------------------


def cmd_graph_export(args: argparse.Namespace) -> int:
    """Export graph in JSON, D3 JSON, or Mermaid format."""
    from cortex.graph_diagnostics import ArtifactNotFoundError, GraphArtifacts
    from cortex.graph_export import export_d3_json, export_json, export_mermaid

    cfg = resolve_config(getattr(args, "config", None))
    try:
        artifacts = GraphArtifacts.load(cfg)
    except ArtifactNotFoundError as e:
        return print_error(f"\n  \u2717  {e}")

    node_types = csv_list(getattr(args, "node_type", None))
    edge_types = csv_list(getattr(args, "edge_type", None))
    neighborhood = getattr(args, "neighborhood", None)

    fmt = args.format
    if fmt == "json":
        output = export_json(artifacts, node_types=node_types, edge_types=edge_types, neighborhood=neighborhood)
    elif fmt == "d3-json":
        output = export_d3_json(
            artifacts,
            node_types=node_types,
            edge_types=edge_types,
            neighborhood=neighborhood,
            include_pagerank=getattr(args, "pagerank", False),
            include_diagnostics=getattr(args, "diagnostics", False),
        )
    elif fmt == "mermaid":
        output = export_mermaid(
            artifacts,
            node_types=node_types,
            edge_types=edge_types,
            neighborhood=neighborhood,
            direction=getattr(args, "direction", "LR"),
        )
    else:
        return print_error(f"Unknown format: {fmt}. Use 'json', 'd3-json', or 'mermaid'.", exit_code=2)

    out_path = getattr(args, "output", None)
    if out_path:
        Path(out_path).write_text(output, encoding="utf-8")
        print(f"Exported to {out_path}", file=sys.stderr)
    else:
        print(output, end="")

    return 0


# ---- viewer ------------------------------------------------------------------


def cmd_graph_viewer(args: argparse.Namespace) -> int:
    """Generate a static D3 HTML graph viewer."""
    from cortex.graph_diagnostics import ArtifactNotFoundError, GraphArtifacts
    from cortex.graph_export import export_d3_json
    from cortex.graph_viewer import generate_graph_viewer_html

    cfg = resolve_config(getattr(args, "config", None))

    embedded_json = None
    if getattr(args, "embed_data", False):
        try:
            artifacts = GraphArtifacts.load(cfg)
        except ArtifactNotFoundError as e:
            return print_error(f"\n  \u2717  {e}")
        embedded_json = export_d3_json(
            artifacts,
            include_diagnostics=getattr(args, "diagnostics", False),
        )
        html = generate_graph_viewer_html(embedded_json=embedded_json)
    else:
        html = generate_graph_viewer_html(data_path=getattr(args, "data", None) or "graph_data.json")

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Viewer written to {out_path}", file=sys.stderr)
    return 0


# ---- subparser builder -------------------------------------------------------


def add_graph_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``cortex graph`` and all its subcommands on *subparsers*."""
    gr = subparsers.add_parser("graph", help="Build and inspect the knowledge graph")
    gr.add_argument("--config", type=str, help="Path to config.yaml")
    gr_sub = gr.add_subparsers(dest="graph_cmd")

    _b = gr_sub.add_parser("build", help="Build graph artifacts from chunks.jsonl")
    _b.add_argument("--force", action="store_true", help="Force rebuild (reserved for future incremental)")
    _b.set_defaults(func=cmd_graph_build)

    _st = gr_sub.add_parser("status", help="Show comprehensive graph status report")
    _st.add_argument("--stale-days", type=int, default=180, help="Days threshold for stale detection (default: 180)")
    _st.set_defaults(func=cmd_graph_status)

    _br = gr_sub.add_parser("broken", help="List broken (unresolved / ambiguous) references")
    _br.add_argument("--json", action="store_true", help="Output JSON")
    _br.set_defaults(func=cmd_graph_broken)

    _o = gr_sub.add_parser("orphans", help="List orphan nodes (zero edges)")
    _o.add_argument("--json", action="store_true", help="Output JSON")
    _o.set_defaults(func=cmd_graph_orphans)

    _c = gr_sub.add_parser("centrality", help="Show centrality rankings")
    _c.add_argument("--limit", type=int, default=20, help="Number of results (default: 20)")
    _c.add_argument("--node-type", type=str, default=None, help="Filter by node type (note, chunk, tag, alias)")
    _c.add_argument("--pagerank", action="store_true", help="Include PageRank computation")
    _c.add_argument("--json", action="store_true", help="Output JSON")
    _c.set_defaults(func=cmd_graph_centrality)

    _sa = gr_sub.add_parser("stale", help="List stale note candidates")
    _sa.add_argument("--stale-days", type=int, default=180, help="Days threshold (default: 180)")
    _sa.add_argument("--json", action="store_true", help="Output JSON")
    _sa.set_defaults(func=cmd_graph_stale)

    _co = gr_sub.add_parser("contradictions", help="List contradiction edges")
    _co.add_argument("--json", action="store_true", help="Output JSON")
    _co.set_defaults(func=cmd_graph_contradictions)

    _ex = gr_sub.add_parser("export", help="Export graph as JSON, D3 JSON, or Mermaid")
    _ex.add_argument("--format", type=str, required=True, choices=["json", "d3-json", "mermaid"], help="Export format")
    _ex.add_argument("--node-type", type=str, default=None, help="Filter by node type (CSV, e.g. 'note,tag')")
    _ex.add_argument("--edge-type", type=str, default=None, help="Filter by edge type (CSV, e.g. 'links_to,contains')")
    _ex.add_argument("--neighborhood", type=str, default=None, help="Export 1-hop neighborhood of a node ID")
    _ex.add_argument("--direction", type=str, default="LR", help="Mermaid graph direction: LR, TD, RL, BT (default: LR)")
    _ex.add_argument("--pagerank", action="store_true", help="Include PageRank in d3-json node metrics")
    _ex.add_argument("--diagnostics", action="store_true", help="Include diagnostics overlay in d3-json output")
    _ex.add_argument("--output", "-o", type=str, default=None, help="Write to file instead of stdout")
    _ex.set_defaults(func=cmd_graph_export)

    _v = gr_sub.add_parser("viewer", help="Generate a standalone static D3 graph viewer")
    _v.add_argument("--output", "-o", type=str, required=True, help="Write HTML viewer to this file")
    _v.add_argument("--data", type=str, default="graph_data.json", help="Relative D3 JSON path loaded by the viewer (default: graph_data.json)")
    _v.add_argument("--embed-data", action="store_true", help="Embed current D3 JSON in the HTML for a portable report")
    _v.add_argument("--diagnostics", action="store_true", help="Include diagnostics overlay when embedding graph data")
    _v.set_defaults(func=cmd_graph_viewer)

    gr.set_defaults(func=cmd_graph_dispatch)
    return gr
