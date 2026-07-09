"""Unit tests for cortex.lifecycle — Phase 6 / Slice 1.

Tests cover:
  - StepResult / MaintenanceReport models
  - run_maintenance orchestration (mocked steps)
  - Rebuild ordering: index before embed before graph
  - Error abort: later steps skipped on earlier failure
  - Dry-run: probes changes without writing
  - _run_index integration with a real mini-vault
  - Content-hash change detection in dry-run
  - CLI: maintenance, maintenance --dry-run, nightly/weekly stubs
"""

from __future__ import annotations

import json
import textwrap
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cortex.lifecycle import (
    MaintenanceReport,
    NightlyPromotionReport,
    StepResult,
    WeeklyReviewReport,
    _dry_run_probe,
    _run_graph_build,
    _run_index,
    run_maintenance,
    run_nightly_promotion,
    run_weekly_review,
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
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return Config(
        vault=VaultConfig(path=vault),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(),
        search=SearchConfig(),
        context_builder=ContextBuilderConfig(),
    )


def _write_note(vault_path: Path, name: str, content: str) -> Path:
    """Write a markdown note into the vault."""
    p = vault_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _make_cli_config(tmp_path: Path) -> str:
    """Write a config.yaml and return its path."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"vault:\n  path: {vault}\n"
        f"index:\n  chunks_path: {tmp_path / 'chunks.jsonl'}\n"
        f"  chroma_path: {tmp_path / 'chroma'}\n"
    )
    return str(cfg_path)


def _write_graph_artifacts(
    cfg,
    *,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    broken: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
) -> Path:
    """Write graph artifacts using the graph_index artifact layout."""
    graph_dir = cfg.index.chunks_path.parent / "graph"
    graph_dir.mkdir(parents=True, exist_ok=True)
    for filename, rows in (
        ("graph_nodes.jsonl", nodes),
        ("graph_edges.jsonl", edges),
        ("graph_broken.jsonl", broken or []),
    ):
        with (graph_dir / filename).open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    if stats is None:
        stats = {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "broken_count": len(broken or []),
            "nodes_by_type": {},
            "edges_by_type": {},
            "broken_by_kind": {},
        }
    (graph_dir / "graph_stats.json").write_text(json.dumps(stats), encoding="utf-8")
    return graph_dir


def _write_chunks(cfg, chunks: list[dict[str, Any]]) -> None:
    cfg.index.chunks_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.index.chunks_path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")


# ---- StepResult / MaintenanceReport models ---------------------------------


class TestStepResult:
    def test_ok_when_no_error(self):
        r = StepResult(name="test")
        assert r.ok is True

    def test_not_ok_when_error(self):
        r = StepResult(name="test", error="boom")
        assert r.ok is False

    def test_skipped(self):
        r = StepResult(name="test", skipped=True, skip_reason="nothing to do")
        assert r.ok is True
        assert r.skipped is True


class TestMaintenanceReport:
    def test_ok_all_steps_pass(self):
        report = MaintenanceReport(steps=[
            StepResult(name="a"),
            StepResult(name="b"),
        ])
        assert report.ok is True

    def test_not_ok_if_any_step_fails(self):
        report = MaintenanceReport(steps=[
            StepResult(name="a"),
            StepResult(name="b", error="fail"),
        ])
        assert report.ok is False
        assert len(report.errors) == 1

    def test_summary_includes_step_names(self):
        report = MaintenanceReport(steps=[
            StepResult(name="index", summary="Indexed 5 files"),
            StepResult(name="embed", skipped=True, skip_reason="no changes"),
        ])
        text = report.summary()
        assert "index" in text
        assert "embed" in text
        assert "SKIP" in text

    def test_dry_run_prefix(self):
        report = MaintenanceReport(dry_run=True, steps=[
            StepResult(name="dry_run_probe"),
        ])
        text = report.summary()
        assert "[DRY RUN]" in text


# ---- run_maintenance with mocked steps -------------------------------------


class TestRunMaintenance:
    def _mock_steps(self, index_result=None, embed_result=None, graph_result=None):
        """Return patches for the three step functions."""
        if index_result is None:
            index_result = StepResult(name="index", summary="Indexed 3 files")
        if embed_result is None:
            embed_result = StepResult(name="embed", summary="Embedded 10 chunks")
        if graph_result is None:
            graph_result = StepResult(name="graph_build", summary="Built graph: 5 nodes")

        return (
            patch("cortex.lifecycle._run_index", return_value=index_result),
            patch("cortex.lifecycle._run_embed", return_value=embed_result),
            patch("cortex.lifecycle._run_graph_build", return_value=graph_result),
        )

    def test_runs_all_three_steps(self, tmp_path):
        cfg = _make_config(tmp_path)
        p_idx, p_emb, p_gr = self._mock_steps()
        with p_idx as m_idx, p_emb as m_emb, p_gr as m_gr:
            report = run_maintenance(cfg)
        assert len(report.steps) == 3
        assert report.ok is True
        m_idx.assert_called_once()
        m_emb.assert_called_once()
        m_gr.assert_called_once()

    def test_ordering_index_before_embed_before_graph(self, tmp_path):
        cfg = _make_config(tmp_path)
        call_order = []
        def mock_index(cfg, **kw):
            call_order.append("index")
            return StepResult(name="index")
        def mock_embed(cfg, **kw):
            call_order.append("embed")
            return StepResult(name="embed")
        def mock_graph(cfg, **kw):
            call_order.append("graph")
            return StepResult(name="graph_build")

        with patch("cortex.lifecycle._run_index", side_effect=mock_index), \
             patch("cortex.lifecycle._run_embed", side_effect=mock_embed), \
             patch("cortex.lifecycle._run_graph_build", side_effect=mock_graph):
            run_maintenance(cfg)

        assert call_order == ["index", "embed", "graph"]

    def test_aborts_on_index_error(self, tmp_path):
        cfg = _make_config(tmp_path)
        p_idx, p_emb, p_gr = self._mock_steps(
            index_result=StepResult(name="index", error="vault missing"),
        )
        with p_idx, p_emb as m_emb, p_gr as m_gr:
            report = run_maintenance(cfg)
        assert not report.ok
        assert len(report.steps) == 1  # only index
        m_emb.assert_not_called()
        m_gr.assert_not_called()

    def test_aborts_on_embed_error(self, tmp_path):
        cfg = _make_config(tmp_path)
        p_idx, p_emb, p_gr = self._mock_steps(
            embed_result=StepResult(name="embed", error="model mismatch"),
        )
        with p_idx, p_emb, p_gr as m_gr:
            report = run_maintenance(cfg)
        assert not report.ok
        assert len(report.steps) == 2  # index + embed
        m_gr.assert_not_called()

    def test_force_passed_through(self, tmp_path):
        cfg = _make_config(tmp_path)
        p_idx, p_emb, p_gr = self._mock_steps()
        with p_idx as m_idx, p_emb as m_emb, p_gr as m_gr:
            run_maintenance(cfg, force=True)
        # Check force=True was passed
        _, kwargs = m_idx.call_args
        assert kwargs.get("force") is True
        _, kwargs = m_emb.call_args
        assert kwargs.get("force") is True
        _, kwargs = m_gr.call_args
        assert kwargs.get("force") is True


# ---- Dry-run probe ---------------------------------------------------------


class TestDryRunProbe:
    def test_empty_vault_no_chunks(self, tmp_path):
        cfg = _make_config(tmp_path)
        result = _dry_run_probe(cfg)
        assert result.ok
        # No vault files, no chunks → "chunks.jsonl missing" or "graph artifacts missing"
        assert "missing" in result.summary.lower() or "nothing" in result.summary.lower()

    def test_detects_new_files(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "facts/Test.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [test]
            confidence: 0.8
            importance: 3
            stability: stable
            ---
            # Test Note
            Some content here.
        """))
        result = _dry_run_probe(cfg)
        assert "1 new" in result.summary

    def test_detects_changed_files(self, tmp_path):
        cfg = _make_config(tmp_path)

        # Write initial note + index it
        note_path = _write_note(cfg.vault.path, "facts/Test.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [test]
            confidence: 0.8
            importance: 3
            stability: stable
            ---
            # Test Note
            Original content.
        """))

        # Run real index to create chunks.jsonl
        from cortex.indexer import index_vault
        index_vault(cfg)
        assert cfg.index.chunks_path.exists()

        # Modify the note
        note_path.write_text(textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [test]
            confidence: 0.8
            importance: 3
            stability: stable
            ---
            # Test Note
            Modified content!
        """), encoding="utf-8")

        result = _dry_run_probe(cfg)
        assert "1 changed" in result.summary

    def test_detects_removed_files(self, tmp_path):
        cfg = _make_config(tmp_path)

        note_path = _write_note(cfg.vault.path, "facts/Gone.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: []
            confidence: 0.5
            importance: 3
            stability: stable
            ---
            # Gone
            Will be deleted.
        """))

        from cortex.indexer import index_vault
        index_vault(cfg)

        # Delete the note
        note_path.unlink()

        result = _dry_run_probe(cfg)
        assert "1 removed" in result.summary

    def test_nothing_to_do(self, tmp_path):
        cfg = _make_config(tmp_path)

        _write_note(cfg.vault.path, "facts/Stable.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: []
            confidence: 0.5
            importance: 3
            stability: stable
            ---
            # Stable
            Unchanged.
        """))

        from cortex.indexer import index_vault
        index_vault(cfg)

        # Also create graph artifacts so they're not "missing"
        graph_dir = cfg.index.chunks_path.parent / "graph"
        graph_dir.mkdir(parents=True, exist_ok=True)
        (graph_dir / "graph_stats.json").write_text("{}")

        result = _dry_run_probe(cfg)
        assert result.skipped is True
        assert "nothing" in result.skip_reason.lower() or result.skip_reason == "nothing to do"


