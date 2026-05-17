"""Unit tests for cortex.graph_diagnostics — Phase 5.5 / Slice 2.

Tests cover:
  - Artifact loader (load/missing)
  - Centrality computation (degree, in/out-degree, PageRank)
  - Orphan detection
  - Stale detection (age-based, no-date, high-importance priority)
  - Contradiction detection
  - Hub-spam detection
  - Status report
  - CLI subcommands (status, broken, orphans, centrality, stale, contradictions)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from cortex.graph_diagnostics import (
    ArtifactNotFoundError,
    GraphArtifacts,
    build_status_report,
    compute_centrality,
    find_contradictions,
    find_hub_spam,
    find_orphans,
    find_stale,
)


# ---- Helpers ---------------------------------------------------------------


def _make_config(tmp_path: Path):
    from cortex.config import (
        Config,
        ContextBuilderConfig,
        EmbeddingsConfig,
        HermesMemoryConfig,
        IndexConfig,
        SearchConfig,
        VaultConfig,
    )
    return Config(
        vault=VaultConfig(path=tmp_path / "vault"),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(),
        search=SearchConfig(),
        context_builder=ContextBuilderConfig(),
    )


def _write_artifacts(
    tmp_path: Path,
    nodes: list[dict],
    edges: list[dict],
    broken: list[dict] | None = None,
    stats: dict | None = None,
) -> Path:
    """Write graph artifacts to disk and return the artifact dir."""
    d = tmp_path / "graph"
    d.mkdir(parents=True, exist_ok=True)

    with (d / "graph_nodes.jsonl").open("w") as f:
        for n in nodes:
            f.write(json.dumps(n) + "\n")
    with (d / "graph_edges.jsonl").open("w") as f:
        for e in edges:
            f.write(json.dumps(e) + "\n")
    with (d / "graph_broken.jsonl").open("w") as f:
        for b in (broken or []):
            f.write(json.dumps(b) + "\n")

    if stats is None:
        stats = {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "broken_count": len(broken or []),
            "nodes_by_type": {},
            "edges_by_type": {},
            "broken_by_kind": {},
        }
    with (d / "graph_stats.json").open("w") as f:
        json.dump(stats, f)

    return d


def _simple_artifacts(tmp_path: Path) -> GraphArtifacts:
    """Build a small test graph and return loaded artifacts."""
    nodes = [
        {"id": "note:A.md", "type": "note", "label": "A", "file": "A.md",
         "metadata": {"last_verified": "2024-01-15", "importance": 5.0, "modified_date": "2024-06-01"}},
        {"id": "note:B.md", "type": "note", "label": "B", "file": "B.md",
         "metadata": {"last_verified": "2025-04-01", "importance": 2.0, "modified_date": "2025-04-01"}},
        {"id": "note:C.md", "type": "note", "label": "C", "file": "C.md",
         "metadata": {"importance": 3.0, "modified_date": "2024-03-01"}},
        {"id": "note:Orphan.md", "type": "note", "label": "Orphan", "file": "Orphan.md",
         "metadata": {}},
        {"id": "chunk:a.md#intro", "type": "chunk", "label": "(intro)", "file": "A.md"},
        {"id": "chunk:b.md#intro", "type": "chunk", "label": "(intro)", "file": "B.md"},
        {"id": "chunk:c.md#intro", "type": "chunk", "label": "(intro)", "file": "C.md"},
        {"id": "tag:python", "type": "tag", "label": "python"},
    ]
    edges = [
        {"source": "note:A.md", "target": "chunk:a.md#intro", "type": "contains"},
        {"source": "note:B.md", "target": "chunk:b.md#intro", "type": "contains"},
        {"source": "note:C.md", "target": "chunk:c.md#intro", "type": "contains"},
        {"source": "note:A.md", "target": "note:B.md", "type": "links_to"},
        {"source": "note:B.md", "target": "note:C.md", "type": "links_to"},
        {"source": "note:A.md", "target": "tag:python", "type": "tagged_with"},
        {"source": "note:A.md", "target": "note:C.md", "type": "contradicts"},
    ]
    broken = [
        {"source_node": "note:C.md", "target_raw": "Ghost", "kind": "unresolved", "context": "wikilink"},
    ]
    _write_artifacts(tmp_path, nodes, edges, broken)
    cfg = _make_config(tmp_path)
    return GraphArtifacts.load(cfg)


# ---- Artifact loader -------------------------------------------------------


class TestArtifactLoader:
    def test_load_success(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        assert len(artifacts.nodes) == 8
        assert len(artifacts.edges) == 7
        assert len(artifacts.broken) == 1

    def test_load_missing_raises(self, tmp_path):
        cfg = _make_config(tmp_path)
        with pytest.raises(ArtifactNotFoundError, match="graph_nodes.jsonl"):
            GraphArtifacts.load(cfg)

    def test_load_partial_missing_raises(self, tmp_path):
        # Only write nodes
        d = tmp_path / "graph"
        d.mkdir(parents=True)
        (d / "graph_nodes.jsonl").write_text("")
        cfg = _make_config(tmp_path)
        with pytest.raises(ArtifactNotFoundError, match="graph_edges.jsonl"):
            GraphArtifacts.load(cfg)


# ---- Centrality ------------------------------------------------------------


class TestCentrality:
    def test_degree_counts(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        centrality = compute_centrality(artifacts)
        by_id = {e.node_id: e for e in centrality}

        # note:A.md has edges: contains(out), links_to B(out), tagged_with(out), contradicts C(out) = 4 out
        # Plus chunk:a.md#intro has contains(in) = A participates in 4 outgoing
        a = by_id["note:A.md"]
        assert a.out_degree == 4  # contains, links_to, tagged_with, contradicts
        assert a.in_degree == 0
        assert a.degree == 4

    def test_sorted_by_degree_desc(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        centrality = compute_centrality(artifacts)
        degrees = [e.degree for e in centrality]
        assert degrees == sorted(degrees, reverse=True)

    def test_deterministic_tiebreak(self, tmp_path):
        """Nodes with same degree are sorted by node_id ascending."""
        artifacts = _simple_artifacts(tmp_path)
        c1 = compute_centrality(artifacts)
        c2 = compute_centrality(artifacts)
        assert [e.node_id for e in c1] == [e.node_id for e in c2]

    def test_pagerank_sums_to_one(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        centrality = compute_centrality(artifacts, include_pagerank=True)
        total_pr = sum(e.pagerank for e in centrality)
        assert abs(total_pr - 1.0) < 0.01  # should sum to ~1.0

    def test_pagerank_positive(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        centrality = compute_centrality(artifacts, include_pagerank=True)
        for e in centrality:
            assert e.pagerank > 0.0

    def test_empty_graph(self, tmp_path):
        _write_artifacts(tmp_path, [], [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        centrality = compute_centrality(artifacts)
        assert centrality == []

    def test_pagerank_empty(self, tmp_path):
        _write_artifacts(tmp_path, [], [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        centrality = compute_centrality(artifacts, include_pagerank=True)
        assert centrality == []


# ---- Orphans ---------------------------------------------------------------


class TestOrphans:
    def test_finds_orphan(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        orphans = find_orphans(artifacts)
        orphan_ids = {o["id"] for o in orphans}
        assert "note:Orphan.md" in orphan_ids

    def test_connected_nodes_not_orphans(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        orphans = find_orphans(artifacts)
        orphan_ids = {o["id"] for o in orphans}
        assert "note:A.md" not in orphan_ids
        assert "note:B.md" not in orphan_ids

    def test_sorted_by_id(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        orphans = find_orphans(artifacts)
        ids = [o["id"] for o in orphans]
        assert ids == sorted(ids)

    def test_no_orphans(self, tmp_path):
        nodes = [
            {"id": "note:A.md", "type": "note", "label": "A"},
            {"id": "note:B.md", "type": "note", "label": "B"},
        ]
        edges = [{"source": "note:A.md", "target": "note:B.md", "type": "links_to"}]
        _write_artifacts(tmp_path, nodes, edges)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        assert find_orphans(artifacts) == []


# ---- Stale detection -------------------------------------------------------


class TestStale:
    def test_finds_stale_by_age(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        stale = find_stale(artifacts, stale_days=180, reference_date=date(2025, 5, 1))
        stale_ids = {s["node_id"] for s in stale}
        # A: last_verified 2024-01-15 → 471 days ago → stale
        assert "note:A.md" in stale_ids
        # B: last_verified 2025-04-01 → 30 days ago → NOT stale
        assert "note:B.md" not in stale_ids

    def test_stale_with_no_date(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        stale = find_stale(artifacts, stale_days=180, reference_date=date(2025, 5, 1))
        # Orphan.md has no date → stale candidate with reason "no_date"
        orphan_stale = [s for s in stale if s["node_id"] == "note:Orphan.md"]
        assert len(orphan_stale) == 1
        assert orphan_stale[0]["reason"] == "no_date"

    def test_stale_falls_back_to_modified_date(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        stale = find_stale(artifacts, stale_days=180, reference_date=date(2025, 5, 1))
        # C: no last_verified, modified_date 2024-03-01 → 426 days → stale
        c_stale = [s for s in stale if s["node_id"] == "note:C.md"]
        assert len(c_stale) == 1
        assert c_stale[0]["reason"] == "age"

    def test_high_importance_sorted_first(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        stale = find_stale(artifacts, stale_days=180, reference_date=date(2025, 5, 1))
        # A has importance=5, C has importance=3 → A should come before C
        stale_ids = [s["node_id"] for s in stale]
        if "note:A.md" in stale_ids and "note:C.md" in stale_ids:
            assert stale_ids.index("note:A.md") < stale_ids.index("note:C.md")

    def test_no_stale(self, tmp_path):
        nodes = [
            {"id": "note:Fresh.md", "type": "note", "label": "Fresh",
             "metadata": {"last_verified": "2025-04-30"}},
        ]
        _write_artifacts(tmp_path, nodes, [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        stale = find_stale(artifacts, stale_days=180, reference_date=date(2025, 5, 1))
        assert stale == []

    def test_skips_non_note_nodes(self, tmp_path):
        nodes = [
            {"id": "tag:old", "type": "tag", "label": "old", "metadata": {}},
        ]
        _write_artifacts(tmp_path, nodes, [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        stale = find_stale(artifacts, stale_days=1, reference_date=date(2025, 5, 1))
        assert stale == []


# ---- Contradictions --------------------------------------------------------


class TestContradictions:
    def test_finds_contradiction_edges(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        contradictions = find_contradictions(artifacts)
        assert len(contradictions) == 1
        assert contradictions[0]["source"] == "note:A.md"
        assert contradictions[0]["target"] == "note:C.md"

    def test_includes_labels(self, tmp_path):
        artifacts = _simple_artifacts(tmp_path)
        contradictions = find_contradictions(artifacts)
        assert contradictions[0]["source_label"] == "A"
        assert contradictions[0]["target_label"] == "C"

    def test_no_contradictions(self, tmp_path):
        nodes = [{"id": "note:A.md", "type": "note", "label": "A"}]
        edges = [{"source": "note:A.md", "target": "note:A.md", "type": "links_to"}]
        _write_artifacts(tmp_path, nodes, edges)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        assert find_contradictions(artifacts) == []

    def test_sorted_deterministically(self, tmp_path):
        nodes = [
            {"id": "note:B.md", "type": "note", "label": "B"},
            {"id": "note:A.md", "type": "note", "label": "A"},
            {"id": "note:C.md", "type": "note", "label": "C"},
        ]
        edges = [
            {"source": "note:B.md", "target": "note:C.md", "type": "contradicts"},
            {"source": "note:A.md", "target": "note:B.md", "type": "contradicts"},
        ]
        _write_artifacts(tmp_path, nodes, edges)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        contradictions = find_contradictions(artifacts)
        sources = [c["source"] for c in contradictions]
        assert sources == sorted(sources)


# ---- Hub-spam --------------------------------------------------------------


class TestHubSpam:
    def test_detects_hub(self, tmp_path):
        # Create a hub node with many edges
        nodes = [{"id": f"note:{i}.md", "type": "note", "label": str(i)} for i in range(20)]
        edges = [
            {"source": "note:0.md", "target": f"note:{i}.md", "type": "links_to"}
            for i in range(1, 20)
        ]
        _write_artifacts(tmp_path, nodes, edges)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        hubs = find_hub_spam(artifacts, min_degree=10)
        hub_ids = {h["node_id"] for h in hubs}
        assert "note:0.md" in hub_ids

    def test_no_hubs_below_threshold(self, tmp_path):
        nodes = [
            {"id": "note:A.md", "type": "note", "label": "A"},
            {"id": "note:B.md", "type": "note", "label": "B"},
        ]
        edges = [{"source": "note:A.md", "target": "note:B.md", "type": "links_to"}]
        _write_artifacts(tmp_path, nodes, edges)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        hubs = find_hub_spam(artifacts, min_degree=10)
        assert hubs == []

    def test_sorted_by_degree_desc(self, tmp_path):
        nodes = [{"id": f"note:{i}.md", "type": "note", "label": str(i)} for i in range(30)]
        edges = []
        for i in range(1, 25):
            edges.append({"source": "note:0.md", "target": f"note:{i}.md", "type": "links_to"})
        for i in range(1, 15):
            edges.append({"source": "note:1.md", "target": f"note:{i+10}.md", "type": "links_to"})
        _write_artifacts(tmp_path, nodes, edges)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        hubs = find_hub_spam(artifacts, min_degree=10)
        if len(hubs) >= 2:
            assert hubs[0]["degree"] >= hubs[1]["degree"]


# ---- Status report ---------------------------------------------------------


class TestStatusReport:
    def test_builds_report(self, tmp_path):
        _simple_artifacts(tmp_path)  # writes artifacts
        cfg = _make_config(tmp_path)
        report = build_status_report(cfg, stale_days=180)
        assert report.node_count == 8
        assert report.edge_count == 7
        assert report.broken_count == 1
        assert report.unresolved_count == 1
        assert report.contradiction_count == 1
        assert report.orphan_count >= 1

    def test_report_summary_string(self, tmp_path):
        _simple_artifacts(tmp_path)
        cfg = _make_config(tmp_path)
        report = build_status_report(cfg)
        summary = report.summary()
        assert "8 nodes" in summary
        assert "7 edges" in summary
        assert "Orphan" in summary or "orphan" in summary.lower()

    def test_missing_artifacts_raises(self, tmp_path):
        cfg = _make_config(tmp_path)
        with pytest.raises(ArtifactNotFoundError):
            build_status_report(cfg)


# ---- CLI tests -------------------------------------------------------------


class TestCLIGraphDiagnostics:
    """Test the CLI subcommands dispatch correctly."""

    def _setup_artifacts(self, tmp_path):
        """Write artifacts and a config pointing to them."""
        _simple_artifacts(tmp_path)
        # Write a minimal config.yaml
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"vault:\n  path: {tmp_path / 'vault'}\n"
            f"index:\n  chunks_path: {tmp_path / 'chunks.jsonl'}\n"
            f"  chroma_path: {tmp_path / 'chroma'}\n"
        )
        # Also create the chunks.jsonl (can be empty for diagnostic commands)
        (tmp_path / "chunks.jsonl").touch()
        (tmp_path / "vault").mkdir(exist_ok=True)
        return str(cfg_path)

    def test_graph_status(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "status"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "8 nodes" in out
        assert "7 edges" in out

    def test_graph_broken(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "broken"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Ghost" in out
        assert "UNRESOLVED" in out

    def test_graph_broken_json(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "broken", "--json"])
        assert ret == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert data[0]["kind"] == "unresolved"

    def test_graph_orphans(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "orphans"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Orphan" in out

    def test_graph_centrality(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "centrality", "--limit", "5"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Rank" in out or "rank" in out.lower()
        assert "Deg" in out

    def test_graph_centrality_pagerank(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "centrality", "--pagerank", "--limit", "3"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "PageRank" in out

    def test_graph_centrality_json(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "centrality", "--json", "--limit", "3"])
        assert ret == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert "degree" in data[0]

    def test_graph_centrality_filter_type(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "centrality", "--node-type", "note", "--json"])
        assert ret == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        for entry in data:
            assert entry["node_type"] == "note"

    def test_graph_stale(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "stale", "--stale-days", "180"])
        assert ret == 0
        out = capsys.readouterr().out
        # Should find stale candidates (A is from 2024-01-15)
        assert "stale" in out.lower() or "imp=" in out

    def test_graph_contradictions(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "contradictions"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "A" in out and "C" in out

    def test_graph_contradictions_json(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = self._setup_artifacts(tmp_path)
        ret = main(["graph", "--config", cfg_path, "contradictions", "--json"])
        assert ret == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert data[0]["source"] == "note:A.md"

    def test_graph_status_missing_artifacts(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"vault:\n  path: {tmp_path / 'vault'}\n"
            f"index:\n  chunks_path: {tmp_path / 'chunks.jsonl'}\n"
            f"  chroma_path: {tmp_path / 'chroma'}\n"
        )
        (tmp_path / "vault").mkdir(exist_ok=True)
        ret = main(["graph", "--config", str(cfg_path), "status"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "cortex graph build" in err
