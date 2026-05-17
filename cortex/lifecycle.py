"""Lifecycle plugins for hermes-cortex — Phase 6.

Provides automated maintenance, review, and promotion workflows that
operate on the indexed vault and its graph artifacts.

Phase 6 / Slice 1: IndexMaintenance + shared infrastructure.
Phase 6 / Slice 2: WeeklyReview (read-only). [stub]
Phase 6 / Slice 3: NightlyPromotion (write path). [stub]

Design constraints:
  - All write operations must be atomic
  - All commands must support --dry-run
  - No new dependencies beyond existing cortex modules
  - Maintenance must run index → embed → graph in strict order
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from cortex.config import Config

log = logging.getLogger("cortex.lifecycle")


# ---- Shared infrastructure -------------------------------------------------


@dataclass
class StepResult:
    """Result of a single lifecycle step."""

    name: str
    skipped: bool = False       # True if the step had nothing to do
    skip_reason: str = ""
    error: str = ""
    summary: str = ""
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class MaintenanceReport:
    """Report from a full maintenance run."""

    steps: list[StepResult] = field(default_factory=list)
    dry_run: bool = False
    total_duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    @property
    def errors(self) -> list[StepResult]:
        return [s for s in self.steps if not s.ok]

    def summary(self) -> str:
        prefix = "[DRY RUN] " if self.dry_run else ""
        lines = [f"{prefix}Maintenance report:"]
        for s in self.steps:
            status = "SKIP" if s.skipped else ("FAIL" if s.error else " OK ")
            lines.append(f"  [{status}] {s.name} ({s.duration_seconds:.1f}s)")
            if s.summary:
                lines.append(f"         {s.summary}")
            if s.error:
                lines.append(f"         ERROR: {s.error}")
            if s.skipped and s.skip_reason:
                lines.append(f"         Reason: {s.skip_reason}")
        lines.append(f"  Total: {self.total_duration_seconds:.1f}s")
        return "\n".join(lines)


# ---- IndexMaintenance ------------------------------------------------------


def _run_index(cfg: Config, *, force: bool = False) -> StepResult:
    """Run the indexing step. Returns a StepResult."""
    from cortex.indexer import index_vault

    t0 = time.monotonic()
    try:
        report = index_vault(cfg, force=force)
    except Exception as e:
        return StepResult(
            name="index",
            error=str(e),
            duration_seconds=time.monotonic() - t0,
        )

    changed = report.indexed_files > 0 or report.removed_files > 0
    return StepResult(
        name="index",
        skipped=not changed and not force,
        skip_reason="no files changed" if not changed and not force else "",
        summary=report.summary(),
        duration_seconds=time.monotonic() - t0,
    )


def _run_embed(cfg: Config, *, force: bool = False, skip_if_no_changes: bool = False) -> StepResult:
    """Run the embedding step. Returns a StepResult.

    If ``skip_if_no_changes`` is True and the previous index step reported
    no changes, we still run embed (it's incremental and cheap) but we
    note in the result whether anything was actually embedded.
    """
    from cortex.embedder import ModelMismatchError, embed_chunks

    t0 = time.monotonic()
    try:
        report = embed_chunks(cfg, force=force)
    except ModelMismatchError as e:
        return StepResult(
            name="embed",
            error=f"Model mismatch: {e}. Run `cortex reset --chroma` and re-embed.",
            duration_seconds=time.monotonic() - t0,
        )
    except Exception as e:
        return StepResult(
            name="embed",
            error=str(e),
            duration_seconds=time.monotonic() - t0,
        )

    changed = report.chunks_embedded > 0 or report.chunks_removed > 0
    return StepResult(
        name="embed",
        skipped=not changed and not force,
        skip_reason="no chunks changed" if not changed and not force else "",
        summary=report.summary(),
        duration_seconds=time.monotonic() - t0,
    )


def _run_graph_build(cfg: Config, *, force: bool = False) -> StepResult:
    """Run the graph build step. Returns a StepResult."""
    from cortex.graph_index import build_graph

    t0 = time.monotonic()
    try:
        report = build_graph(cfg, force=force)
    except FileNotFoundError as e:
        return StepResult(
            name="graph_build",
            error=str(e),
            duration_seconds=time.monotonic() - t0,
        )
    except Exception as e:
        return StepResult(
            name="graph_build",
            error=str(e),
            duration_seconds=time.monotonic() - t0,
        )

    return StepResult(
        name="graph_build",
        summary=report.summary(),
        duration_seconds=time.monotonic() - t0,
    )


def run_maintenance(
    cfg: Config,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> MaintenanceReport:
    """Run the full maintenance pipeline: index → embed → graph build.

    Executes steps in strict order. Each step is incremental by default —
    unchanged content is skipped. ``force=True`` re-processes everything.

    ``dry_run=True`` reports what *would* happen without writing anything.
    In dry-run mode, only the index step runs (to detect changes), but
    its output is not persisted and embed/graph are reported as skipped.

    Args:
        cfg: Loaded cortex config.
        force: If True, force-rebuild all steps.
        dry_run: If True, don't actually write anything.

    Returns:
        MaintenanceReport with per-step results.
    """
    report = MaintenanceReport(dry_run=dry_run)
    t0 = time.monotonic()

    if dry_run:
        # Dry-run: probe what would change without writing
        report.steps.append(_dry_run_probe(cfg))
        report.total_duration_seconds = time.monotonic() - t0
        return report

    # Step 1: Index
    log.info("Maintenance: starting index step")
    index_result = _run_index(cfg, force=force)
    report.steps.append(index_result)
    if not index_result.ok:
        report.total_duration_seconds = time.monotonic() - t0
        return report  # abort on error

    # Step 2: Embed
    log.info("Maintenance: starting embed step")
    embed_result = _run_embed(cfg, force=force)
    report.steps.append(embed_result)
    if not embed_result.ok:
        report.total_duration_seconds = time.monotonic() - t0
        return report  # abort on error

    # Step 3: Graph build (always runs — it's fast and reads chunks.jsonl)
    log.info("Maintenance: starting graph build step")
    graph_result = _run_graph_build(cfg, force=force)
    report.steps.append(graph_result)

    report.total_duration_seconds = time.monotonic() - t0
    log.info("Maintenance complete in %.1fs", report.total_duration_seconds)
    return report


def _dry_run_probe(cfg: Config) -> StepResult:
    """Probe what a maintenance run would do without writing.

    Checks:
      - How many vault files have changed (by comparing file hashes
        against chunks.jsonl)
      - Whether chunks.jsonl exists
      - Whether graph artifacts exist
    """
    import hashlib
    from cortex.indexer import _normalize_newlines, iter_vault_files, load_existing_chunks

    t0 = time.monotonic()
    try:
        chunks_path = cfg.index.chunks_path
        existing_chunks = load_existing_chunks(chunks_path) if chunks_path.exists() else []

        # Build file → hash map from existing chunks
        existing_hashes: dict[str, str] = {}
        for c in existing_chunks:
            f_ = c.get("file")
            h = c.get("content_hash")
            if f_ and h:
                existing_hashes[f_] = h

        # Scan vault files
        changed = 0
        new = 0
        unchanged = 0
        vault_files: set[str] = set()

        for md_path in iter_vault_files(cfg):
            rel = md_path.relative_to(cfg.vault.path).as_posix()
            vault_files.add(rel)
            raw = _normalize_newlines(md_path.read_text(encoding="utf-8"))
            current_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

            if rel not in existing_hashes:
                new += 1
            elif existing_hashes[rel] != current_hash:
                changed += 1
            else:
                unchanged += 1

        # Files in chunks but no longer in vault
        existing_files = set(existing_hashes.keys())
        removed = len(existing_files - vault_files)

        # Graph artifacts present?
        graph_dir = cfg.index.chunks_path.parent / "graph"
        graph_exists = (graph_dir / "graph_stats.json").exists()

        parts = []
        if new:
            parts.append(f"{new} new")
        if changed:
            parts.append(f"{changed} changed")
        if removed:
            parts.append(f"{removed} removed")
        if unchanged:
            parts.append(f"{unchanged} unchanged")
        if not chunks_path.exists():
            parts.append("chunks.jsonl missing")
        if not graph_exists:
            parts.append("graph artifacts missing")

        needs_work = new > 0 or changed > 0 or removed > 0 or not chunks_path.exists() or not graph_exists

        return StepResult(
            name="dry_run_probe",
            skipped=not needs_work,
            skip_reason="nothing to do" if not needs_work else "",
            summary=f"Would process: {', '.join(parts) or 'nothing'}",
            duration_seconds=time.monotonic() - t0,
        )
    except Exception as e:
        return StepResult(
            name="dry_run_probe",
            error=str(e),
            duration_seconds=time.monotonic() - t0,
        )


# ---- NightlyPromotion ------------------------------------------------------

_TYPE_FOLDERS = {
    "fact": "10_facts",
    "decision": "20_decisions",
    "project": "30_projects",
    "runbook": "40_runbooks",
}
_PROMOTION_INTERNAL_KEYS = {"promote", "cortex_promote", "promote_type", "review_status", "review_reason"}
_ARCHIVED_STATUSES = {"archived"}


@dataclass
class NightlyPromotionReport:
    """Report from the nightly promotion workflow.

    ``dry_run=True`` only proposes candidates. Non-dry-run writes canonical
    notes and source/superseded note updates via atomic temp-file replacement.
    """

    candidates: list[dict[str, Any]] = field(default_factory=list)
    promoted: list[dict[str, Any]] = field(default_factory=list)
    skipped_duplicates: list[dict[str, Any]] = field(default_factory=list)
    contradiction_blocks: list[dict[str, Any]] = field(default_factory=list)
    superseded: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    error: str = ""
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        prefix = "[DRY RUN] " if self.dry_run else ""
        lines = [
            f"{prefix}Nightly promotion report:",
            f"  candidates: {len(self.candidates)}",
            f"  promoted: {len(self.promoted)}",
            f"  duplicates skipped: {len(self.skipped_duplicates)}",
            f"  contradiction blocks: {len(self.contradiction_blocks)}",
            f"  superseded: {len(self.superseded)}",
        ]
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        lines.append(f"  Total: {self.duration_seconds:.1f}s")
        return "\n".join(lines)


def run_nightly_promotion(
    cfg: Config,
    *,
    dry_run: bool = True,
    reference_date: date | None = None,
) -> NightlyPromotionReport:
    """Run the Phase 6 nightly promotion workflow.

    Candidate contract: markdown notes with ``promote: true`` or
    ``cortex_promote: true`` in frontmatter are live promotion candidates, unless
    their source note is already archived. Notes are copied into the canonical
    folder for their target type; canonical frontmatter is normalized; provenance
    is preserved with a ``derived_from`` wikilink. Contradiction edges block
    promotion before any write. Existing canonical title/alias collisions are
    treated as duplicates. After promotion, source notes are archived and stripped
    of promotion-internal keys so they cannot remain eligible.
    """
    t0 = time.monotonic()
    today = (reference_date or date.today()).isoformat()
    report = NightlyPromotionReport(dry_run=dry_run)
    try:
        candidates = _find_promotion_candidates(cfg, today)
        report.candidates = [_candidate_summary(c) for c in candidates]
        canonical_index = _build_canonical_identity_index(cfg, exclude_files={c["file"] for c in candidates})
        contradiction_blocks = _load_contradiction_blocks(cfg)

        for candidate in candidates:
            if candidate["node_id"] in contradiction_blocks:
                report.contradiction_blocks.append({
                    "file": candidate["file"],
                    "target_file": candidate["target_file"],
                    "reason": contradiction_blocks[candidate["node_id"]],
                })
                continue

            duplicate_reason = _duplicate_reason(candidate, canonical_index)
            if duplicate_reason:
                report.skipped_duplicates.append({
                    "file": candidate["file"],
                    "target_file": candidate["target_file"],
                    "reason": duplicate_reason,
                })
                continue

            if dry_run:
                continue

            _write_promoted_candidate(cfg, candidate, today=today)
            report.promoted.append({"file": candidate["target_file"], "derived_from": candidate["derived_from"]})
            for item in _apply_supersedes(cfg, candidate, today):
                report.superseded.append(item)

            canonical_index["titles"].add(candidate["title"].casefold())
            canonical_index["files"].add(candidate["target_file"])
            for alias in candidate["aliases"]:
                canonical_index["aliases"].add(alias.casefold())
    except Exception as e:
        report.error = str(e)
    finally:
        report.duration_seconds = time.monotonic() - t0
    return report


def _find_promotion_candidates(cfg: Config, today: str) -> list[dict[str, Any]]:
    from cortex.indexer import _normalize_newlines, parse_frontmatter
    from cortex.frontmatter import normalize as normalize_frontmatter

    candidates: list[dict[str, Any]] = []
    for path in sorted(cfg.vault.path.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(cfg.vault.path).parts):
            continue
        raw_text = _normalize_newlines(path.read_text(encoding="utf-8"))
        fm, body = parse_frontmatter(raw_text)
        if not (fm.get("promote") is True or fm.get("cortex_promote") is True):
            continue
        rel = path.relative_to(cfg.vault.path).as_posix()
        if str(fm.get("status") or "").strip().casefold() in _ARCHIVED_STATUSES:
            log.info("Skipping archived promotion candidate: %s", rel)
            continue
        target_type = str(fm.get("promote_type") or fm.get("type") or "fact").strip() or "fact"
        folder = _TYPE_FOLDERS.get(target_type, "10_facts")
        target_rel = f"{folder}/{path.name}"
        title = path.stem
        aliases = _as_str_list(fm.get("aliases"))
        normalized = normalize_frontmatter({**fm, "type": target_type, "status": "active"})
        canonical_fm = dict(normalized.raw)
        for key in _PROMOTION_INTERNAL_KEYS:
            canonical_fm.pop(key, None)
        canonical_fm.update({
            "type": target_type,
            "status": "active",
            "created": canonical_fm.get("created") or today,
            "updated": today,
            "last_verified": canonical_fm.get("last_verified") or today,
            "tags": normalized.tags,
            "aliases": aliases,
            "confidence": normalized.confidence,
            "importance": normalized.importance,
            "stability": normalized.stability or "stable",
            "source": canonical_fm.get("source") or "session",
            "derived_from": _merge_unique_wikilinks(canonical_fm.get("derived_from"), [f"[[{path.stem}]]"]),
        })
        supersedes = _as_str_list(canonical_fm.get("supersedes"))
        if supersedes:
            canonical_fm["supersedes"] = supersedes
        candidates.append({
            "path": path,
            "file": rel,
            "node_id": f"note:{rel}",
            "target_path": cfg.vault.path / target_rel,
            "target_file": target_rel,
            "title": title,
            "aliases": aliases,
            "body": body,
            "frontmatter": canonical_fm,
            "derived_from": f"[[{path.stem}]]",
            "supersedes": supersedes,
        })
    return candidates


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": candidate["file"],
        "target_file": candidate["target_file"],
        "type": candidate["frontmatter"].get("type", "fact"),
        "derived_from": candidate["derived_from"],
    }


def _build_canonical_identity_index(cfg: Config, *, exclude_files: set[str]) -> dict[str, set[str]]:
    from cortex.indexer import _normalize_newlines, parse_frontmatter

    out = {"titles": set(), "aliases": set(), "files": set()}
    for path in sorted(cfg.vault.path.rglob("*.md")):
        rel = path.relative_to(cfg.vault.path).as_posix()
        if rel in exclude_files or rel.startswith("00_inbox/"):
            continue
        fm, _ = parse_frontmatter(_normalize_newlines(path.read_text(encoding="utf-8")))
        out["files"].add(rel)
        out["titles"].add(path.stem.casefold())
        for alias in _as_str_list(fm.get("aliases")):
            out["aliases"].add(alias.casefold())
    return out


def _load_contradiction_blocks(cfg: Config) -> dict[str, str]:
    try:
        from cortex.graph_diagnostics import GraphArtifacts
        artifacts = GraphArtifacts.load(cfg)
    except Exception:
        return {}

    blocked: dict[str, str] = {}
    for edge in artifacts.edges:
        if edge.get("type") != "contradicts":
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source:
            blocked[source] = f"contradicts {target}"
        if target:
            blocked[target] = f"contradicted by {source}"
    return blocked


def _duplicate_reason(candidate: dict[str, Any], index: dict[str, set[str]]) -> str:
    if candidate["target_file"] in index["files"]:
        return f"target file already exists: {candidate['target_file']}"
    if candidate["title"].casefold() in index["titles"]:
        return f"title already exists: {candidate['title']}"
    for alias in candidate["aliases"]:
        if alias.casefold() in index["aliases"]:
            return f"alias already exists: {alias}"
    return ""


def _write_promoted_candidate(cfg: Config, candidate: dict[str, Any], *, today: str) -> None:
    text = _render_markdown(candidate["frontmatter"], candidate["body"])
    _atomic_write_text(candidate["target_path"], text)

    _archive_promoted_source_note(
        candidate["path"],
        promoted_to=f"[[{Path(candidate['target_file']).stem}]]",
        today=today,
    )


def _archive_promoted_source_note(path: Path, *, promoted_to: str, today: str) -> None:
    """Archive a source candidate and remove active promotion eligibility.

    The lifecycle contract treats promotion and review flags as work-queue
    metadata. Once a source note has been promoted, those flags must disappear
    from the archived source so later vault-wide scans cannot promote it again
    and archived notes cannot still look like pending human-review queue items.
    """
    from cortex.indexer import _normalize_newlines, parse_frontmatter

    text = _normalize_newlines(path.read_text(encoding="utf-8"))
    fm, body = parse_frontmatter(text)
    archived_fm = dict(fm)
    for key in _PROMOTION_INTERNAL_KEYS:
        archived_fm.pop(key, None)
    archived_fm.update({
        "status": "archived",
        "updated": today,
        "promoted_to": promoted_to,
        "archived_reason": "promoted",
    })
    _atomic_write_text(path, _render_markdown(archived_fm, body))


def _apply_supersedes(cfg: Config, candidate: dict[str, Any], today: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not candidate["supersedes"]:
        return out
    title_to_path = {p.stem.casefold(): p for p in cfg.vault.path.rglob("*.md")}
    new_link = f"[[{Path(candidate['target_file']).stem}]]"
    for ref in candidate["supersedes"]:
        title = _wikilink_title(ref).casefold()
        old_path = title_to_path.get(title)
        if not old_path:
            continue
        _patch_note_frontmatter(old_path, {
            "status": "superseded",
            "updated": today,
            "superseded_by": [new_link],
        })
        out.append({
            "file": old_path.relative_to(cfg.vault.path).as_posix(),
            "superseded_by": new_link,
        })
    return out


def _patch_note_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    from cortex.indexer import _normalize_newlines, parse_frontmatter

    text = _normalize_newlines(path.read_text(encoding="utf-8"))
    fm, body = parse_frontmatter(text)
    merged = dict(fm)
    for key, value in updates.items():
        if isinstance(value, list):
            merged[key] = _merge_unique_wikilinks(merged.get(key), value)
        else:
            merged[key] = value
    _atomic_write_text(path, _render_markdown(merged, body))


def _render_markdown(fm: dict[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_text}\n---\n{body.lstrip()}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        if not text.endswith("\n"):
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _as_str_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        raw = [v.strip() for v in value.split(",")] if "," in value else [value]
    else:
        raw = [value]
    seen: dict[str, None] = {}
    for item in raw:
        text = str(item).strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


def _merge_unique_wikilinks(existing: Any, additions: list[str]) -> list[str]:
    values = _as_str_list(existing) + additions
    seen: dict[str, None] = {}
    for value in values:
        if value and value not in seen:
            seen[value] = None
    return list(seen)


def _wikilink_title(ref: str) -> str:
    text = str(ref).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    if "|" in text:
        text = text.split("|", 1)[0]
    return Path(text).stem


# ---- WeeklyReview ----------------------------------------------------------


@dataclass
class WeeklyReviewReport:
    """Read-only lifecycle review over graph artifacts.

    Weekly review intentionally proposes work; it never mutates the vault,
    chunks index, embeddings, graph artifacts, or generated viewer files.
    """

    graph_stats: dict[str, Any] = field(default_factory=dict)
    duplicate_groups: list[dict[str, Any]] = field(default_factory=list)
    stale_high_importance: list[dict[str, Any]] = field(default_factory=list)
    broken_references: list[dict[str, Any]] = field(default_factory=list)
    consolidation_proposals: list[dict[str, Any]] = field(default_factory=list)
    orphan_nodes: list[dict[str, Any]] = field(default_factory=list)
    contradiction_clusters: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    error: str = ""
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    def summary(self) -> str:
        prefix = "[DRY RUN] " if self.dry_run else ""
        lines = [
            f"{prefix}Weekly review report:",
            f"  graph: {self.graph_stats.get('node_count', 0)} nodes, "
            f"{self.graph_stats.get('edge_count', 0)} edges",
            f"  duplicates: {len(self.duplicate_groups)}",
            f"  stale high-importance: {len(self.stale_high_importance)}",
            f"  broken references: {len(self.broken_references)}",
            f"  consolidation proposals: {len(self.consolidation_proposals)}",
            f"  orphans: {len(self.orphan_nodes)}",
            f"  contradictions: {len(self.contradiction_clusters)}",
        ]
        if self.error:
            lines.append(f"  ERROR: {self.error}")
        lines.append(f"  Total: {self.duration_seconds:.1f}s")
        return "\n".join(lines)

    def to_markdown(self) -> str:
        """Render the weekly review as a reusable Markdown report."""
        prefix = "[DRY RUN] " if self.dry_run else ""
        lines = [
            f"# {prefix}Cortex Weekly Review",
            "",
            f"Weekly review report ({'dry-run' if self.dry_run else 'read-only'}).",
            "",
            "## Summary",
            "",
            f"- Status: {'ERROR' if self.error else 'OK'}",
            f"- Graph: {self.graph_stats.get('node_count', 0)} nodes, {self.graph_stats.get('edge_count', 0)} edges",
            f"- Duplicates: {len(self.duplicate_groups)}",
            f"- Stale high-importance notes: {len(self.stale_high_importance)}",
            f"- Broken references: {len(self.broken_references)}",
            f"- Consolidation proposals: {len(self.consolidation_proposals)}",
            f"- Orphan nodes: {len(self.orphan_nodes)}",
            f"- Contradictions: {len(self.contradiction_clusters)}",
            f"- Duration: {self.duration_seconds:.1f}s",
        ]
        if self.error:
            lines.append(f"- Error: {self.error}")

        lines.extend(["", "## Graph Stats", ""])
        if self.graph_stats:
            for key in sorted(self.graph_stats):
                lines.append(f"- {key}: {self.graph_stats[key]}")
        else:
            lines.append("- No graph stats available.")

        _append_items(lines, "Duplicates", self.duplicate_groups, ["kind", "key", "nodes"])
        _append_items(lines, "Stale High-Importance Notes", self.stale_high_importance, ["node_id", "importance", "last_verified", "age_days"])
        _append_items(lines, "Broken References", self.broken_references, ["kind", "source_node", "target_raw"])
        _append_items(lines, "Consolidation Proposals", self.consolidation_proposals, ["parent", "label", "degree", "note_neighbor_count", "neighbors"])
        _append_items(lines, "Orphan Nodes", self.orphan_nodes, ["id", "label", "file"])
        _append_items(lines, "Contradictions", self.contradiction_clusters, ["source", "target", "relation"])

        lines.extend(["", "## Runtime", "", f"- Duration: {self.duration_seconds:.1f}s"])
        if self.error:
            lines.append(f"- Error: {self.error}")
        else:
            lines.append("- Error: none")
        return "\n".join(lines).rstrip() + "\n"


def _append_items(lines: list[str], title: str, items: list[dict[str, Any]], keys: list[str]) -> None:
    lines.extend(["", f"## {title}", ""])
    if not items:
        lines.append("- None.")
        return
    for item in items:
        parts = []
        for key in keys:
            if key in item and item[key] not in (None, "", []):
                value = item[key]
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                parts.append(f"{key}: {value}")
        if not parts:
            parts = [str(item)]
        lines.append("- " + "; ".join(parts))


def run_weekly_review(
    cfg: Config,
    *,
    dry_run: bool = False,
    stale_days: int = 180,
    stale_min_importance: float = 4.0,
    consolidation_min_degree: int = 3,
    reference_date: date | None = None,
) -> WeeklyReviewReport:
    """Run the Phase 6 weekly review in read-only mode.

    Consumes the graph JSONL artifacts produced by ``cortex graph build`` and
    optionally reads ``chunks.jsonl`` only to enrich dedupe with content hashes.
    No output files are written; ``dry_run`` only affects report labeling.
    """
    from cortex.graph_diagnostics import (
        GraphArtifacts,
        compute_centrality,
        find_contradictions,
        find_orphans,
        find_stale,
    )

    t0 = time.monotonic()
    report = WeeklyReviewReport(dry_run=dry_run)
    try:
        artifacts = GraphArtifacts.load(cfg)
        report.graph_stats = dict(artifacts.stats)

        file_hashes = _load_content_hashes_by_file(cfg)
        report.duplicate_groups = _find_duplicate_note_groups(artifacts.nodes, file_hashes)
        report.stale_high_importance = [
            s for s in find_stale(artifacts, stale_days=stale_days, reference_date=reference_date)
            if float(s.get("importance") or 0.0) >= stale_min_importance
        ]
        report.broken_references = sorted(
            list(artifacts.broken),
            key=lambda b: (b.get("kind", ""), b.get("source_node", ""), b.get("target_raw", "")),
        )
        report.orphan_nodes = find_orphans(artifacts)
        report.contradiction_clusters = find_contradictions(artifacts)
        centrality = compute_centrality(artifacts)
        report.consolidation_proposals = _propose_consolidations(
            artifacts.nodes,
            artifacts.edges,
            centrality,
            min_degree=consolidation_min_degree,
        )
    except Exception as e:
        report.error = str(e)
    finally:
        report.duration_seconds = time.monotonic() - t0
    return report


def _load_content_hashes_by_file(cfg: Config) -> dict[str, str]:
    """Read chunks.jsonl and return file -> content_hash for single-hash files."""
    chunks_path = cfg.index.chunks_path
    if not chunks_path.exists():
        return {}

    hashes_by_file: dict[str, set[str]] = defaultdict(set)
    with chunks_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            chunk = json.loads(line)
            file_rel = chunk.get("file")
            content_hash = chunk.get("content_hash")
            if file_rel and content_hash:
                hashes_by_file[str(file_rel)].add(str(content_hash))

    # If a file has multiple chunk-level hashes, don't invent a note-level hash.
    # Exact whole-note hash dedupe is only safe when the index gives us one hash.
    return {file_rel: next(iter(hashes)) for file_rel, hashes in hashes_by_file.items() if len(hashes) == 1}


def _find_duplicate_note_groups(
    nodes: list[dict[str, Any]],
    file_hashes: dict[str, str],
) -> list[dict[str, Any]]:
    """Find duplicate note candidates by title, alias, or content hash."""
    notes = [n for n in nodes if n.get("type") == "note"]
    buckets: dict[tuple[str, str], set[str]] = defaultdict(set)

    for note in notes:
        node_id = str(note.get("id") or "")
        if not node_id:
            continue

        title = str(note.get("label") or "").strip().casefold()
        if title:
            buckets[("title", title)].add(node_id)

        aliases = (note.get("metadata") or {}).get("aliases", [])
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(",") if a.strip()]
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias.strip():
                    buckets[("alias", alias.strip().casefold())].add(node_id)

        file_rel = str(note.get("file") or "")
        content_hash = (note.get("metadata") or {}).get("content_hash") or file_hashes.get(file_rel)
        if content_hash:
            buckets[("content_hash", str(content_hash))].add(node_id)

    groups = [
        {"kind": kind, "key": key, "nodes": sorted(node_ids)}
        for (kind, key), node_ids in buckets.items()
        if len(node_ids) > 1
    ]
    groups.sort(key=lambda g: (g["kind"], g["key"], g["nodes"]))
    return groups


def _propose_consolidations(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    centrality: list[Any],
    *,
    min_degree: int,
) -> list[dict[str, Any]]:
    """Propose high-degree note clusters that may deserve parent notes."""
    node_by_id = {n.get("id"): n for n in nodes}
    note_ids = {n.get("id") for n in nodes if n.get("type") == "note"}

    note_neighbors: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        src = edge.get("source")
        tgt = edge.get("target")
        if src in note_ids and tgt in note_ids and src != tgt:
            note_neighbors[str(src)].add(str(tgt))
            note_neighbors[str(tgt)].add(str(src))

    proposals: list[dict[str, Any]] = []
    for entry in centrality:
        if entry.node_type != "note" or entry.degree < min_degree:
            continue
        neighbors = sorted(note_neighbors.get(entry.node_id, set()))
        if len(neighbors) < 2:
            continue
        proposals.append({
            "parent": entry.node_id,
            "label": node_by_id.get(entry.node_id, {}).get("label", entry.label),
            "degree": entry.degree,
            "note_neighbor_count": len(neighbors),
            "neighbors": neighbors,
        })

    proposals.sort(key=lambda p: (-p["degree"], p["parent"]))
    return proposals