# ---- _run_index integration ------------------------------------------------


class TestRunIndex:
    def test_indexes_vault(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "facts/Alpha.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [test]
            confidence: 0.8
            importance: 3
            stability: stable
            ---
            # Alpha
            Alpha content.
        """))

        result = _run_index(cfg)
        assert result.ok
        assert "1" in result.summary  # indexed 1 file
        assert cfg.index.chunks_path.exists()

    def test_skips_unchanged(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "facts/Alpha.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [test]
            confidence: 0.8
            importance: 3
            stability: stable
            ---
            # Alpha
            Alpha content.
        """))

        # First run
        _run_index(cfg)
        # Second run — nothing changed
        result = _run_index(cfg)
        assert result.skipped is True
        assert "no files changed" in result.skip_reason


# ---- _run_graph_build integration ------------------------------------------


class TestRunGraphBuild:
    def test_builds_from_chunks(self, tmp_path):
        cfg = _make_config(tmp_path)

        # Write chunks.jsonl
        chunks = [
            {"id": "a.md#intro", "file": "A.md", "wikilinks": [], "tags": ["test"],
             "heading_path": [], "frontmatter": {}, "fm_normalized": {"type": "fact"}},
        ]
        cfg.index.chunks_path.parent.mkdir(parents=True, exist_ok=True)
        with cfg.index.chunks_path.open("w") as f:
            for c in chunks:
                f.write(json.dumps(c) + "\n")

        result = _run_graph_build(cfg)
        assert result.ok
        assert "node" in result.summary.lower() or "built" in result.summary.lower()

    def test_fails_without_chunks(self, tmp_path):
        cfg = _make_config(tmp_path)
        result = _run_graph_build(cfg)
        assert not result.ok
        assert "chunks.jsonl" in result.error


