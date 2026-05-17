"""Tests for the standalone D3 graph viewer generator — Phase 5.6 / Slice 3."""

from __future__ import annotations

import json
from pathlib import Path

from cortex.graph_diagnostics import GraphArtifacts
from cortex.graph_export import export_d3_json
from cortex.graph_viewer import generate_graph_viewer_html


NODES = [
    {"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"},
    {"id": "note:B.md", "type": "note", "label": "B", "file": "B.md"},
]
EDGES = [
    {"source": "note:A.md", "target": "note:B.md", "type": "links_to"},
]


def _write_artifacts(
    tmp_path: Path,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    broken: list[dict] | None = None,
) -> Path:
    nodes = NODES if nodes is None else nodes
    edges = EDGES if edges is None else edges
    d = tmp_path / "graph"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "graph_nodes.jsonl").open("w", encoding="utf-8") as f:
        for n in nodes:
            f.write(json.dumps(n) + "\n")
    with (d / "graph_edges.jsonl").open("w", encoding="utf-8") as f:
        for e in edges:
            f.write(json.dumps(e) + "\n")
    with (d / "graph_broken.jsonl").open("w", encoding="utf-8") as f:
        for b in broken or []:
            f.write(json.dumps(b) + "\n")
    (d / "graph_stats.json").write_text(
        json.dumps({"node_count": len(nodes), "edge_count": len(edges), "broken_count": len(broken or [])}),
        encoding="utf-8",
    )
    return d


def _make_config(tmp_path: Path) -> str:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"vault:\n  path: {tmp_path / 'vault'}\n"
        f"index:\n  chunks_path: {tmp_path / 'chunks.jsonl'}\n"
        f"  chroma_path: {tmp_path / 'chroma'}\n",
        encoding="utf-8",
    )
    (tmp_path / "chunks.jsonl").touch()
    (tmp_path / "vault").mkdir(exist_ok=True)
    return str(cfg_path)


def _load_artifacts(tmp_path: Path) -> GraphArtifacts:
    _write_artifacts(tmp_path)
    from cortex.config import load_config

    return GraphArtifacts.load(load_config(_make_config(tmp_path)))


