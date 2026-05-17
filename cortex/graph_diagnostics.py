"""Graph diagnostics for hermes-cortex — Phase 5.5 / Slice 2.

Reads the stable graph artifacts produced by ``graph_index.build_graph()``
and provides diagnostic queries:

  - **status**: summary report (node/edge counts, broken refs, top hubs)
  - **broken**: list broken (unresolved / ambiguous) references
  - **orphans**: nodes with zero edges
  - **centrality**: degree / in-degree / out-degree rankings
  - **stale**: note nodes whose ``last_verified`` is older than a threshold
  - **contradictions**: pairs of notes linked by ``contradicts`` edges
  - **hub-spam**: over-connected notes that risk dominating retrieval

All operations are read-only against the JSONL artifacts. No Chroma, no
embeddings, no retrieval logic. Heavy computation (PageRank) is optional and
gated behind a flag.

Design constraints:
  - No new dependencies (stdlib + existing cortex modules)
  - Centrality is for diagnostics first, NOT an unconditional retrieval boost
  - Deterministic output (sorted)
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from cortex.config import Config

log = logging.getLogger("cortex.graph_diagnostics")


# ---- Artifact loader -------------------------------------------------------


def _graph_artifact_dir(cfg: Config) -> Path:
    """Return the graph artifact directory (mirrors graph_index convention)."""
    return cfg.index.chunks_path.parent / "graph"


class ArtifactNotFoundError(Exception):
    """Raised when required graph artifacts don't exist."""


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ArtifactNotFoundError(
            f"Graph artifact not found: {path}\n"
            f"Run `cortex graph build` first."
        )
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ArtifactNotFoundError(
            f"Graph artifact not found: {path}\n"
            f"Run `cortex graph build` first."
        )
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@dataclass
class GraphArtifacts:
    """Loaded graph artifacts — the read-only data layer for diagnostics."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    broken: list[dict[str, Any]]
    stats: dict[str, Any]
    artifact_dir: Path

    @classmethod
    def load(cls, cfg: Config) -> "GraphArtifacts":
        """Load all four artifacts from disk."""
        d = _graph_artifact_dir(cfg)
        return cls(
            nodes=_load_jsonl(d / "graph_nodes.jsonl"),
            edges=_load_jsonl(d / "graph_edges.jsonl"),
            broken=_load_jsonl(d / "graph_broken.jsonl"),
            stats=_load_json(d / "graph_stats.json"),
            artifact_dir=d,
        )


# ---- Centrality computation ------------------------------------------------


@dataclass
class CentralityEntry:
    """Centrality metrics for a single node."""

    node_id: str
    label: str
    node_type: str
    degree: int = 0
    in_degree: int = 0
    out_degree: int = 0
    pagerank: float = 0.0


def compute_centrality(
    artifacts: GraphArtifacts,
    *,
    include_pagerank: bool = False,
    damping: float = 0.85,
    iterations: int = 40,
    tolerance: float = 1e-6,
) -> list[CentralityEntry]:
    """Compute degree centrality (and optional PageRank) for all nodes.

    Returns entries sorted by degree descending, then node_id ascending.
    """
    node_map: dict[str, CentralityEntry] = {}
    for n in artifacts.nodes:
        nid = n["id"]
        node_map[nid] = CentralityEntry(
            node_id=nid,
            label=n.get("label", ""),
            node_type=n.get("type", ""),
        )

    # Degree counts
    for e in artifacts.edges:
        src = e.get("source", "")
        tgt = e.get("target", "")
        if src in node_map:
            node_map[src].out_degree += 1
            node_map[src].degree += 1
        if tgt in node_map:
            node_map[tgt].in_degree += 1
            node_map[tgt].degree += 1

    # Optional PageRank (iterative power method, no deps)
    if include_pagerank and node_map:
        _compute_pagerank(node_map, artifacts.edges, damping, iterations, tolerance)

    # Sort: highest degree first, then by node_id for determinism
    entries = sorted(
        node_map.values(),
        key=lambda e: (-e.degree, e.node_id),
    )
    return entries


def _compute_pagerank(
    node_map: dict[str, CentralityEntry],
    edges: list[dict[str, Any]],
    damping: float,
    iterations: int,
    tolerance: float,
) -> None:
    """In-place PageRank computation on the node_map entries.

    Simple iterative power method — no external deps. Convergence is
    checked per-iteration; we stop early if the L1 norm of the delta
    vector drops below tolerance.
    """
    n = len(node_map)
    if n == 0:
        return

    ids = sorted(node_map.keys())
    id_to_idx = {nid: i for i, nid in enumerate(ids)}

    # Build adjacency: outgoing[i] = list of target indices
    outgoing: list[list[int]] = [[] for _ in range(n)]
    out_degree_count = [0] * n

    for e in edges:
        src = e.get("source", "")
        tgt = e.get("target", "")
        si = id_to_idx.get(src)
        ti = id_to_idx.get(tgt)
        if si is not None and ti is not None:
            outgoing[si].append(ti)
            out_degree_count[si] += 1

    # Initialize uniform
    pr = [1.0 / n] * n
    base = (1.0 - damping) / n

    for _ in range(iterations):
        new_pr = [base] * n

        # Dangling node mass (nodes with no outgoing edges)
        dangling_sum = sum(pr[i] for i in range(n) if out_degree_count[i] == 0)
        dangling_add = damping * dangling_sum / n

        for i in range(n):
            new_pr[i] += dangling_add

        for i in range(n):
            if out_degree_count[i] > 0:
                share = damping * pr[i] / out_degree_count[i]
                for j in outgoing[i]:
                    new_pr[j] += share

        # Check convergence
        delta = sum(abs(new_pr[i] - pr[i]) for i in range(n))
        pr = new_pr
        if delta < tolerance:
            break

    # Write back
    for i, nid in enumerate(ids):
        node_map[nid].pagerank = pr[i]


# ---- Orphan detection ------------------------------------------------------


def find_orphans(artifacts: GraphArtifacts) -> list[dict[str, Any]]:
    """Return node dicts that have zero incoming AND zero outgoing edges.

    Sorted by node ID for determinism.
    """
    connected: set[str] = set()
    for e in artifacts.edges:
        connected.add(e.get("source", ""))
        connected.add(e.get("target", ""))

    orphans = [
        n for n in artifacts.nodes
        if n["id"] not in connected
    ]
    return sorted(orphans, key=lambda n: n["id"])


# ---- Stale detection -------------------------------------------------------


def find_stale(
    artifacts: GraphArtifacts,
    *,
    stale_days: int = 180,
    reference_date: date | None = None,
) -> list[dict[str, Any]]:
    """Return note nodes whose last_verified is older than ``stale_days``.

    Notes without ``last_verified`` in metadata are included as stale
    candidates if they have ``modified_date`` older than the threshold.
    Chunk/tag/alias nodes are skipped.

    Returns dicts with keys: node_id, label, file, last_verified, modified_date,
    days_since_verified, importance.
    """
    ref = reference_date or date.today()
    results: list[dict[str, Any]] = []

    for n in artifacts.nodes:
        if n.get("type") != "note":
            continue

        meta = n.get("metadata", {})
        last_verified = meta.get("last_verified", "")
        modified_date = meta.get("modified_date", "")
        importance = meta.get("importance", 3.0)

        # Determine the "freshness" date
        check_date_str = last_verified or modified_date
        if not check_date_str:
            # No date at all — mark as stale candidate
            results.append({
                "node_id": n["id"],
                "label": n.get("label", ""),
                "file": n.get("file", ""),
                "last_verified": last_verified,
                "modified_date": modified_date,
                "days_since_verified": None,
                "importance": importance,
                "reason": "no_date",
            })
            continue

        try:
            check_date = date.fromisoformat(check_date_str[:10])
        except (ValueError, TypeError):
            results.append({
                "node_id": n["id"],
                "label": n.get("label", ""),
                "file": n.get("file", ""),
                "last_verified": last_verified,
                "modified_date": modified_date,
                "days_since_verified": None,
                "importance": importance,
                "reason": "unparseable_date",
            })
            continue

        age_days = (ref - check_date).days
        if age_days >= stale_days:
            results.append({
                "node_id": n["id"],
                "label": n.get("label", ""),
                "file": n.get("file", ""),
                "last_verified": last_verified,
                "modified_date": modified_date,
                "days_since_verified": age_days,
                "importance": importance,
                "reason": "age",
            })

    # Sort by importance descending (high-importance stale = more urgent),
    # then by days_since_verified descending, then node_id
    results.sort(key=lambda r: (
        -(r.get("importance") or 0),
        -(r.get("days_since_verified") or 999999),
        r["node_id"],
    ))
    return results


# ---- Contradiction detection -----------------------------------------------


def find_contradictions(artifacts: GraphArtifacts) -> list[dict[str, Any]]:
    """Return pairs of nodes linked by ``contradicts`` edges.

    Returns dicts with: source, target, source_label, target_label.
    Sorted by (source, target) for determinism.
    """
    # Build a quick node label lookup
    label_map: dict[str, str] = {
        n["id"]: n.get("label", "") for n in artifacts.nodes
    }

    results: list[dict[str, Any]] = []
    for e in artifacts.edges:
        if e.get("type") == "contradicts":
            results.append({
                "source": e["source"],
                "target": e["target"],
                "source_label": label_map.get(e["source"], ""),
                "target_label": label_map.get(e["target"], ""),
            })

    results.sort(key=lambda r: (r["source"], r["target"]))
    return results


# ---- Hub-spam detection ----------------------------------------------------


def find_hub_spam(
    artifacts: GraphArtifacts,
    *,
    threshold_percentile: float = 95.0,
    min_degree: int = 10,
) -> list[dict[str, Any]]:
    """Detect over-connected nodes (MOCs, index pages) that could dominate retrieval.

    A node is flagged as hub-spam if its degree is:
      1. Above the ``threshold_percentile`` of all node degrees, AND
      2. At least ``min_degree`` edges

    Returns dicts with: node_id, label, node_type, file, degree, in_degree, out_degree.
    Sorted by degree descending.
    """
    centrality = compute_centrality(artifacts)
    if not centrality:
        return []

    degrees = [e.degree for e in centrality]
    threshold = _percentile(degrees, threshold_percentile)
    effective_threshold = max(threshold, min_degree)

    results: list[dict[str, Any]] = []
    for entry in centrality:
        if entry.degree >= effective_threshold:
            results.append({
                "node_id": entry.node_id,
                "label": entry.label,
                "node_type": entry.node_type,
                "degree": entry.degree,
                "in_degree": entry.in_degree,
                "out_degree": entry.out_degree,
            })

    # Already sorted by centrality (degree desc)
    return results


def _percentile(values: list[int], pct: float) -> float:
    """Compute the given percentile of a sorted list. No numpy needed."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (pct / 100.0) * (len(s) - 1)
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return float(s[lo])
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


