"""Graph artifact builder for hermes-cortex — Phase 5.5 / Slice 1.

Builds a full knowledge graph from the indexed vault and writes stable JSONL
artifacts for consumption by diagnostics (Slice 2), export (Slice 3), and
lifecycle plugins (Phase 6).

This module is SEPARATE from ``cortex.graph.WikilinkGraph`` which is a
lightweight, in-memory, retrieval-time structure. ``graph_index`` is a
*build-time* artifact generator that:

  1. Collects ALL nodes (notes, chunks, tags, aliases) from chunks.jsonl
  2. Resolves ALL edges deterministically against the node registry
  3. Emits unresolved/ambiguous/orphan diagnostics into graph_broken.jsonl

Resolution rules (in priority order):
  - exact title match
  - case-insensitive title match
  - alias match (from frontmatter ``aliases`` field)
  - slug match (using cortex.text.slugify)
  - vault-relative path match (without .md extension)

Design constraints:
  - Ambiguous matches must NOT silently resolve
  - Broken references are preserved as diagnostics, never dropped
  - Output must be deterministic (sorted, stable ordering)
  - No heavy dependencies (stdlib + existing cortex modules only)
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from cortex.config import Config
from cortex.indexer import load_existing_chunks
from cortex.text import slugify

log = logging.getLogger("cortex.graph_index")


# ---- Node & Edge types -----------------------------------------------------

NodeType = Literal["note", "chunk", "tag", "alias", "memory", "skill", "session", "topic"]
EdgeType = Literal[
    "contains",
    "links_to",
    "mentions",
    "tagged_with",
    "aliases",
    "derived_from",
    "supports",
    "contradicts",
    "supersedes",
    "superseded_by",
    "stale_relative_to",
]

# Types of broken references
BrokenKind = Literal["unresolved", "ambiguous"]


# ---- Data models -----------------------------------------------------------


@dataclass
class GraphNode:
    """A node in the knowledge graph."""

    id: str                # unique node identifier
    type: NodeType         # node type discriminator
    label: str             # human-readable label (title, tag name, etc.)
    file: str = ""         # vault-relative path (for note/chunk nodes)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop empty metadata to keep JSONL compact
        if not d["metadata"]:
            del d["metadata"]
        if not d["file"]:
            del d["file"]
        return d


@dataclass
class GraphEdge:
    """A directed edge in the knowledge graph."""

    source: str            # source node ID
    target: str            # target node ID
    type: EdgeType         # edge type discriminator
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["metadata"]:
            del d["metadata"]
        return d


@dataclass
class BrokenReference:
    """An unresolved or ambiguous reference found during graph building."""

    source_node: str       # node ID that contains the reference
    target_raw: str        # the raw reference string (e.g. wikilink target)
    kind: BrokenKind       # "unresolved" or "ambiguous"
    candidates: list[str] = field(default_factory=list)  # for ambiguous: the candidate node IDs
    context: str = ""      # where in the source this ref was found

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if not d["candidates"]:
            del d["candidates"]
        if not d["context"]:
            del d["context"]
        return d


@dataclass
class GraphStats:
    """Summary statistics about the built graph."""

    node_count: int = 0
    edge_count: int = 0
    broken_count: int = 0
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_type: dict[str, int] = field(default_factory=dict)
    broken_by_kind: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---- Node registry (Pass 1) -----------------------------------------------


class NodeRegistry:
    """Collects and indexes all nodes for deterministic resolution.

    Supports multiple lookup strategies:
      - exact title (case-sensitive)
      - case-insensitive title
      - alias
      - slug
      - vault-relative path (without .md)
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}  # id → node

        # Lookup indices: key → list of node IDs
        self._by_title_exact: dict[str, list[str]] = {}
        self._by_title_lower: dict[str, list[str]] = {}
        self._by_alias_lower: dict[str, list[str]] = {}
        self._by_slug: dict[str, list[str]] = {}
        self._by_path: dict[str, list[str]] = {}  # vault-relative without .md

    def add(self, node: GraphNode) -> None:
        """Register a node and update all lookup indices."""
        if node.id in self._nodes:
            return  # already registered
        self._nodes[node.id] = node

        # Alias nodes are first-class graph nodes for visualization/edges, but
        # they must not be independently resolvable by title or slug. Alias
        # lookup should resolve to the owning note via that note's metadata.
        # Otherwise self-aliases such as aliases: ["My Note"] make [[My Note]]
        # ambiguous between the note node and alias node.
        if node.type == "alias":
            return

        # Index by label (title)
        label = node.label
        self._by_title_exact.setdefault(label, []).append(node.id)
        self._by_title_lower.setdefault(label.lower(), []).append(node.id)

        # Index by slug
        slug = slugify(label)
        self._by_slug.setdefault(slug, []).append(node.id)

        # Index by vault-relative path for note nodes only.
        # Chunk nodes also carry node.file, but a plain Obsidian wikilink like
        # [[folder/Note]] should resolve to the note, not to every chunk in it.
        if node.type == "note" and node.file:
            # Remove .md extension for path-based lookup
            path_key = node.file
            if path_key.endswith(".md"):
                path_key = path_key[:-3]
            self._by_path.setdefault(path_key, []).append(node.id)

        # Index aliases from note metadata to the owning note.
        if node.type == "note":
            aliases = node.metadata.get("aliases", [])
            if isinstance(aliases, list):
                for alias in aliases:
                    if alias and isinstance(alias, str):
                        self._by_alias_lower.setdefault(alias.strip().lower(), []).append(node.id)

    def get(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def all_nodes(self) -> list[GraphNode]:
        """Return all nodes sorted by ID for deterministic output."""
        return [self._nodes[k] for k in sorted(self._nodes)]

    def __len__(self) -> int:
        return len(self._nodes)

    def resolve(self, raw_ref: str) -> tuple[list[str], BrokenKind | None]:
        """Resolve a raw reference string to node ID(s).

        Returns:
            (node_ids, broken_kind)
            - If exactly one match: ([node_id], None) — resolved
            - If multiple matches: ([candidates...], "ambiguous")
            - If no matches: ([], "unresolved")

        Resolution priority:
            1. exact title match (case-sensitive)
            2. case-insensitive title match
            3. alias match (case-insensitive)
            4. slug match
            5. vault-relative path match

        Disambiguation: when ``note:`` and ``chunk:`` nodes share the same
        title, ``note:`` wins — a plain wikilink like ``[[Foo]]`` targets
        the whole note, not one of its heading sections.
        """
        if not raw_ref or not isinstance(raw_ref, str):
            return [], "unresolved"

        ref = raw_ref.strip()
        if not ref:
            return [], "unresolved"

        def _disambiguate(candidates: list[str]) -> tuple[list[str], BrokenKind | None]:
            """Prefer note over chunk when both match the same title."""
            if len(candidates) <= 1:
                return candidates[:], None
            notes = [
                nid for nid in candidates
                if self._nodes.get(nid) and self._nodes[nid].type == "note"
            ]
            if len(notes) == 1:
                return notes[:], None
            if len(notes) > 1:
                return sorted(notes), "ambiguous"
            return sorted(candidates), "ambiguous"

        # 1. Exact title match
        exact = self._by_title_exact.get(ref, [])
        if exact:
            return _disambiguate(exact)

        # 2. Case-insensitive title match
        lower = self._by_title_lower.get(ref.lower(), [])
        if lower:
            return _disambiguate(lower)

        # 3. Alias match (case-insensitive)
        alias_hits = self._by_alias_lower.get(ref.lower(), [])
        if alias_hits:
            return _disambiguate(alias_hits)

        # 4. Slug match
        ref_slug = slugify(ref)
        slug_hits = self._by_slug.get(ref_slug, [])
        if slug_hits:
            return _disambiguate(slug_hits)

        # 5. Vault-relative path match
        # Try with and without .md extension
        path_ref = ref
        if path_ref.endswith(".md"):
            path_ref = path_ref[:-3]
        path_hits = self._by_path.get(path_ref, [])
        if path_hits:
            return _disambiguate(path_hits)

        return [], "unresolved"


# ---- Graph builder ---------------------------------------------------------


def _as_ref_list(value: Any) -> list[str]:
    """Normalize frontmatter reference fields into a list of strings."""
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


def _normalize_ref(ref: str) -> str:
    """Strip Obsidian wikilink delimiters/alias from a reference."""
    text = str(ref).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    return text


@dataclass
class GraphBuildReport:
    """Report from a graph build operation."""

    nodes: int = 0
    edges: int = 0
    broken: int = 0
    nodes_by_type: dict[str, int] = field(default_factory=dict)
    edges_by_type: dict[str, int] = field(default_factory=dict)
    broken_by_kind: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"Graph built: {self.nodes} nodes, {self.edges} edges, "
            f"{self.broken} broken references"
        )