class TestGraphViewerGenerator:
    def test_external_data_mode_fetches_relative_graph_data_path(self):
        html = generate_graph_viewer_html(data_path="graph_data.json")

        assert "https://d3js.org/d3.v7.min.js" in html
        assert "fetch('graph_data.json')" in html
        assert "function initGraph(data)" in html
        assert "forceSimulation(data.nodes)" in html
        assert "__EMBEDDED_GRAPH_DATA__" not in html

    def test_viewer_contains_suspicious_filter_button(self):
        html = generate_graph_viewer_html(data_path="graph_data.json")

        assert 'id="suspicious-filter"' in html
        assert "Show suspicious memory" in html
        assert "function computeSuspiciousFlags" in html
        assert "function renderSuspiciousReasons" in html
        assert "broken links" in html
        assert "missing type" in html
        assert "missing status" in html
        assert "missing domain" in html
        assert "contradiction flag" in html
        assert "this.classList.toggle('active')" in html
        assert "#controls button.active" in html

    def test_embedded_data_mode_serializes_json_without_fetch(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        graph_json = export_d3_json(artifacts)
        html = generate_graph_viewer_html(embedded_json=graph_json)

        assert "const embeddedGraphData =" in html
        assert "fetch(" not in html
        embedded = html.split("const embeddedGraphData = ", 1)[1].split(";\n", 1)[0]
        assert json.loads(embedded) == json.loads(graph_json)

    def test_rejects_both_external_and_embedded_modes(self):
        graph_json = json.dumps({"nodes": [], "edges": [], "stats": {}})

        try:
            generate_graph_viewer_html(data_path="graph_data.json", embedded_json=graph_json)
        except ValueError as e:
            assert "Choose either data_path or embedded_json" in str(e)
        else:  # pragma: no cover - failing branch documents the contract
            raise AssertionError("Expected ValueError")

    def test_generator_does_not_mutate_graph_artifacts(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        before = (list(artifacts.nodes), list(artifacts.edges), dict(artifacts.stats))

        graph_json = export_d3_json(artifacts)
        generate_graph_viewer_html(embedded_json=graph_json)

        assert artifacts.nodes == before[0]
        assert artifacts.edges == before[1]
        assert artifacts.stats == before[2]

    def test_viewer_contains_diagnostic_rendering_rules(self):
        html = generate_graph_viewer_html(data_path="graph_data.json")

        assert "diagnostic_unresolved" in html
        assert "diagnostic_ambiguous" in html
        assert "diagnostic_candidate" in html
        assert "d.visual?.color === 'red'" in html
        assert "d.visual?.color === 'orange'" in html
        assert "d.visual?.status === 'orphan'" in html

    def test_viewer_contains_client_side_filter_controls(self):
        html = generate_graph_viewer_html(data_path="graph_data.json")

        assert 'id="search"' in html
        assert 'id="node-type-filter"' in html
        assert 'id="edge-type-filter"' in html
        assert 'id="status-filter"' in html
        assert "function populateFilterOptions" in html
        assert "function applyFilters" in html
        assert "matchesSearch" in html
        assert "matchesNodeType" in html
        assert "matchesEdgeType" in html
        assert "matchesStatus" in html

    def test_viewer_contains_neighborhood_focus_and_detail_panel(self):
        html = generate_graph_viewer_html(data_path="graph_data.json")

        assert 'id="focus-neighborhood"' in html
        assert 'id="reset-focus"' in html
        assert 'id="details"' in html
        assert "let focusedNodeId = null" in html
        assert "function focusNeighborhood" in html
        assert "function resetNeighborhoodFocus" in html
        assert "function updateDetails" in html
        assert "in_degree" in html
        assert "out_degree" in html
        assert "diagnostics" in html

    def test_viewer_contains_basic_force_sliders(self):
        html = generate_graph_viewer_html(data_path="graph_data.json")

        assert 'id="link-distance"' in html
        assert 'id="charge-strength"' in html
        assert 'id="collision-radius"' in html
        assert "simulation.force('link').distance" in html
        assert "simulation.force('charge').strength" in html
        assert "simulation.force('collision').radius" in html
        assert "simulation.alpha(0.3).restart()" in html


class TestCLIGraphViewer:
    def test_viewer_writes_external_data_html_file(self, tmp_path, capsys):
        from cortex.cli import main

        _write_artifacts(tmp_path)
        cfg = _make_config(tmp_path)
        out = tmp_path / "graph.html"
        ret = main(["graph", "--config", cfg, "viewer", "-o", str(out), "--data", "graph_data.json"])

        assert ret == 0
        assert out.exists()
        html = out.read_text(encoding="utf-8")
        assert "fetch('graph_data.json')" in html
        assert "https://d3js.org/d3.v7.min.js" in html
        assert "Viewer written to" in capsys.readouterr().err

    def test_viewer_embedded_mode_writes_portable_html(self, tmp_path):
        from cortex.cli import main

        _write_artifacts(tmp_path)
        cfg = _make_config(tmp_path)
        out = tmp_path / "portable.html"
        ret = main(["graph", "--config", cfg, "viewer", "-o", str(out), "--embed-data"])

        assert ret == 0
        html = out.read_text(encoding="utf-8")
        assert "const embeddedGraphData =" in html
        assert "fetch(" not in html
        assert '"node_count": 2' in html

    def test_viewer_embedded_diagnostics_mode_includes_overlay_data(self, tmp_path):
        from cortex.cli import main

        broken = [{"source_node": "note:A.md", "target_raw": "Missing", "kind": "unresolved"}]
        _write_artifacts(tmp_path, broken=broken)
        cfg = _make_config(tmp_path)
        out = tmp_path / "diagnostics.html"
        ret = main(["graph", "--config", cfg, "viewer", "-o", str(out), "--embed-data", "--diagnostics"])

        assert ret == 0
        html = out.read_text(encoding="utf-8")
        assert "diagnostic_unresolved" in html
        assert "diagnostic:unresolved:" in html
        assert '"diagnostics": {"unresolved": 1, "ambiguous": 0, "orphans": 0}' in html

    def test_viewer_missing_artifacts_returns_clear_error(self, tmp_path, capsys):
        from cortex.cli import main

        cfg = _make_config(tmp_path)
        out = tmp_path / "graph.html"
        ret = main(["graph", "--config", cfg, "viewer", "-o", str(out), "--embed-data"])

        assert ret == 1
        assert not out.exists()
        err = capsys.readouterr().err
        assert "Graph artifact not found" in err
        assert "cortex graph build" in err
