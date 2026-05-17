"""Unit tests for cortex.graph_export — Phase 5.5 / Slice 3.

Tests cover:
  - JSON export: full graph, round-trip, filtered subgraph
  - Mermaid export: syntax validity, subgraph grouping, edge labels
  - Filtering: by node type, edge type, neighborhood
  - CLI: export --format json, export --format mermaid, --output file,
         missing artifacts error, filtered export
"""

from __future__ import annotations

import json
from pathlib import Path


from cortex.graph_diagnostics import GraphArtifacts
from cortex.graph_export import (
    export_d3_json,
    export_json,
    export_mermaid,
    filter_subgraph,
)


# ---- Helpers ---------------------------------------------------------------


def _write_artifacts(
    tmp_path: Path,
    nodes: list[dict],
    edges: list[dict],
    broken: list[dict] | None = None,
) -> Path:
    d = tmp_path / "graph"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "graph_nodes.jsonl").open("w") as f:
        for n in nodes:
            f.write(json.dumps(n) + "\n")
    with (d / "graph_edges.jsonl").open("w") as f:
        for e in edges:
            f.write(json.dumps(e) + "\n")
    with (d / "graph_broken.jsonl").open("w") as f:
        for b in broken or []:
            f.write(json.dumps(b) + "\n")
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


NODES = [
    {"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"},
    {"id": "note:B.md", "type": "note", "label": "B", "file": "B.md"},
    {"id": "note:C.md", "type": "note", "label": "C", "file": "C.md"},
    {"id": "chunk:a.md#intro", "type": "chunk", "label": "(intro)", "file": "A.md"},
    {"id": "tag:python", "type": "tag", "label": "python"},
]
EDGES = [
    {"source": "note:A.md", "target": "note:B.md", "type": "links_to"},
    {"source": "note:A.md", "target": "note:C.md", "type": "links_to"},
    {"source": "note:A.md", "target": "chunk:a.md#intro", "type": "contains"},
    {"source": "note:A.md", "target": "tag:python", "type": "tagged_with"},
    {"source": "note:B.md", "target": "note:C.md", "type": "contradicts"},
]


def _load_artifacts(tmp_path: Path) -> GraphArtifacts:
    _write_artifacts(tmp_path, NODES, EDGES)
    cfg = _make_config(tmp_path)
    return GraphArtifacts.load(cfg)


# ---- filter_subgraph -------------------------------------------------------


