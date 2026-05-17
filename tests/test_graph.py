"""Unit tests for cortex.graph — title resolution + 1-hop expansion."""

from __future__ import annotations

from typing import Any

from cortex.graph import WikilinkGraph, expand, graph_candidates


def _chunk(cid: str, file: str, wikilinks: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": cid,
        "file": file,
        "wikilinks": wikilinks or [],
        "tags": [],
        "fm_normalized": {"type": "fact"},
        "frontmatter": {"type": "fact"},
    }


CHUNKS = [
    _chunk("alpha.md#0", "10_facts/Alpha.md", wikilinks=["Beta", "Gamma"]),
    _chunk("alpha.md#1", "10_facts/Alpha.md", wikilinks=["Delta"]),
    _chunk("beta.md#0",  "10_facts/Beta.md",  wikilinks=["Gamma"]),
    _chunk("gamma.md#0", "10_facts/Gamma.md"),
    _chunk("delta.md#0", "10_facts/Delta.md", wikilinks=["Beta"]),
]
BY_ID = {c["id"]: c for c in CHUNKS}


# ---- WikilinkGraph ---------------------------------------------------------


def test_resolve_basic():
    g = WikilinkGraph(CHUNKS)
    assert g.resolve("Beta") == ["beta.md#0"]


def test_resolve_case_insensitive():
    g = WikilinkGraph(CHUNKS)
    assert g.resolve("beta") == ["beta.md#0"]
    assert g.resolve("  BETA  ") == ["beta.md#0"]


def test_resolve_unknown_returns_empty():
    g = WikilinkGraph(CHUNKS)
    assert g.resolve("Nonexistent") == []
    assert g.resolve("") == []
    assert g.resolve(None) == []  # type: ignore[arg-type]


def test_resolve_returns_all_chunks_in_file():
    """Multi-chunk file: a wikilink to its title resolves to every chunk."""
    g = WikilinkGraph(CHUNKS)
    assert g.resolve("Alpha") == ["alpha.md#0", "alpha.md#1"]


def test_known_titles_count():
    g = WikilinkGraph(CHUNKS)
    # Alpha, Beta, Gamma, Delta = 4 distinct files.
    assert g.known_titles() == 4


# ---- 1-hop expand ----------------------------------------------------------


def test_expand_one_hop_excludes_seeds():
    g = WikilinkGraph(CHUNKS)
    out = expand(["alpha.md#0"], g, BY_ID, hops=1)
    # Alpha → Beta, Gamma. Seeds excluded.
    assert "alpha.md#0" not in out
    assert set(out) == {"beta.md#0", "gamma.md#0"}


def test_expand_zero_hops_returns_empty():
    g = WikilinkGraph(CHUNKS)
    assert expand(["alpha.md#0"], g, BY_ID, hops=0) == []


def test_expand_dedupes_across_seeds():
    g = WikilinkGraph(CHUNKS)
    # alpha.md#0 → Gamma; beta.md#0 → Gamma. Should appear once.
    out = expand(["alpha.md#0", "beta.md#0"], g, BY_ID, hops=1)
    assert out.count("gamma.md#0") == 1


def test_expand_two_hops_bfs_order():
    g = WikilinkGraph(CHUNKS)
    # Seed alpha.md#0 → hop1: beta, gamma; hop2: from beta→gamma (skip,seen),
    # nothing new. Add a chain: alpha→beta→gamma is already covered.
    # Use delta as seed: delta→beta (hop1), beta→gamma (hop2).
    out = expand(["delta.md#0"], g, BY_ID, hops=2)
    assert out == ["beta.md#0", "gamma.md#0"]


def test_expand_unknown_target_silently_skipped():
    chunks = [_chunk("x.md#0", "10_facts/X.md", wikilinks=["DoesNotExist", "X"])]
    by_id = {c["id"]: c for c in chunks}
    g = WikilinkGraph(chunks)
    # Self-link to "X" resolves to seed → excluded; unknown → skipped.
    assert expand(["x.md#0"], g, by_id, hops=1) == []


# ---- graph_candidates ------------------------------------------------------


def test_graph_candidates_synthetic_score_descends():
    g = WikilinkGraph(CHUNKS)
    cands = graph_candidates(["alpha.md#0"], g, BY_ID, hops=1)
    scores = [s for _, s in cands]
    assert scores == sorted(scores, reverse=True)


def test_graph_candidates_pool_size_caps():
    g = WikilinkGraph(CHUNKS)
    cands = graph_candidates(["alpha.md#0"], g, BY_ID, hops=1, pool_size=1)
    assert len(cands) == 1


def test_graph_candidates_filter_predicate_drops():
    g = WikilinkGraph(CHUNKS)
    # Reject Gamma via predicate; keep Beta.
    def pred(c):
        return c.get("id") != "gamma.md#0"

    cands = graph_candidates(["alpha.md#0"], g, BY_ID, hops=1, filter_predicate=pred)
    ids = [cid for cid, _ in cands]
    assert "gamma.md#0" not in ids
    assert "beta.md#0" in ids