# ---- run_maintenance dry-run -----------------------------------------------


class TestRunMaintenanceDryRun:
    def test_dry_run_does_not_write(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "facts/New.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: []
            confidence: 0.5
            importance: 3
            stability: stable
            ---
            # New
            New content.
        """))

        report = run_maintenance(cfg, dry_run=True)
        assert report.dry_run is True
        assert report.ok is True
        # chunks.jsonl should NOT have been created
        assert not cfg.index.chunks_path.exists()

    def test_dry_run_reports_new_files(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "facts/New.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: []
            confidence: 0.5
            importance: 3
            stability: stable
            ---
            # New
            New content.
        """))

        report = run_maintenance(cfg, dry_run=True)
        assert "1 new" in report.steps[0].summary

    def test_dry_run_does_not_append_lifecycle_log(self, tmp_path):
        cfg = _make_config(tmp_path)
        log_path = cfg.vault.path / "log.md"
        log_path.write_text("# Log\n\nexisting\n", encoding="utf-8")
        before = log_path.read_bytes()

        report = run_maintenance(cfg, dry_run=True)

        assert report.ok is True
        assert log_path.read_bytes() == before

    def test_write_run_appends_lifecycle_log_after_final_status(self, tmp_path):
        cfg = _make_config(tmp_path)
        log_path = cfg.vault.path / "log.md"
        log_path.write_text("# Log\n\nexisting", encoding="utf-8")
        before = log_path.read_bytes()
        index_result = StepResult(name="index", summary="Indexed 1 file")
        embed_result = StepResult(name="embed", skipped=True, skip_reason="no chunks changed")
        graph_result = StepResult(name="graph_build", summary="Built graph")

        with patch("cortex.lifecycle._run_index", return_value=index_result), \
             patch("cortex.lifecycle._run_embed", return_value=embed_result), \
             patch("cortex.lifecycle._run_graph_build", return_value=graph_result):
            report = run_maintenance(cfg)

        text = log_path.read_text(encoding="utf-8")
        assert report.ok is True
        assert log_path.read_bytes().startswith(before + b"\n")
        assert "event=lifecycle.maintenance" in text
        assert "mode=write" in text
        assert "status=ok" in text
        assert "steps=3" in text


# ---- WeeklyReview (read-only) ----------------------------------------------