class GraphBuilder:
    """Build a knowledge graph from chunks.jsonl data.

    Three-pass architecture:
      Pass 1: Collect all nodes (notes, chunks, tags, aliases)
      Pass 2: Resolve edges deterministically against node registry
      Pass 3: Emit broken/ambiguous/orphan diagnostics
    """

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self._chunks = chunks
        self._registry = NodeRegistry()
        self._edges: list[GraphEdge] = []
        self._broken: list[BrokenReference] = []

    @property
    def registry(self) -> NodeRegistry:
        return self._registry

    @property
    def edges(self) -> list[GraphEdge]:
        return self._edges

    @property
    def broken(self) -> list[BrokenReference]:
        return self._broken

    def build(self) -> GraphBuildReport:
        """Execute the three-pass build."""
        self._pass1_collect_nodes()
        self._pass2_resolve_edges()
        self._pass3_diagnostics()
        return self._make_report()

    # ---- Pass 1: Collect nodes ----

    def _pass1_collect_nodes(self) -> None:
        """Collect all nodes from chunks data."""
        seen_notes: set[str] = set()
        seen_tags: set[str] = set()

        for chunk in self._chunks:
            file_rel = chunk.get("file", "")
            chunk_id = chunk.get("id", "")

            # Note node (one per file)
            if file_rel and file_rel not in seen_notes:
                seen_notes.add(file_rel)
                # Note title = filename stem
                stem = Path(file_rel).stem
                note_id = f"note:{file_rel}"

                # Collect aliases from frontmatter
                fm = chunk.get("frontmatter", {}) or {}
                aliases = fm.get("aliases", [])
                if isinstance(aliases, str):
                    aliases = [a.strip() for a in aliases.split(",") if a.strip()]
                elif not isinstance(aliases, list):
                    aliases = []

                metadata: dict[str, Any] = {}
                if aliases:
                    metadata["aliases"] = aliases

                # Include frontmatter-level info
                fm_norm = chunk.get("fm_normalized", {}) or {}
                if fm_norm.get("type"):
                    metadata["note_type"] = fm_norm["type"]
                if fm_norm.get("status"):
                    metadata["status"] = fm_norm["status"]
                # Diagnostic-relevant fields (Phase 5.5 Slice 2)
                if fm_norm.get("last_verified"):
                    metadata["last_verified"] = fm_norm["last_verified"]
                if fm_norm.get("importance"):
                    metadata["importance"] = fm_norm["importance"]
                if fm_norm.get("confidence"):
                    metadata["confidence"] = fm_norm["confidence"]
                if chunk.get("modified_date"):
                    metadata["modified_date"] = chunk["modified_date"]

                self._registry.add(GraphNode(
                    id=note_id,
                    type="note",
                    label=stem,
                    file=file_rel,
                    metadata=metadata,
                ))

                # Alias nodes (separate so aliases are first-class resolvable)
                for alias in aliases:
                    alias_id = f"alias:{alias.lower()}"
                    self._registry.add(GraphNode(
                        id=alias_id,
                        type="alias",
                        label=alias,
                    ))
                    # Edge: note -[aliases]-> alias node
                    self._edges.append(GraphEdge(
                        source=note_id,
                        target=alias_id,
                        type="aliases",
                    ))

            # Chunk node
            if chunk_id:
                heading_path = chunk.get("heading_path", [])
                label = " / ".join(heading_path) if heading_path else "(intro)"
                self._registry.add(GraphNode(
                    id=f"chunk:{chunk_id}",
                    type="chunk",
                    label=label,
                    file=file_rel,
                ))

                # Edge: note -[contains]-> chunk
                if file_rel:
                    note_id = f"note:{file_rel}"
                    self._edges.append(GraphEdge(
                        source=note_id,
                        target=f"chunk:{chunk_id}",
                        type="contains",
                    ))

            # Tag nodes
            tags = chunk.get("tags", []) or []
            for tag in tags:
                if not tag or not isinstance(tag, str):
                    continue
                tag_lower = tag.strip().lower()
                tag_id = f"tag:{tag_lower}"
                if tag_lower not in seen_tags:
                    seen_tags.add(tag_lower)
                    self._registry.add(GraphNode(
                        id=tag_id,
                        type="tag",
                        label=tag.strip(),
                    ))

                # Edge: note -[tagged_with]-> tag
                if file_rel:
                    note_id = f"note:{file_rel}"
                    self._edges.append(GraphEdge(
                        source=note_id,
                        target=tag_id,
                        type="tagged_with",
                    ))

        log.info("Pass 1 complete: %d nodes collected", len(self._registry))

    # ---- Pass 2: Resolve edges ----

    def _pass2_resolve_edges(self) -> None:
        """Resolve wikilinks into graph edges."""
        seen_link_edges: set[tuple[str, str]] = set()
        seen_notes: set[str] = set()

        for chunk in self._chunks:
            file_rel = chunk.get("file", "")
            if not file_rel:
                continue

            note_id = f"note:{file_rel}"
            wikilinks = chunk.get("wikilinks", []) or []

            # Only process wikilinks once per note (dedup across chunks)
            if note_id in seen_notes:
                # Still process per-chunk wikilinks for finer-grained edges
                pass
            seen_notes.add(note_id)

            for link_target in wikilinks:
                if not link_target or not isinstance(link_target, str):
                    continue

                link_target = link_target.strip()
                if not link_target:
                    continue

                # Deduplicate link edges per (source_note, target_raw)
                edge_key = (note_id, link_target.lower())
                if edge_key in seen_link_edges:
                    continue
                seen_link_edges.add(edge_key)

                # Resolve against registry
                resolved_ids, broken_kind = self._registry.resolve(link_target)

                if broken_kind is None and resolved_ids:
                    # Successful resolution — add links_to edge to each resolved node
                    for target_id in resolved_ids:
                        self._edges.append(GraphEdge(
                            source=note_id,
                            target=target_id,
                            type="links_to",
                        ))
                elif broken_kind == "ambiguous":
                    self._broken.append(BrokenReference(
                        source_node=note_id,
                        target_raw=link_target,
                        kind="ambiguous",
                        candidates=resolved_ids,
                        context="wikilink",
                    ))
                else:
                    # Unresolved
                    self._broken.append(BrokenReference(
                        source_node=note_id,
                        target_raw=link_target,
                        kind="unresolved",
                        context="wikilink",
                    ))

            for fm_field, edge_type in (
                ("derived_from", "derived_from"),
                ("supports", "supports"),
                ("contradicts", "contradicts"),
                ("supersedes", "supersedes"),
                ("superseded_by", "superseded_by"),
            ):
                for link_target in _as_ref_list((chunk.get("frontmatter", {}) or {}).get(fm_field)):
                    self._resolve_reference_edge(
                        source_node=note_id,
                        target_raw=link_target,
                        edge_type=edge_type,
                        context=f"frontmatter:{fm_field}",
                    )

        log.info("Pass 2 complete: %d edges, %d broken", len(self._edges), len(self._broken))

    def _resolve_reference_edge(
        self,
        *,
        source_node: str,
        target_raw: str,
        edge_type: EdgeType,
        context: str,
    ) -> None:
        """Resolve a frontmatter reference into a typed graph edge or broken ref."""
        ref = _normalize_ref(target_raw)
        if not ref:
            return
        resolved_ids, broken_kind = self._registry.resolve(ref)
        if broken_kind is None and resolved_ids:
            for target_id in resolved_ids:
                edge_key = (source_node, target_id, edge_type)
                if any((e.source, e.target, e.type) == edge_key for e in self._edges):
                    continue
                self._edges.append(GraphEdge(source=source_node, target=target_id, type=edge_type))
        elif broken_kind == "ambiguous":
            self._broken.append(BrokenReference(
                source_node=source_node,
                target_raw=ref,
                kind="ambiguous",
                candidates=resolved_ids,
                context=context,
            ))
        else:
            self._broken.append(BrokenReference(
                source_node=source_node,
                target_raw=ref,
                kind="unresolved",
                context=context,
            ))

    # ---- Pass 3: Diagnostics ----

    def _pass3_diagnostics(self) -> None:
        """Identify orphan nodes and emit additional diagnostics.

        Orphan detection: nodes with zero incoming AND zero outgoing edges.
        These are noted in metadata but not added to broken references
        (they're valid nodes, just disconnected).
        """
        # Count edges per node
        edge_participation: Counter[str] = Counter()
        for edge in self._edges:
            edge_participation[edge.source] += 1
            edge_participation[edge.target] += 1

        orphan_count = 0
        for node in self._registry.all_nodes():
            if edge_participation[node.id] == 0:
                orphan_count += 1
                node.metadata["orphan"] = True

        log.info("Pass 3 complete: %d orphan nodes detected", orphan_count)

    # ---- Report ----

    def _make_report(self) -> GraphBuildReport:
        nodes = self._registry.all_nodes()
        node_types: Counter[str] = Counter(n.type for n in nodes)
        edge_types: Counter[str] = Counter(e.type for e in self._edges)
        broken_kinds: Counter[str] = Counter(b.kind for b in self._broken)

        return GraphBuildReport(
            nodes=len(nodes),
            edges=len(self._edges),
            broken=len(self._broken),
            nodes_by_type=dict(node_types),
            edges_by_type=dict(edge_types),
            broken_by_kind=dict(broken_kinds),
        )