# ---- Status report ---------------------------------------------------------


@dataclass
class GraphStatusReport:
    """Comprehensive status report of the knowledge graph."""

    node_count: int = 0
    edge_count: int = 0
    broken_count: int = 0
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_type: dict[str, int] = field(default_factory=dict)
    unresolved_count: int = 0
    ambiguous_count: int = 0
    orphan_count: int = 0
    stale_count: int = 0
    contradiction_count: int = 0
    top_hubs: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Graph status: {self.node_count} nodes, {self.edge_count} edges",
            "",
        ]

        if self.nodes_by_type:
            lines.append("  Nodes by type:")
            for t, c in sorted(self.nodes_by_type.items()):
                lines.append(f"    {t:>10}: {c}")

        if self.edges_by_type:
            lines.append("  Edges by type:")
            for t, c in sorted(self.edges_by_type.items()):
                lines.append(f"    {t:>14}: {c}")

        lines.append("")
        lines.append(f"  Unresolved references: {self.unresolved_count}")
        lines.append(f"  Ambiguous references:  {self.ambiguous_count}")
        lines.append(f"  Orphan nodes:          {self.orphan_count}")
        lines.append(f"  Stale candidates:      {self.stale_count}")
        lines.append(f"  Contradiction edges:   {self.contradiction_count}")

        if self.top_hubs:
            lines.append("")
            lines.append("  Top hubs:")
            for h in self.top_hubs[:10]:
                lines.append(
                    f"    {h['label']:30s}  degree={h['degree']:3d}  "
                    f"({h['node_type']})"
                )

        return "\n".join(lines)