class TestFilterSubgraph:
    def test_no_filters_returns_all(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(artifacts)
        assert len(nodes) == 5
        assert len(edges) == 5

    def test_filter_by_node_type(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(artifacts, node_types=["note"])
        assert all(n["type"] == "note" for n in nodes)
        assert len(nodes) == 3
        # Edges referencing chunk/tag nodes should be pruned
        for e in edges:
            assert e["source"] in {n["id"] for n in nodes}
            assert e["target"] in {n["id"] for n in nodes}

    def test_filter_by_edge_type(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(artifacts, edge_types=["links_to"])
        assert all(e["type"] == "links_to" for e in edges)
        assert len(edges) == 2

    def test_filter_by_neighborhood(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(artifacts, neighborhood="note:B.md")
        node_ids = {n["id"] for n in nodes}
        # B's neighbors: A (incoming links_to), C (outgoing contradicts)
        assert "note:B.md" in node_ids
        assert "note:A.md" in node_ids
        assert "note:C.md" in node_ids
        # chunk and tag should NOT be included (not neighbors of B)
        assert "chunk:a.md#intro" not in node_ids
        assert "tag:python" not in node_ids

    def test_combined_filters(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(
            artifacts,
            node_types=["note"],
            edge_types=["links_to"],
        )
        assert all(n["type"] == "note" for n in nodes)
        assert all(e["type"] == "links_to" for e in edges)

    def test_output_is_sorted(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(artifacts)
        node_ids = [n["id"] for n in nodes]
        assert node_ids == sorted(node_ids)
        edge_keys = [(e["source"], e["target"], e["type"]) for e in edges]
        assert edge_keys == sorted(edge_keys)

    def test_neighborhood_nonexistent_node(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        nodes, edges = filter_subgraph(artifacts, neighborhood="note:GHOST.md")
        # No node matches, no neighbors → empty
        assert len(nodes) == 0
        assert len(edges) == 0


# ---- JSON export -----------------------------------------------------------


class TestExportJSON:
    def test_full_graph(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_json(artifacts)
        doc = json.loads(output)
        assert "nodes" in doc
        assert "edges" in doc
        assert "stats" in doc
        assert doc["stats"]["node_count"] == 5
        assert doc["stats"]["edge_count"] == 5
        assert doc["stats"]["filtered"] is False

    def test_round_trip(self, tmp_path):
        """JSON export should be parseable back to the same structure."""
        artifacts = _load_artifacts(tmp_path)
        output = export_json(artifacts)
        doc = json.loads(output)
        # Re-export from the parsed doc
        re_output = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=False)
        assert json.loads(re_output) == doc

    def test_filtered_marks_stats(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_json(artifacts, node_types=["note"])
        doc = json.loads(output)
        assert doc["stats"]["filtered"] is True
        assert doc["stats"]["node_count"] == 3

    def test_neighborhood_export(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_json(artifacts, neighborhood="note:B.md")
        doc = json.loads(output)
        node_ids = {n["id"] for n in doc["nodes"]}
        assert "note:B.md" in node_ids
        assert doc["stats"]["filtered"] is True

    def test_empty_graph(self, tmp_path):
        _write_artifacts(tmp_path, [], [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        output = export_json(artifacts)
        doc = json.loads(output)
        assert doc["nodes"] == []
        assert doc["edges"] == []


# ---- D3 JSON export --------------------------------------------------------

class TestExportD3JSON:
    def test_includes_viewer_schema_and_degree_metrics(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_d3_json(artifacts)
        doc = json.loads(output)

        assert set(doc) == {"nodes", "edges", "stats"}
        assert doc["stats"] == {
            "node_count": 5,
            "edge_count": 5,
            "filtered": False,
        }

        nodes = {n["id"]: n for n in doc["nodes"]}
        assert nodes["note:A.md"] == {
            "id": "note:A.md",
            "label": "A",
            "type": "note",
            "file": "A.md",
            "degree": 4,
            "in_degree": 0,
            "out_degree": 4,
            "fm_status": "",
            "fm_type": "",
        }
        assert nodes["note:C.md"]["degree"] == 2
        assert nodes["note:C.md"]["in_degree"] == 2
        assert nodes["note:C.md"]["out_degree"] == 0

        edge = doc["edges"][0]
        assert set(edge) == {"source", "target", "type"}

    def test_filtering_and_neighborhood_match_existing_export_semantics(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_d3_json(
            artifacts,
            node_types=["note"],
            edge_types=["links_to"],
            neighborhood="note:B.md",
        )
        doc = json.loads(output)

        assert doc["stats"]["filtered"] is True
        assert {n["id"] for n in doc["nodes"]} == {"note:A.md", "note:B.md", "note:C.md"}
        assert all(n["type"] == "note" for n in doc["nodes"])
        assert all(e["type"] == "links_to" for e in doc["edges"])
        assert [n["id"] for n in doc["nodes"]] == sorted(n["id"] for n in doc["nodes"])

    def test_empty_graph(self, tmp_path):
        _write_artifacts(tmp_path, [], [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        doc = json.loads(export_d3_json(artifacts))

        assert doc["nodes"] == []
        assert doc["edges"] == []
        assert doc["stats"]["node_count"] == 0
        assert doc["stats"]["edge_count"] == 0

    def test_pagerank_omitted_by_default(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        doc = json.loads(export_d3_json(artifacts))

        assert doc["nodes"]
        assert all("pagerank" not in node for node in doc["nodes"])

    def test_pagerank_opt_in(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        doc = json.loads(export_d3_json(artifacts, include_pagerank=True))
        nodes = {n["id"]: n for n in doc["nodes"]}

        assert all("pagerank" in node for node in doc["nodes"])
        assert all(node["pagerank"] > 0 for node in doc["nodes"])
        assert abs(sum(node["pagerank"] for node in nodes.values()) - 1.0) < 0.01

    def test_filtered_degree_metrics_use_filtered_subgraph(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        doc = json.loads(export_d3_json(artifacts, edge_types=["links_to"]))
        nodes = {n["id"]: n for n in doc["nodes"]}

        assert nodes["note:A.md"]["degree"] == 2
        assert nodes["note:A.md"]["out_degree"] == 2
        assert nodes["note:B.md"]["degree"] == 1
        assert nodes["note:B.md"]["in_degree"] == 1
        assert nodes["tag:python"]["degree"] == 0

    def test_diagnostics_overlay_adds_unresolved_ghost_node_with_stable_id(self, tmp_path):
        broken = [
            {
                "source_node": "note:A.md",
                "target_raw": "Missing Note",
                "kind": "unresolved",
                "context": "[[Missing Note]]",
            }
        ]
        _write_artifacts(tmp_path, NODES, EDGES, broken=broken)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)

        first = json.loads(export_d3_json(artifacts, include_diagnostics=True))
        second = json.loads(export_d3_json(artifacts, include_diagnostics=True))

        ghost_nodes = [n for n in first["nodes"] if n.get("diagnostic_kind") == "unresolved"]
        assert len(ghost_nodes) == 1
        ghost = ghost_nodes[0]
        assert ghost["id"].startswith("diagnostic:unresolved:")
        assert ghost["id"] == [n for n in second["nodes"] if n.get("diagnostic_kind") == "unresolved"][0]["id"]
        assert ghost["label"] == "Missing Note"
        assert ghost["visual"] == {"status": "ghost", "color": "red"}
        assert {
            "source": "note:A.md",
            "target": ghost["id"],
            "type": "diagnostic_unresolved",
        } in first["edges"]
        assert first["stats"]["diagnostics"] == {"unresolved": 1, "ambiguous": 0, "orphans": 0}

    def test_diagnostics_overlay_adds_ambiguous_node_and_candidate_edges(self, tmp_path):
        broken = [
            {
                "source_node": "note:A.md",
                "target_raw": "Maybe",
                "kind": "ambiguous",
                "candidates": ["note:C.md", "note:B.md"],
            }
        ]
        _write_artifacts(tmp_path, NODES, EDGES, broken=broken)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)

        doc = json.loads(export_d3_json(artifacts, include_diagnostics=True))
        amb = [n for n in doc["nodes"] if n.get("diagnostic_kind") == "ambiguous"][0]

        assert amb["id"].startswith("diagnostic:ambiguous:")
        assert amb["label"] == "Maybe"
        assert amb["candidates"] == ["note:B.md", "note:C.md"]
        assert amb["visual"] == {"status": "diagnostic", "color": "orange"}
        assert {"source": "note:A.md", "target": amb["id"], "type": "diagnostic_ambiguous"} in doc["edges"]
        assert {"source": amb["id"], "target": "note:B.md", "type": "diagnostic_candidate"} in doc["edges"]
        assert {"source": amb["id"], "target": "note:C.md", "type": "diagnostic_candidate"} in doc["edges"]
        assert doc["stats"]["diagnostics"] == {"unresolved": 0, "ambiguous": 1, "orphans": 0}

    def test_diagnostics_overlay_marks_orphan_nodes_without_extra_nodes(self, tmp_path):
        nodes = NODES + [{"id": "note:Lonely.md", "type": "note", "label": "Lonely", "file": "Lonely.md"}]
        _write_artifacts(tmp_path, nodes, EDGES)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)

        doc = json.loads(export_d3_json(artifacts, include_diagnostics=True))
        by_id = {n["id"]: n for n in doc["nodes"]}

        assert by_id["note:Lonely.md"]["diagnostics"] == {"orphan": True}
        assert by_id["note:Lonely.md"]["visual"] == {"status": "orphan", "color": "muted"}
        assert not [n for n in doc["nodes"] if n["id"].startswith("diagnostic:orphan:")]
        assert doc["stats"]["diagnostics"] == {"unresolved": 0, "ambiguous": 0, "orphans": 1}

    def test_diagnostics_overlay_respects_filtered_visible_sources(self, tmp_path):
        broken = [
            {"source_node": "note:A.md", "target_raw": "Visible Missing", "kind": "unresolved"},
            {"source_node": "chunk:a.md#intro", "target_raw": "Hidden Missing", "kind": "unresolved"},
        ]
        _write_artifacts(tmp_path, NODES, EDGES, broken=broken)
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)

        doc = json.loads(export_d3_json(artifacts, node_types=["note"], include_diagnostics=True))

        labels = {n["label"] for n in doc["nodes"] if n.get("diagnostic_kind") == "unresolved"}
        assert labels == {"Visible Missing"}
        assert doc["stats"]["diagnostics"]["unresolved"] == 1


# ---- Mermaid export --------------------------------------------------------


class TestExportMermaid:
    def test_starts_with_graph_directive(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts)
        assert output.startswith("graph LR\n")

    def test_direction_override(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts, direction="TD")
        assert output.startswith("graph TD\n")

    def test_has_subgraph_per_type(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts)
        assert "subgraph notes" in output
        assert "subgraph chunks" in output
        assert "subgraph tags" in output
        # Each subgraph must be closed
        assert output.count("subgraph") == output.count("    end")

    def test_node_labels_present(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts)
        assert '"A"' in output
        assert '"B"' in output
        assert '"python"' in output

    def test_edge_labels_present(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts)
        assert "links to" in output
        assert "contains" in output
        assert "tagged with" in output

    def test_contradicts_uses_special_arrow(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts)
        assert "x--x" in output

    def test_filtered_mermaid(self, tmp_path):
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts, node_types=["note"])
        assert "subgraph notes" in output
        assert "subgraph chunks" not in output
        assert "subgraph tags" not in output

    def test_no_mermaid_syntax_errors(self, tmp_path):
        """Basic Mermaid syntax validation: no unbalanced quotes or brackets."""
        artifacts = _load_artifacts(tmp_path)
        output = export_mermaid(artifacts)
        # Check balanced quotes in node definitions
        for line in output.split("\n"):
            stripped = line.strip()
            if '("' in stripped or '(["' in stripped or '{{"' in stripped:
                assert stripped.count('"') % 2 == 0, f"Unbalanced quotes: {stripped}"

    def test_empty_graph_mermaid(self, tmp_path):
        _write_artifacts(tmp_path, [], [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        output = export_mermaid(artifacts)
        assert output.startswith("graph LR\n")
        # No subgraphs for empty graph
        assert "subgraph" not in output

    def test_special_chars_in_labels(self, tmp_path):
        """Labels with special chars should be escaped."""
        nodes = [
            {"id": "note:test.md", "type": "note", "label": 'A "quoted" [note]'},
        ]
        _write_artifacts(tmp_path, nodes, [])
        cfg = _make_config(tmp_path)
        artifacts = GraphArtifacts.load(cfg)
        output = export_mermaid(artifacts)
        # The unsafe chars should be stripped
        assert '"' not in output.split("\n")[2].replace('"A quoted note"', '')
        # But the label should still be present (cleaned)
        assert "A quoted note" in output


# ---- CLI tests -------------------------------------------------------------


class TestCLIGraphExport:
    def _setup(self, tmp_path, broken: list[dict] | None = None):
        _write_artifacts(tmp_path, NODES, EDGES, broken=broken)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"vault:\n  path: {tmp_path / 'vault'}\n"
            f"index:\n  chunks_path: {tmp_path / 'chunks.jsonl'}\n"
            f"  chroma_path: {tmp_path / 'chroma'}\n"
        )
        (tmp_path / "chunks.jsonl").touch()
        (tmp_path / "vault").mkdir(exist_ok=True)
        return str(cfg_path)

    def test_export_json_stdout(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "json"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert len(doc["nodes"]) == 5

    def test_export_mermaid_stdout(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "mermaid"])
        assert ret == 0
        out = capsys.readouterr().out
        assert out.startswith("graph LR")

    def test_export_json_to_file(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        out_file = str(tmp_path / "export.json")
        ret = main(["graph", "--config", cfg, "export", "--format", "json", "-o", out_file])
        assert ret == 0
        content = Path(out_file).read_text()
        doc = json.loads(content)
        assert len(doc["nodes"]) == 5
        # stderr should mention the file
        err = capsys.readouterr().err
        assert "export.json" in err

    def test_export_d3_json_stdout(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "d3-json"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert set(doc) == {"nodes", "edges", "stats"}
        assert doc["nodes"][0].keys() >= {"id", "label", "type", "degree", "in_degree", "out_degree"}
        assert all("pagerank" not in node for node in doc["nodes"])

    def test_export_d3_json_pagerank_flag(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "d3-json", "--pagerank"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert all("pagerank" in node for node in doc["nodes"])

    def test_export_d3_json_diagnostics_flag(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(
            tmp_path,
            broken=[{"source_node": "note:A.md", "target_raw": "Missing", "kind": "unresolved"}],
        )
        ret = main(["graph", "--config", cfg, "export", "--format", "d3-json", "--diagnostics"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert doc["stats"]["diagnostics"]["unresolved"] == 1
        assert any(node.get("diagnostic_kind") == "unresolved" for node in doc["nodes"])

    def test_export_mermaid_to_file(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        out_file = str(tmp_path / "graph.mmd")
        ret = main(["graph", "--config", cfg, "export", "--format", "mermaid", "-o", out_file])
        assert ret == 0
        content = Path(out_file).read_text()
        assert content.startswith("graph LR")

    def test_export_filtered_by_node_type(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "json", "--node-type", "note"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert all(n["type"] == "note" for n in doc["nodes"])
        assert doc["stats"]["filtered"] is True

    def test_export_filtered_by_edge_type(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "json", "--edge-type", "links_to"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        assert all(e["type"] == "links_to" for e in doc["edges"])

    def test_export_neighborhood(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "json", "--neighborhood", "note:B.md"])
        assert ret == 0
        out = capsys.readouterr().out
        doc = json.loads(out)
        node_ids = {n["id"] for n in doc["nodes"]}
        assert "note:B.md" in node_ids

    def test_export_missing_artifacts(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(
            f"vault:\n  path: {tmp_path / 'vault'}\n"
            f"index:\n  chunks_path: {tmp_path / 'chunks.jsonl'}\n"
            f"  chroma_path: {tmp_path / 'chroma'}\n"
        )
        (tmp_path / "vault").mkdir(exist_ok=True)
        ret = main(["graph", "--config", str(cfg_path), "export", "--format", "json"])
        assert ret == 1
        err = capsys.readouterr().err
        assert "cortex graph build" in err

    def test_export_mermaid_direction(self, tmp_path, capsys):
        from cortex.cli import main
        cfg = self._setup(tmp_path)
        ret = main(["graph", "--config", cfg, "export", "--format", "mermaid", "--direction", "TD"])
        assert ret == 0
        out = capsys.readouterr().out
        assert out.startswith("graph TD")