# ---- Artifact writer -------------------------------------------------------


# Default artifact directory lives alongside chunks.jsonl
_GRAPH_DIR = "graph"


def _graph_artifact_dir(cfg: Config) -> Path:
    """Return the directory for graph artifacts (next to chunks.jsonl)."""
    return cfg.index.chunks_path.parent / _GRAPH_DIR


def write_graph_artifacts(
    cfg: Config,
    builder: GraphBuilder,
) -> Path:
    """Write all graph artifacts atomically.

    Writes to a temp dir first, then renames into place. Returns the
    artifact directory path.
    """
    artifact_dir = _graph_artifact_dir(cfg)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    nodes = builder.registry.all_nodes()
    edges = builder.edges
    broken = builder.broken

    # graph_nodes.jsonl
    _write_jsonl(
        artifact_dir / "graph_nodes.jsonl",
        [n.to_dict() for n in nodes],
    )

    # graph_edges.jsonl — sort for determinism
    sorted_edges = sorted(edges, key=lambda e: (e.source, e.target, e.type))
    _write_jsonl(
        artifact_dir / "graph_edges.jsonl",
        [e.to_dict() for e in sorted_edges],
    )

    # graph_broken.jsonl — sort for determinism
    sorted_broken = sorted(broken, key=lambda b: (b.source_node, b.target_raw, b.kind))
    _write_jsonl(
        artifact_dir / "graph_broken.jsonl",
        [b.to_dict() for b in sorted_broken],
    )

    # graph_stats.json
    node_types: Counter[str] = Counter(n.type for n in nodes)
    edge_types: Counter[str] = Counter(e.type for e in edges)
    broken_kinds: Counter[str] = Counter(b.kind for b in broken)

    stats = GraphStats(
        node_count=len(nodes),
        edge_count=len(edges),
        broken_count=len(broken),
        nodes_by_type=dict(sorted(node_types.items())),
        edges_by_type=dict(sorted(edge_types.items())),
        broken_by_kind=dict(sorted(broken_kinds.items())),
    )
    _write_json(artifact_dir / "graph_stats.json", stats.to_dict())

    log.info(
        "Graph artifacts written to %s: %d nodes, %d edges, %d broken",
        artifact_dir, len(nodes), len(edges), len(broken),
    )
    return artifact_dir


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    """Atomic JSONL write (write to .tmp then rename)."""
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.rename(path)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic JSON write."""
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    tmp.rename(path)


# ---- Public API: build_graph -----------------------------------------------


def build_graph(cfg: Config, *, force: bool = False) -> GraphBuildReport:
    """Build graph artifacts from the current chunks.jsonl.

    This is the main entry point called by ``cortex graph build``.
    Requires that ``cortex index`` has been run first (chunks.jsonl must exist).

    Args:
        cfg: Loaded cortex config.
        force: If True, always rebuild (currently always rebuilds; reserved
               for future incremental builds).

    Returns:
        GraphBuildReport with summary statistics.

    Raises:
        FileNotFoundError: If chunks.jsonl doesn't exist.
    """
    chunks_path = cfg.index.chunks_path
    if not chunks_path.exists():
        raise FileNotFoundError(
            f"chunks.jsonl not found at {chunks_path}. "
            f"Run `cortex index` first to build the chunk index."
        )

    chunks = load_existing_chunks(chunks_path)
    if not chunks:
        log.warning("chunks.jsonl is empty — graph will have no nodes")

    builder = GraphBuilder(chunks)
    report = builder.build()

    write_graph_artifacts(cfg, builder)

    return report
