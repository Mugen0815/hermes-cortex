"""Tests for cortex.context — Phase 4 ContextBuilder."""

from __future__ import annotations

import re
from pathlib import Path


from cortex.config import (
    Config,
    ContextBuilderConfig,
    EmbeddingsConfig,
    HermesMemoryConfig,
    IndexConfig,
    SearchConfig,
    VaultConfig,
)
from cortex.context import ContextBuilder
from cortex.search import SearchResult
from cortex.text import estimate_tokens


# ---- Fixtures --------------------------------------------------------------


def make_cfg(
    tmp_path: Path,
    *,
    token_budget: int = 4000,
    include_hermes_memory: bool = False,
    memory_path: Path | None = None,
    user_path: Path | None = None,
) -> Config:
    return Config(
        vault=VaultConfig(path=tmp_path / "vault"),
        hermes_memory=HermesMemoryConfig(
            memory_path=memory_path,
            user_path=user_path,
        ),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(model="test-model", device="cpu"),
        search=SearchConfig(),
        context_builder=ContextBuilderConfig(
            token_budget=token_budget,
            cite_sources=True,
            include_hermes_memory=include_hermes_memory,
        ),
    )


def make_result(
    cid: str,
    text: str,
    *,
    file: str = "10_facts/Foo.md",
    heading_path: list[str] | None = None,
    final_score: float = 0.5,
) -> SearchResult:
    if heading_path is None:
        heading_path = ["Section A"]
    chunk = {
        "id": cid,
        "file": file,
        "folder": file.split("/", 1)[0],
        "heading_path": heading_path,
        "heading": heading_path[-1] if heading_path else None,
        "text": text,
        "tags": [],
        "wikilinks": [],
        "frontmatter": {},
        "fm_normalized": {},
        "modified_date": "2026-04-15",
        "modified": "2026-04-15T00:00:00",
        "content_hash": cid,
        "char_len": len(text),
        "token_estimate": max(1, len(text) // 4),
    }
    return SearchResult(
        chunk_id=cid,
        chunk=chunk,
        bm25_score=0.5,
        vector_score=0.5,
        bm25_rank=1,
        vector_rank=1,
        rrf_score=final_score,
        final_score=final_score,
    )


# ---- Empty / trivial cases -------------------------------------------------


def test_build_empty_results_returns_doc_heading_only(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([])
    assert ctx.text.startswith("# Context")
    assert ctx.chunks_included == []
    assert ctx.citation_count == 0
    assert "## Vault Hits" not in ctx.text
    assert "## Citations" not in ctx.text


def test_zero_budget_returns_empty(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path, token_budget=0))
    ctx = builder.build([make_result("a.md#0", "stuff")])
    assert ctx.text == ""
    assert ctx.tokens_used == 0


# ---- Citations & body integrity --------------------------------------------


def test_chunk_text_emitted_verbatim(tmp_path: Path) -> None:
    """The original chunk text must NOT be modified by injection."""
    body = "First line.\nSecond line with [[Wikilink]] and code `foo()`."
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([make_result("a.md#0", body)])
    assert body in ctx.text
    # No citation marker injected inside the body.
    assert "[^1]" in ctx.text  # in header + bibliography
    body_section_start = ctx.text.index(body)
    body_section_end = body_section_start + len(body)
    extracted = ctx.text[body_section_start:body_section_end]
    assert extracted == body


def test_citation_marker_only_in_header_and_bibliography(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([
        make_result("a.md#0", "alpha body text"),
        make_result("b.md#0", "beta body text"),
    ])
    # Two citations expected: each appears in a header line and in
    # bibliography line, total 2 occurrences of [^1] and 2 of [^2].
    assert ctx.text.count("[^1]") == 2
    assert ctx.text.count("[^2]") == 2
    assert ctx.citation_count == 2


def test_citation_header_format(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([
        make_result(
            "a.md#0", "body",
            file="10_facts/Foo.md",
            heading_path=["Section A", "Subsection B"],
        ),
    ])
    assert "### [^1] 10_facts/Foo.md :: Section A / Subsection B" in ctx.text


def test_bibliography_includes_score(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([
        make_result("a.md#0", "body", final_score=0.1234),
    ])
    assert re.search(r"\[\^1\]:.*score=0\.1234", ctx.text)


def test_chunk_with_no_heading_path_gets_intro_label(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([
        make_result("a.md#0", "body", heading_path=[]),
    ])
    assert "(intro)" in ctx.text


# ---- Budget enforcement ----------------------------------------------------


def test_oversized_chunk_skipped_not_truncated(tmp_path: Path) -> None:
    """A chunk that doesn't fit must be skipped whole; never truncated."""
    huge_body = "word " * 2000  # ~2500 chars → ~625 tokens
    small_body = "small chunk body"
    builder = ContextBuilder(make_cfg(tmp_path, token_budget=200))
    ctx = builder.build([
        make_result("big.md#0", huge_body, final_score=0.9),
        make_result("small.md#0", small_body, final_score=0.5),
    ])
    # Big chunk skipped, small chunk included whole.
    assert "big.md#0" in ctx.chunks_skipped_oversize
    assert "big.md#0" not in ctx.chunks_included
    assert "small.md#0" in ctx.chunks_included
    assert small_body in ctx.text
    # The huge body is not in the output anywhere.
    assert huge_body[:100] not in ctx.text


def test_subsequent_chunks_still_considered_after_skip(tmp_path: Path) -> None:
    """Skipping one oversized chunk must not abort the loop."""
    builder = ContextBuilder(make_cfg(tmp_path, token_budget=200))
    ctx = builder.build([
        make_result("big.md#0", "x" * 4000, final_score=0.9),
        make_result("a.md#0", "small a", final_score=0.5),
        make_result("b.md#0", "small b", final_score=0.4),
    ])
    assert ctx.chunks_included == ["a.md#0", "b.md#0"]


def test_token_budget_never_exceeded(tmp_path: Path) -> None:
    """Sum of body estimates + overhead must stay <= budget."""
    builder = ContextBuilder(make_cfg(tmp_path, token_budget=300))
    results = [
        make_result(f"c{i}.md#0", "word " * 30, final_score=1.0 - i * 0.01)
        for i in range(20)
    ]
    ctx = builder.build(results)
    assert ctx.tokens_used <= ctx.tokens_budget


def test_chunks_appear_in_input_order(tmp_path: Path) -> None:
    """Builder preserves caller's ordering — does not re-sort."""
    builder = ContextBuilder(make_cfg(tmp_path))
    ctx = builder.build([
        make_result("z.md#0", "first body", file="10_facts/Z.md", final_score=0.9),
        make_result("a.md#0", "second body", file="10_facts/A.md", final_score=0.5),
    ])
    assert ctx.chunks_included == ["z.md#0", "a.md#0"]
    z_pos = ctx.text.index("10_facts/Z.md")
    a_pos = ctx.text.index("10_facts/A.md")
    assert z_pos < a_pos


# ---- Hermes-memory integration --------------------------------------------


def test_hermes_memory_emitted_when_enabled(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    mem.write_text("# Memory\n- I prefer concise replies.\n")
    usr = tmp_path / "USER.md"
    usr.write_text("# User\nGerman developer.\n")
    builder = ContextBuilder(make_cfg(
        tmp_path,
        include_hermes_memory=True,
        memory_path=mem,
        user_path=usr,
    ))
    ctx = builder.build([make_result("a.md#0", "vault body")])
    assert "## Hermes Memory" in ctx.text
    assert "## Hermes User Profile" in ctx.text
    assert ctx.hermes_memory_included
    assert ctx.hermes_user_included
    # Hermes content does not get a citation marker.
    mem_section = ctx.text.split("## Vault Hits", 1)[0]
    assert "[^" not in mem_section


def test_hermes_disabled_when_flag_false(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    mem.write_text("# Memory\nstuff\n")
    builder = ContextBuilder(make_cfg(
        tmp_path,
        include_hermes_memory=False,
        memory_path=mem,
    ))
    ctx = builder.build([make_result("a.md#0", "body")])
    assert "Hermes Memory" not in ctx.text
    assert not ctx.hermes_memory_included


def test_hermes_skipped_when_path_missing(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(
        tmp_path,
        include_hermes_memory=True,
        memory_path=tmp_path / "does-not-exist.md",
    ))
    ctx = builder.build([make_result("a.md#0", "body")])
    assert "Hermes Memory" not in ctx.text
    assert not ctx.hermes_memory_included


def test_hermes_atomic_skip_when_too_large_for_budget(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    huge = "memory " * 1000  # ~7000 chars → ~1750 tokens
    mem.write_text(huge)
    # Budget too small to fit MEMORY.md → must be skipped whole, not partial.
    builder = ContextBuilder(make_cfg(
        tmp_path,
        token_budget=200,
        include_hermes_memory=True,
        memory_path=mem,
    ))
    ctx = builder.build([make_result("a.md#0", "small body")])
    assert not ctx.hermes_memory_included
    assert "Hermes Memory" not in ctx.text
    # But the small vault chunk still fits.
    assert "a.md#0" in ctx.chunks_included


def test_hermes_deducts_from_same_budget(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    mem.write_text("memory blob " * 30)  # ~360 chars → ~90 tokens
    builder = ContextBuilder(make_cfg(
        tmp_path,
        token_budget=200,
        include_hermes_memory=True,
        memory_path=mem,
    ))
    ctx = builder.build([
        make_result("a.md#0", "small a"),
        make_result("b.md#0", "x" * 600, final_score=0.5),  # no longer fits
    ])
    assert ctx.hermes_memory_included
    assert "a.md#0" in ctx.chunks_included
    assert "b.md#0" in ctx.chunks_skipped_oversize
    assert ctx.tokens_used <= ctx.tokens_budget


# ---- budget_override -------------------------------------------------------


def test_budget_override_takes_precedence(tmp_path: Path) -> None:
    builder = ContextBuilder(make_cfg(tmp_path, token_budget=10000))
    body = "word " * 100
    # Override down to 50 tokens — bigger than tiny body? estimate:
    body_tokens = estimate_tokens(body)
    assert body_tokens > 50
    ctx = builder.build([make_result("a.md#0", body)], budget_override=50)
    assert ctx.chunks_included == []
    assert "a.md#0" in ctx.chunks_skipped_oversize
    assert ctx.tokens_budget == 50
