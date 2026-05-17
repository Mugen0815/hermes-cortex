"""Graph export for hermes-cortex — Phase 5.5 / Slice 3.

Exports the knowledge graph in JSON or Mermaid format from the stable
JSONL artifacts produced by ``graph_index.build_graph()``.

Export modes:
  - **JSON**: full graph or filtered subgraph as a single JSON document
    with ``nodes`` and ``edges`` arrays. Round-trip safe.
  - **Mermaid**: flowchart with ``subgraph`` grouping by node type.
    Suitable for pasting into Markdown docs or rendering via Mermaid CLI.

Filtering:
  - ``--node-type``: only include nodes of specified type(s)
  - ``--edge-type``: only include edges of specified type(s)
  - ``--neighborhood NODE_ID``: include a node and its direct neighbors

Design constraints:
  - Read-only against artifacts (no Chroma, no embeddings)
  - No new dependencies
  - Deterministic output (sorted)
"""

from __future__ import annotations

import json
import re
from hashlib import sha1
from typing import Any

from cortex.graph_diagnostics import GraphArtifacts, compute_centrality, find_orphans


# ---- Filtering -------------------------------------------------------------


def filter_subgraph(
    artifacts: GraphArtifacts,
    *,
    node_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    neighborhood: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter artifacts to a subgraph.

    Returns (filtered_nodes, filtered_edges). Filtering rules:
      1. If ``neighborhood`` is set, start with that node + its direct
         neighbors (1-hop in either direction), then apply type filters.
      2. If ``node_types`` is set, keep only nodes of those types.
      3. If ``edge_types`` is set, keep only edges of those types.
      4. After all filters, prune edges whose source or target was removed.

    All filters are AND-combined.
    """
    nodes = list(artifacts.nodes)
    edges = list(artifacts.edges)

    # Step 1: neighborhood filter
    if neighborhood:
        neighbor_ids: set[str] = {neighborhood}
        for e in edges:
            if e.get("source") == neighborhood:
                neighbor_ids.add(e.get("target", ""))
            if e.get("target") == neighborhood:
                neighbor_ids.add(e.get("source", ""))
        nodes = [n for n in nodes if n["id"] in neighbor_ids]
        edges = [
            e for e in edges
            if e.get("source") in neighbor_ids and e.get("target") in neighbor_ids
        ]

    # Step 2: node type filter
    if node_types:
        type_set = set(node_types)
        nodes = [n for n in nodes if n.get("type") in type_set]

    # Step 3: edge type filter
    if edge_types:
        etype_set = set(edge_types)
        edges = [e for e in edges if e.get("type") in etype_set]

    # Step 4: prune edges referencing removed nodes
    node_ids = {n["id"] for n in nodes}
    edges = [
        e for e in edges
        if e.get("source") in node_ids and e.get("target") in node_ids
    ]

    # Sort for determinism
    nodes.sort(key=lambda n: n["id"])
    edges.sort(key=lambda e: (e.get("source", ""), e.get("target", ""), e.get("type", "")))

    return nodes, edges


# ---- JSON export -----------------------------------------------------------


def export_json(
    artifacts: GraphArtifacts,
    *,
    node_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    neighborhood: str | None = None,
    indent: int = 2,
) -> str:
    """Export graph as a JSON document.

    Returns a JSON string with ``{"nodes": [...], "edges": [...], "stats": {...}}``.
    Round-trip safe: the output can be parsed back into the same structure.
    """
    nodes, edges = filter_subgraph(
        artifacts,
        node_types=node_types,
        edge_types=edge_types,
        neighborhood=neighborhood,
    )

    doc = {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "filtered": bool(node_types or edge_types or neighborhood),
        },
    }
    return json.dumps(doc, ensure_ascii=False, indent=indent, sort_keys=False)


def export_d3_json(
    artifacts: GraphArtifacts,
    *,
    node_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    neighborhood: str | None = None,
    include_pagerank: bool = False,
    include_diagnostics: bool = False,
    indent: int = 2,
) -> str:
    """Export graph as minimal D3-compatible JSON for static viewers.

    This projection preserves the existing filter semantics but emits only
    fields the browser viewer needs. Degree metrics are taken from
    ``graph_diagnostics.compute_centrality`` so export, diagnostics, and
    future lifecycle review share one centrality implementation.

    PageRank is opt-in because it is iterative; when disabled the field is
    omitted entirely instead of filled with misleading zeroes. Diagnostics are
    opt-in and are projected as render metadata / diagnostic nodes, using
    stable IDs derived from source, target, kind, and candidates.
    """
    nodes, edges = filter_subgraph(
        artifacts,
        node_types=node_types,
        edge_types=edge_types,
        neighborhood=neighborhood,
    )
    filtered_artifacts = GraphArtifacts(
        nodes=nodes,
        edges=edges,
        broken=artifacts.broken,
        stats=artifacts.stats,
        artifact_dir=artifacts.artifact_dir,
    )
    centrality_by_id = {
        entry.node_id: entry
        for entry in compute_centrality(
            filtered_artifacts,
            include_pagerank=include_pagerank,
        )
    }

    d3_nodes: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        c = centrality_by_id.get(node_id)
        meta = node.get("metadata", {}) or {}
        d3_node = {
            "id": node_id,
            "label": node.get("label", node_id),
            "type": node.get("type", "unknown"),
            "file": node.get("file", ""),
            "degree": c.degree if c else 0,
            "in_degree": c.in_degree if c else 0,
            "out_degree": c.out_degree if c else 0,
            "fm_status": meta.get("status", ""),
            "fm_type": meta.get("note_type", ""),
        }
        if include_pagerank:
            d3_node["pagerank"] = c.pagerank if c else 0.0
        d3_nodes.append(d3_node)

    d3_edges = [
        {
            "source": edge.get("source", ""),
            "target": edge.get("target", ""),
            "type": edge.get("type", ""),
        }
        for edge in edges
    ]

    diagnostics = {"unresolved": 0, "ambiguous": 0, "orphans": 0}
    if include_diagnostics:
        diagnostics = _apply_d3_diagnostics(
            artifacts=filtered_artifacts,
            d3_nodes=d3_nodes,
            d3_edges=d3_edges,
        )

    doc = {
        "nodes": d3_nodes,
        "edges": d3_edges,
        "stats": {
            "node_count": len(d3_nodes),
            "edge_count": len(d3_edges),
            "filtered": bool(node_types or edge_types or neighborhood),
        },
    }
    if include_diagnostics:
        doc["stats"]["diagnostics"] = diagnostics
    return json.dumps(doc, ensure_ascii=False, indent=indent, sort_keys=False)


def _diagnostic_id(kind: str, source_node: str, target_raw: str, candidates: list[str] | None = None) -> str:
    """Build a deterministic, compact diagnostic node ID."""
    payload = json.dumps(
        {
            "kind": kind,
            "source_node": source_node,
            "target_raw": target_raw,
            "candidates": sorted(candidates or []),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"diagnostic:{kind}:{sha1(payload.encode('utf-8')).hexdigest()[:12]}"


def _apply_d3_diagnostics(
    *,
    artifacts: GraphArtifacts,
    d3_nodes: list[dict[str, Any]],
    d3_edges: list[dict[str, Any]],
) -> dict[str, int]:
    """Mutate D3 projections with visual diagnostics metadata.

    Runs on the already-filtered projection: hidden sources do not leak
    diagnostics into filtered exports. Orphans are marked on existing nodes.
    """
    visible_ids = {node["id"] for node in d3_nodes}
    counts = {"unresolved": 0, "ambiguous": 0, "orphans": 0}

    node_by_id = {node["id"]: node for node in d3_nodes}
    for orphan in find_orphans(artifacts):
        orphan_id = orphan.get("id", "")
        if orphan_id not in node_by_id:
            continue
        node_by_id[orphan_id]["diagnostics"] = {"orphan": True}
        node_by_id[orphan_id]["visual"] = {"status": "orphan", "color": "muted"}
        counts["orphans"] += 1

    for broken in sorted(
        artifacts.broken,
        key=lambda b: (b.get("source_node", ""), b.get("kind", ""), b.get("target_raw", "")),
    ):
        source = str(broken.get("source_node", ""))
        if source not in visible_ids:
            continue

        kind = str(broken.get("kind", ""))
        target_raw = str(broken.get("target_raw", ""))
        candidates = sorted(str(c) for c in broken.get("candidates", []) if str(c) in visible_ids)

        if kind == "unresolved":
            diag_id = _diagnostic_id("unresolved", source, target_raw)
            d3_nodes.append({
                "id": diag_id,
                "label": target_raw,
                "type": "diagnostic",
                "file": "",
                "degree": 1,
                "in_degree": 1,
                "out_degree": 0,
                "diagnostic_kind": "unresolved",
                "source_node": source,
                "target_raw": target_raw,
                "context": broken.get("context", ""),
                "visual": {"status": "ghost", "color": "red"},
            })
            d3_edges.append({"source": source, "target": diag_id, "type": "diagnostic_unresolved"})
            counts["unresolved"] += 1
        elif kind == "ambiguous":
            diag_id = _diagnostic_id("ambiguous", source, target_raw, candidates)
            d3_nodes.append({
                "id": diag_id,
                "label": target_raw,
                "type": "diagnostic",
                "file": "",
                "degree": 1 + len(candidates),
                "in_degree": 1,
                "out_degree": len(candidates),
                "diagnostic_kind": "ambiguous",
                "source_node": source,
                "target_raw": target_raw,
                "candidates": candidates,
                "context": broken.get("context", ""),
                "visual": {"status": "diagnostic", "color": "orange"},
            })
            d3_edges.append({"source": source, "target": diag_id, "type": "diagnostic_ambiguous"})
            for candidate in candidates:
                d3_edges.append({"source": diag_id, "target": candidate, "type": "diagnostic_candidate"})
            counts["ambiguous"] += 1

    d3_nodes.sort(key=lambda n: n["id"])
    d3_edges.sort(key=lambda e: (e.get("source", ""), e.get("target", ""), e.get("type", "")))
    return counts


# ---- Mermaid export --------------------------------------------------------

# Characters that break Mermaid syntax in node labels.
_MERMAID_UNSAFE = re.compile(r'["\[\]{}()<>|#&;`]')


def _mermaid_id(node_id: str) -> str:
    """Convert a node ID to a Mermaid-safe identifier.

    Mermaid node IDs must be alphanumeric + underscores. We replace
    everything else with underscores and collapse runs.
    """
    safe = re.sub(r"[^a-zA-Z0-9]", "_", node_id)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe or "node"


def _mermaid_label(text: str) -> str:
    """Escape a label for Mermaid. Strip unsafe chars, truncate long labels."""
    escaped = _MERMAID_UNSAFE.sub("", text)
    if len(escaped) > 40:
        escaped = escaped[:37] + "..."
    return escaped or "?"


# Node type → Mermaid shape. Notes get rounded boxes, tags get hexagons, etc.
_MERMAID_SHAPES: dict[str, tuple[str, str]] = {
    "note":    ("([", "])"),    # stadium-shaped / rounded
    "chunk":   ("[", "]"),     # rectangle
    "tag":     ("{{", "}}"),   # hexagon
    "alias":   ("(", ")"),     # rounded
    "memory":  ("([", "])"),
    "skill":   ("([", "])"),
    "session": ("[", "]"),
    "topic":   ("{{", "}}"),
}

# Edge type → Mermaid arrow style
_MERMAID_ARROWS: dict[str, str] = {
    "contains":         "-->",
    "links_to":         "-->",
    "mentions":         "-.->",
    "tagged_with":      "-->",
    "aliases":          "-.-",
    "derived_from":     "==>",
    "supports":         "-->",
    "contradicts":      "x--x",
    "supersedes":       "==>",
    "stale_relative_to": "-.->",
}


def export_mermaid(
    artifacts: GraphArtifacts,
    *,
    node_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    neighborhood: str | None = None,
    direction: str = "LR",
) -> str:
    """Export graph as a Mermaid flowchart with subgraph grouping by node type.

    Args:
        direction: Mermaid graph direction — "LR" (left-right), "TD" (top-down),
                   "RL", "BT". Default "LR".

    Returns a Mermaid source string ready for rendering.
    """
    nodes, edges = filter_subgraph(
        artifacts,
        node_types=node_types,
        edge_types=edge_types,
        neighborhood=neighborhood,
    )

    lines: list[str] = [f"graph {direction}"]

    # Group nodes by type for subgraphs
    by_type: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        ntype = n.get("type", "unknown")
        by_type.setdefault(ntype, []).append(n)

    for ntype in sorted(by_type):
        type_nodes = by_type[ntype]
        lines.append(f"    subgraph {ntype}s")
        for n in type_nodes:
            mid = _mermaid_id(n["id"])
            label = _mermaid_label(n.get("label", n["id"]))
            lo, rc = _MERMAID_SHAPES.get(ntype, ("[", "]"))
            lines.append(f"        {mid}{lo}\"{label}\"{rc}")
        lines.append("    end")

    # Edges
    for e in edges:
        src = _mermaid_id(e.get("source", ""))
        tgt = _mermaid_id(e.get("target", ""))
        etype = e.get("type", "links_to")
        arrow = _MERMAID_ARROWS.get(etype, "-->")
        edge_label = etype.replace("_", " ")
        lines.append(f"    {src} {arrow}|{edge_label}| {tgt}")

    return "\n".join(lines) + "\n"