def build_status_report(
    cfg: Config,
    *,
    stale_days: int = 180,
    hub_limit: int = 10,
) -> GraphStatusReport:
    """Build a comprehensive status report from graph artifacts.

    This is the main entry point for ``cortex graph status``.
    """
    artifacts = GraphArtifacts.load(cfg)

    # Basic counts from stats.json
    stats = artifacts.stats

    # Broken ref breakdown
    broken_kinds: Counter[str] = Counter()
    for b in artifacts.broken:
        broken_kinds[b.get("kind", "unknown")] += 1

    # Orphans
    orphans = find_orphans(artifacts)

    # Stale
    stale = find_stale(artifacts, stale_days=stale_days)

    # Contradictions
    contradictions = find_contradictions(artifacts)

    # Top hubs (by degree)
    centrality = compute_centrality(artifacts)
    # Filter to note nodes for the "top hubs" display
    note_hubs = [
        {
            "node_id": e.node_id,
            "label": e.label,
            "node_type": e.node_type,
            "degree": e.degree,
            "in_degree": e.in_degree,
            "out_degree": e.out_degree,
        }
        for e in centrality
        if e.node_type == "note" and e.degree > 0
    ][:hub_limit]

    return GraphStatusReport(
        node_count=stats.get("node_count", len(artifacts.nodes)),
        edge_count=stats.get("edge_count", len(artifacts.edges)),
        broken_count=stats.get("broken_count", len(artifacts.broken)),
        nodes_by_type=stats.get("nodes_by_type", {}),
        edges_by_type=stats.get("edges_by_type", {}),
        unresolved_count=broken_kinds.get("unresolved", 0),
        ambiguous_count=broken_kinds.get("ambiguous", 0),
        orphan_count=len(orphans),
        stale_count=len(stale),
        contradiction_count=len(contradictions),
        top_hubs=note_hubs,
    )