class TestWeeklyReviewReport:
    def test_summary_includes_all_sections(self):
        report = WeeklyReviewReport(
            graph_stats={"node_count": 3, "edge_count": 2},
            duplicate_groups=[{"kind": "title", "key": "alpha", "nodes": ["note:A.md", "note:B.md"]}],
            stale_high_importance=[{"node_id": "note:A.md", "importance": 5.0}],
            broken_references=[{"kind": "unresolved", "target_raw": "Ghost"}],
            consolidation_proposals=[{"parent": "note:Hub.md", "degree": 4, "neighbors": ["note:A.md"]}],
            orphan_nodes=[{"id": "note:Orphan.md"}],
            contradiction_clusters=[{"source": "note:A.md", "target": "note:B.md"}],
            dry_run=True,
        )
        text = report.summary()
        assert "[DRY RUN]" in text
        assert "duplicates: 1" in text
        assert "stale high-importance: 1" in text
        assert "broken references: 1" in text
        assert "consolidation proposals: 1" in text
        assert "orphans: 1" in text
        assert "contradictions: 1" in text

    def test_to_markdown_includes_detail_sections_and_runtime(self):
        report = WeeklyReviewReport(
            graph_stats={"node_count": 3, "edge_count": 2},
            duplicate_groups=[{"kind": "title", "key": "alpha", "nodes": ["note:A.md", "note:B.md"]}],
            stale_high_importance=[{"node_id": "note:A.md", "importance": 5.0, "last_verified": "2024-01-01"}],
            broken_references=[{"kind": "unresolved", "source_node": "note:A.md", "target_raw": "Ghost"}],
            consolidation_proposals=[{"parent": "note:Hub.md", "label": "Hub", "degree": 4, "neighbors": ["note:A.md"]}],
            orphan_nodes=[{"id": "note:Orphan.md", "label": "Orphan", "file": "Orphan.md"}],
            contradiction_clusters=[{"source": "note:A.md", "target": "note:B.md", "relation": "contradicts"}],
            dry_run=True,
            duration_seconds=1.25,
        )

        text = report.to_markdown()

        assert text.startswith("# [DRY RUN] Cortex Weekly Review")
        assert "## Summary" in text
        assert "## Graph Stats" in text
        assert "## Duplicates" in text
        assert "## Stale High-Importance Notes" in text
        assert "## Broken References" in text
        assert "## Consolidation Proposals" in text
        assert "## Orphan Nodes" in text
        assert "## Contradictions" in text
        assert "## Runtime" in text
        assert "note:A.md" in text
        assert "Ghost" in text
        assert "Duration: 1.2s" in text


class TestRunWeeklyReview:
    def test_consumes_graph_artifacts_and_reports_stats(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_graph_artifacts(
            cfg,
            nodes=[{"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"}],
            edges=[],
            stats={"node_count": 1, "edge_count": 0, "broken_count": 0},
        )

        report = run_weekly_review(cfg)

        assert report.ok is True
        assert report.graph_stats["node_count"] == 1
        assert report.graph_stats["edge_count"] == 0

    def test_deduplicates_by_title_alias_and_content_hash(self, tmp_path):
        cfg = _make_config(tmp_path)
        nodes = [
            {"id": "note:A.md", "type": "note", "label": "Same", "file": "A.md",
             "metadata": {"aliases": ["shared-alias"]}},
            {"id": "note:B.md", "type": "note", "label": "Same", "file": "B.md",
             "metadata": {}},
            {"id": "note:C.md", "type": "note", "label": "Unique C", "file": "C.md",
             "metadata": {"aliases": ["shared-alias"]}},
            {"id": "note:D.md", "type": "note", "label": "Unique D", "file": "D.md",
             "metadata": {}},
            {"id": "note:E.md", "type": "note", "label": "Unique E", "file": "E.md",
             "metadata": {}},
        ]
        _write_graph_artifacts(cfg, nodes=nodes, edges=[])
        _write_chunks(cfg, [
            {"id": "A.md#0", "file": "A.md", "content_hash": "hash-a"},
            {"id": "B.md#0", "file": "B.md", "content_hash": "hash-b"},
            {"id": "D.md#0", "file": "D.md", "content_hash": "hash-body"},
            {"id": "E.md#0", "file": "E.md", "content_hash": "hash-body"},
        ])

        report = run_weekly_review(cfg)
        groups = {(g["kind"], g["key"]): set(g["nodes"]) for g in report.duplicate_groups}

        assert groups[("title", "same")] == {"note:A.md", "note:B.md"}
        assert groups[("alias", "shared-alias")] == {"note:A.md", "note:C.md"}
        assert groups[("content_hash", "hash-body")] == {"note:D.md", "note:E.md"}

    def test_reports_only_stale_high_importance_notes(self, tmp_path):
        cfg = _make_config(tmp_path)
        nodes = [
            {"id": "note:Important.md", "type": "note", "label": "Important", "file": "Important.md",
             "metadata": {"last_verified": "2024-01-01", "importance": 5.0}},
            {"id": "note:Low.md", "type": "note", "label": "Low", "file": "Low.md",
             "metadata": {"last_verified": "2024-01-01", "importance": 2.0}},
            {"id": "note:Fresh.md", "type": "note", "label": "Fresh", "file": "Fresh.md",
             "metadata": {"last_verified": "2025-04-20", "importance": 5.0}},
        ]
        _write_graph_artifacts(cfg, nodes=nodes, edges=[])

        report = run_weekly_review(
            cfg,
            stale_days=180,
            stale_min_importance=4.0,
            reference_date=date(2025, 5, 1),
        )

        assert [s["node_id"] for s in report.stale_high_importance] == ["note:Important.md"]

    def test_reports_broken_references_orphans_and_contradictions(self, tmp_path):
        cfg = _make_config(tmp_path)
        nodes = [
            {"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"},
            {"id": "note:B.md", "type": "note", "label": "B", "file": "B.md"},
            {"id": "note:Orphan.md", "type": "note", "label": "Orphan", "file": "Orphan.md"},
        ]
        edges = [{"source": "note:A.md", "target": "note:B.md", "type": "contradicts"}]
        broken = [{"source_node": "note:A.md", "target_raw": "Ghost", "kind": "unresolved"}]
        _write_graph_artifacts(cfg, nodes=nodes, edges=edges, broken=broken)

        report = run_weekly_review(cfg)

        assert report.broken_references == broken
        assert [o["id"] for o in report.orphan_nodes] == ["note:Orphan.md"]
        assert report.contradiction_clusters[0]["source"] == "note:A.md"
        assert report.contradiction_clusters[0]["target"] == "note:B.md"

    def test_proposes_consolidation_for_high_degree_note_clusters(self, tmp_path):
        cfg = _make_config(tmp_path)
        nodes = [
            {"id": "note:Hub.md", "type": "note", "label": "Hub", "file": "Hub.md"},
            {"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"},
            {"id": "note:B.md", "type": "note", "label": "B", "file": "B.md"},
            {"id": "note:C.md", "type": "note", "label": "C", "file": "C.md"},
            {"id": "tag:x", "type": "tag", "label": "x"},
        ]
        edges = [
            {"source": "note:Hub.md", "target": "note:A.md", "type": "links_to"},
            {"source": "note:Hub.md", "target": "note:B.md", "type": "links_to"},
            {"source": "note:C.md", "target": "note:Hub.md", "type": "links_to"},
            {"source": "note:Hub.md", "target": "tag:x", "type": "tagged_with"},
        ]
        _write_graph_artifacts(cfg, nodes=nodes, edges=edges)

        report = run_weekly_review(cfg, consolidation_min_degree=3)

        assert report.consolidation_proposals == [
            {
                "parent": "note:Hub.md",
                "label": "Hub",
                "degree": 4,
                "note_neighbor_count": 3,
                "neighbors": ["note:A.md", "note:B.md", "note:C.md"],
            }
        ]

    def test_weekly_review_is_read_only(self, tmp_path):
        cfg = _make_config(tmp_path)
        (cfg.vault.path / "log.md").write_text("# Log\n\nexisting\n", encoding="utf-8")
        _write_graph_artifacts(
            cfg,
            nodes=[{"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"}],
            edges=[],
        )
        before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

        report = run_weekly_review(cfg, dry_run=True)

        after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
        assert report.dry_run is True
        assert after == before


# ---- NightlyPromotion ------------------------------------------------------

class TestNightlyPromotionReport:
    def test_summary_includes_sections(self):
        report = NightlyPromotionReport(
            dry_run=True,
            candidates=[{"file": "00_inbox/Raw.md", "target_file": "10_facts/Raw.md"}],
            promoted=[{"file": "10_facts/Raw.md"}],
            skipped_duplicates=[{"file": "00_inbox/Dupe.md"}],
            contradiction_blocks=[{"file": "00_inbox/Conflict.md"}],
            superseded=[{"file": "10_facts/Old.md"}],
        )
        text = report.summary()
        assert "[DRY RUN]" in text
        assert "candidates: 1" in text
        assert "promoted: 1" in text
        assert "duplicates skipped: 1" in text
        assert "contradiction blocks: 1" in text
        assert "superseded: 1" in text


class TestRunNightlyPromotion:
    def test_dry_run_finds_candidates_without_mutating_vault(self, tmp_path):
        cfg = _make_config(tmp_path)
        raw = _write_note(cfg.vault.path, "00_inbox/Raw Fact.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            source: session
            ---
            # Raw Fact
            Useful fact.
        """))
        before = raw.read_text(encoding="utf-8")

        report = run_nightly_promotion(cfg, dry_run=True, reference_date=date(2026, 5, 3))

        assert report.ok is True
        assert report.dry_run is True
        assert report.candidates[0]["file"] == "00_inbox/Raw Fact.md"
        assert report.candidates[0]["target_file"] == "10_facts/Raw Fact.md"
        assert raw.read_text(encoding="utf-8") == before
        assert not (cfg.vault.path / "10_facts/Raw Fact.md").exists()

    def test_write_run_appends_log_without_rewriting_history_or_raw_sources(self, tmp_path):
        cfg = _make_config(tmp_path)
        log_path = cfg.vault.path / "log.md"
        log_path.write_text("# Log\n\nexisting entry\n", encoding="utf-8")
        before_log = log_path.read_bytes()
        raw_source = _write_note(cfg.vault.path, "raw/articles/source.md", "RAW SOURCE\n")
        before_raw = raw_source.read_text(encoding="utf-8")
        _write_note(cfg.vault.path, "00_inbox/Raw Fact.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            source: session
            ---
            # Raw Fact
            Useful fact.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        text = log_path.read_text(encoding="utf-8")
        assert report.ok is True
        assert log_path.read_bytes().startswith(before_log)
        assert raw_source.read_text(encoding="utf-8") == before_raw
        assert "event=lifecycle.nightly" in text
        assert "mode=write" in text
        assert "status=ok" in text
        assert "candidates=1" in text
        assert "promoted=1" in text
        assert "10_facts/Raw Fact.md" in text
        assert "Useful fact." not in text

    def test_missing_log_is_not_created_by_write_run(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "00_inbox/Raw Fact.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            source: session
            ---
            # Raw Fact
            Useful fact.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        assert report.ok is True
        assert [p["file"] for p in report.promoted] == ["10_facts/Raw Fact.md"]
        assert not (cfg.vault.path / "log.md").exists()

    def test_promotes_candidate_with_normalized_frontmatter_and_derived_from(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "00_inbox/Raw Fact.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: memory
            aliases: [Useful Fact]
            confidence: high
            importance: high
            stability: stable
            source: session
            ---
            # Raw Fact
            Useful fact.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        promoted = cfg.vault.path / "10_facts/Raw Fact.md"
        assert report.ok is True
        assert [p["file"] for p in report.promoted] == ["10_facts/Raw Fact.md"]
        text = promoted.read_text(encoding="utf-8")
        assert "type: fact" in text
        assert "status: active" in text
        assert "created: '2026-05-03'" in text or "created: 2026-05-03" in text
        assert "updated: '2026-05-03'" in text or "updated: 2026-05-03" in text
        assert "last_verified: '2026-05-03'" in text or "last_verified: 2026-05-03" in text
        assert "tags:" in text and "- memory" in text
        assert "aliases:" in text and "- Useful Fact" in text
        assert "derived_from:" in text and "'[[Raw Fact]]'" in text
        assert "promote:" not in text
        assert "promote_type:" not in text


    def test_promoted_source_is_archived_without_promotion_flags(self, tmp_path):
        cfg = _make_config(tmp_path)
        source = _write_note(cfg.vault.path, "00_inbox/Raw Fact.md", textwrap.dedent("""\
            ---
            promote: true
            cortex_promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            source: session
            review_status: pending
            review_reason: "needs review before promotion"
            ---
            # Raw Fact
            Useful fact.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        source_text = source.read_text(encoding="utf-8")
        assert report.ok is True
        assert [p["file"] for p in report.promoted] == ["10_facts/Raw Fact.md"]
        assert "status: archived" in source_text
        assert "promoted_to: '[[Raw Fact]]'" in source_text or 'promoted_to: "[[Raw Fact]]"' in source_text
        assert "archived_reason: promoted" in source_text
        assert "updated: '2026-05-03'" in source_text or "updated: 2026-05-03" in source_text
        assert "promote:" not in source_text
        assert "cortex_promote:" not in source_text
        assert "promote_type:" not in source_text
        assert "review_status:" not in source_text
        assert "review_reason:" not in source_text

        promoted_text = (cfg.vault.path / "10_facts/Raw Fact.md").read_text(encoding="utf-8")
        assert "review_status:" not in promoted_text
        assert "review_reason:" not in promoted_text

    def test_skips_archived_candidate_even_with_promotion_flags(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "00_inbox/Already Promoted.md", textwrap.dedent("""\
            ---
            status: archived
            promote: true
            promote_type: fact
            promoted_to: '[[Already Promoted]]'
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            ---
            # Already Promoted
            Historical source note.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        assert report.ok is True
        assert report.candidates == []
        assert report.promoted == []
        assert not (cfg.vault.path / "10_facts/Already Promoted.md").exists()

    def test_missing_or_invalid_frontmatter_is_not_promotable(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "00_inbox/No Frontmatter.md", "# No Frontmatter\nText.\n")
        _write_note(cfg.vault.path, "00_inbox/Broken Frontmatter.md", textwrap.dedent("""\
            ---
            promote: [unterminated
            ---
            # Broken
            Text.
        """))

        report = run_nightly_promotion(cfg, dry_run=True, reference_date=date(2026, 5, 3))

        assert report.ok is True
        assert report.candidates == []

    def test_skips_duplicate_canonical_fact(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "10_facts/Existing.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [memory]
            aliases: [Useful Fact]
            confidence: 0.9
            importance: 5
            stability: stable
            ---
            # Existing
            Same meaning.
        """))
        _write_note(cfg.vault.path, "00_inbox/Raw.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            aliases: [Useful Fact]
            confidence: high
            importance: high
            stability: stable
            ---
            # Raw
            Candidate.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        assert report.promoted == []
        assert report.skipped_duplicates[0]["file"] == "00_inbox/Raw.md"
        assert "Useful Fact" in report.skipped_duplicates[0]["reason"]
        assert not (cfg.vault.path / "10_facts/Raw.md").exists()

    def test_blocks_candidate_with_contradiction_edge(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "00_inbox/Conflict.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            ---
            # Conflict
            Candidate.
        """))
        _write_graph_artifacts(
            cfg,
            nodes=[
                {"id": "note:00_inbox/Conflict.md", "type": "note", "label": "Conflict", "file": "00_inbox/Conflict.md"},
                {"id": "note:10_facts/Existing.md", "type": "note", "label": "Existing", "file": "10_facts/Existing.md"},
            ],
            edges=[{"source": "note:00_inbox/Conflict.md", "target": "note:10_facts/Existing.md", "type": "contradicts"}],
        )

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        assert report.promoted == []
        assert report.contradiction_blocks[0]["file"] == "00_inbox/Conflict.md"
        assert not (cfg.vault.path / "10_facts/Conflict.md").exists()

    def test_supersedes_updates_both_notes_atomically(self, tmp_path):
        cfg = _make_config(tmp_path)
        _write_note(cfg.vault.path, "10_facts/Old Fact.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [memory]
            confidence: 0.6
            importance: 3
            stability: evolving
            ---
            # Old Fact
            Old content.
        """))
        _write_note(cfg.vault.path, "00_inbox/New Fact.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            supersedes: ['[[Old Fact]]']
            ---
            # New Fact
            Better content.
        """))

        report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        new_text = (cfg.vault.path / "10_facts/New Fact.md").read_text(encoding="utf-8")
        old_text = (cfg.vault.path / "10_facts/Old Fact.md").read_text(encoding="utf-8")
        assert report.promoted[0]["file"] == "10_facts/New Fact.md"
        assert report.superseded == [{"file": "10_facts/Old Fact.md", "superseded_by": "[[New Fact]]"}]
        assert "supersedes:" in new_text and "'[[Old Fact]]'" in new_text
        assert "status: superseded" in old_text
        assert "superseded_by:" in old_text and "'[[New Fact]]'" in old_text


# ---- CLI tests -------------------------------------------------------------


class TestCLILifecycle:
    def test_lifecycle_no_subcommand(self, capsys):
        from cortex.cli import main
        ret = main(["lifecycle"])
        assert ret == 2
        err = capsys.readouterr().err
        assert "maintenance" in err.lower()

    def test_lifecycle_nightly_runs_promotion_dry_run(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = _make_cli_config(tmp_path)
        vault = tmp_path / "vault"
        _write_note(vault, "00_inbox/Raw.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            ---
            # Raw
            Candidate.
        """))
        ret = main(["lifecycle", "--config", cfg_path, "nightly", "--dry-run"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Nightly promotion report" in out
        assert "candidates: 1" in out

    def test_lifecycle_weekly_runs_review(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = _make_cli_config(tmp_path)
        cfg = _make_config(tmp_path)
        _write_graph_artifacts(
            cfg,
            nodes=[{"id": "note:A.md", "type": "note", "label": "A", "file": "A.md"}],
            edges=[],
            stats={"node_count": 1, "edge_count": 0, "broken_count": 0},
        )
        ret = main(["lifecycle", "--config", cfg_path, "weekly", "--dry-run"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "Weekly review report" in out
        assert "1 nodes" in out

    def test_lifecycle_maintenance_dry_run(self, tmp_path, capsys):
        from cortex.cli import main
        cfg_path = _make_cli_config(tmp_path)
        ret = main(["lifecycle", "--config", cfg_path, "maintenance", "--dry-run"])
        assert ret == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_lifecycle_maintenance_with_vault(self, tmp_path, capsys):
        """Integration: maintenance on a mini vault (mocked embed to avoid deps)."""
        from cortex.cli import main
        cfg_path = _make_cli_config(tmp_path)

        vault = tmp_path / "vault"
        _write_note(vault, "facts/Test.md", textwrap.dedent("""\
            ---
            type: fact
            status: active
            tags: [test]
            confidence: 0.8
            importance: 3
            stability: stable
            ---
            # Test
            Content.
        """))

        # Mock embed_chunks to avoid sentence-transformers dependency
        mock_embed_result = StepResult(name="embed", summary="Mocked embed")
        with patch("cortex.lifecycle._run_embed", return_value=mock_embed_result):
            ret = main(["lifecycle", "--config", cfg_path, "maintenance"])

        assert ret == 0
        out = capsys.readouterr().out
        assert "index" in out.lower()
        assert "graph_build" in out.lower() or "graph" in out.lower()


class TestLifecycleLogAppendNonFatal:
    """Tests for non-fatal lifecycle log append behavior."""

    def test_unwritable_log_does_not_abort_maintenance(self, tmp_path) -> None:
        cfg = _make_config(tmp_path)
        log_path = cfg.vault.path / "log.md"
        log_path.write_text("# Log\n\nexisting\n", encoding="utf-8")
        before = log_path.read_bytes()

        # Force _append_lifecycle_log's open call to raise OSError.
        import cortex.lifecycle as lifecycle_mod

        original_open = lifecycle_mod.Path.open

        def patched_open(self, *args, **kwargs):
            if self == log_path:
                raise OSError("simulated write failure")
            return original_open(self, *args, **kwargs)

        index_result = StepResult(name="index", summary="Indexed 1 file")
        embed_result = StepResult(name="embed", skipped=True, skip_reason="no chunks changed")
        graph_result = StepResult(name="graph_build", summary="Built graph")

        with patch("cortex.lifecycle._run_index", return_value=index_result), \
             patch("cortex.lifecycle._run_embed", return_value=embed_result), \
             patch("cortex.lifecycle._run_graph_build", return_value=graph_result), \
             patch.object(lifecycle_mod.Path, "open", patched_open):
            report = run_maintenance(cfg)

        assert report.ok is True
        assert log_path.read_bytes() == before

    def test_log_as_directory_does_not_abort_maintenance(self, tmp_path) -> None:
        cfg = _make_config(tmp_path)
        log_dir = cfg.vault.path / "log.md"
        log_dir.mkdir()

        index_result = StepResult(name="index", summary="Indexed 1 file")
        embed_result = StepResult(name="embed", skipped=True, skip_reason="no chunks changed")
        graph_result = StepResult(name="graph_build", summary="Built graph")

        with patch("cortex.lifecycle._run_index", return_value=index_result), \
             patch("cortex.lifecycle._run_embed", return_value=embed_result), \
             patch("cortex.lifecycle._run_graph_build", return_value=graph_result):
            report = run_maintenance(cfg)

        assert report.ok is True
        assert log_dir.is_dir()

    def test_unwritable_log_does_not_abort_nightly_promotion(self, tmp_path) -> None:
        cfg = _make_config(tmp_path)
        log_path = cfg.vault.path / "log.md"
        log_path.write_text("# Log\n\nexisting\n", encoding="utf-8")
        before = log_path.read_bytes()
        _write_note(cfg.vault.path, "00_inbox/Raw Fact.md", textwrap.dedent("""\
            ---
            promote: true
            promote_type: fact
            tags: [memory]
            confidence: high
            importance: high
            stability: stable
            source: session
            ---
            # Raw Fact
            Useful fact.
        """))

        # Patch _append_lifecycle_log to simulate a failed-but-caught OSError
        # append (returns False). The real try/except is tested directly in
        # test_append_lifecycle_log_catches_oserror below.
        with patch("cortex.lifecycle._append_lifecycle_log", return_value=False):
            report = run_nightly_promotion(cfg, dry_run=False, reference_date=date(2026, 5, 3))

        assert report.ok is True
        assert log_path.read_bytes() == before
        assert [p["file"] for p in report.promoted] == ["10_facts/Raw Fact.md"]

    def test_append_lifecycle_log_catches_oserror_and_returns_false(self, tmp_path) -> None:
        """Directly test that _append_lifecycle_log catches OSError from open and
        returns False instead of raising.
        """
        from cortex.lifecycle import LifecycleLogEvent, _append_lifecycle_log

        cfg = _make_config(tmp_path)
        log_path = cfg.vault.path / "log.md"
        log_path.write_text("# Log\n\nexisting\n", encoding="utf-8")

        import cortex.lifecycle as lifecycle_mod

        def raise_oserror(*args, **kwargs):
            raise OSError("simulated write failure")

        event = LifecycleLogEvent(event="test", mode="write", status="ok")

        with patch.object(lifecycle_mod.Path, "open", raise_oserror):
            result = _append_lifecycle_log(cfg, event)

        assert result is False

    def test_missing_log_is_not_created_by_maintenance_write(self, tmp_path) -> None:
        cfg = _make_config(tmp_path)

        index_result = StepResult(name="index", summary="Indexed 1 file")
        embed_result = StepResult(name="embed", skipped=True, skip_reason="no chunks changed")
        graph_result = StepResult(name="graph_build", summary="Built graph")

        with patch("cortex.lifecycle._run_index", return_value=index_result), \
             patch("cortex.lifecycle._run_embed", return_value=embed_result), \
             patch("cortex.lifecycle._run_graph_build", return_value=graph_result):
            report = run_maintenance(cfg)

        assert report.ok is True
        assert not (cfg.vault.path / "log.md").exists()
