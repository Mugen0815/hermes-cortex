"""Tests for cortex.indexer."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from cortex.config import (
    Config,
    ContextBuilderConfig,
    EmbeddingsConfig,
    HermesMemoryConfig,
    IndexConfig,
    SearchConfig,
    VaultConfig,
)
from cortex.indexer import (
    MAX_CHUNK_CHARS,
    chunk_body,
    extract_wikilinks,
    index_vault,
    iter_vault_files,
    parse_frontmatter,
    parse_note,
    validate_frontmatter,
    write_chunks,
)


# ---- Helpers ---------------------------------------------------------------


def make_config(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config(
        vault=VaultConfig(
            path=vault,
            include_folders=["10_facts", "20_decisions", "30_projects", "60_maps"],
            exclude_folders=["00_inbox", "80_templates", "raw"],
        ),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(),
        search=SearchConfig(),
        context_builder=ContextBuilderConfig(),
    )


def write_note(vault: Path, rel: str, content: str, *, encoding: str = "utf-8") -> Path:
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content.encode(encoding))
    return p


SAMPLE_NOTE = dedent("""\
    ---
    type: fact
    status: active
    tags: [memory, architecture]
    confidence: high
    importance: high
    stability: stable
    last_verified: 2026-04-27
    related:
      - '[[Other Note]]'
    ---

    # Sample Note

    Intro text before any header.

    ## Section One

    Body of section one. Mentions [[Linked Note]].

    ```python
    # this should be ignored: [[Not A Real Link]]
    ```

    ## Section Two

    Section two body. References [[Linked Note]] again and [[Third Note]].
""")


# ---- parse_frontmatter -----------------------------------------------------


def test_parse_frontmatter_extracts_yaml() -> None:
    fm, body = parse_frontmatter(SAMPLE_NOTE)
    assert fm["type"] == "fact"
    assert fm["confidence"] == "high"
    assert body.lstrip().startswith("# Sample Note")


def test_parse_frontmatter_absent() -> None:
    text = "# No Frontmatter\n\nJust body."
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_invalid_yaml_returns_empty() -> None:
    text = "---\nthis: is: not: valid\n---\n\nbody"
    fm, body = parse_frontmatter(text)
    assert fm == {}


def test_parse_frontmatter_handles_crlf() -> None:
    """CRLF line endings (Windows / Obsidian sync) must be tolerated."""
    text = "---\r\ntype: fact\r\ntags: [a]\r\n---\r\n\r\nBody.\r\n"
    fm, body = parse_frontmatter(text)
    assert fm["type"] == "fact"
    assert "Body." in body
    assert "\r" not in body  # newlines normalized


def test_parse_frontmatter_handles_bom() -> None:
    text = "\ufeff---\ntype: fact\n---\n\nBody."
    fm, body = parse_frontmatter(text)
    assert fm["type"] == "fact"
    assert not body.startswith("\ufeff")


# ---- chunk_body ------------------------------------------------------------


def test_chunk_body_splits_on_h1_and_h2() -> None:
    body = "Pre intro.\n\n# A\n\nbody A\n\n## B\n\nbody B\n"
    sections = chunk_body(body)
    paths = [p for p, _ in sections]
    texts = [t for _, t in sections]
    assert paths == [[], ["A"], ["B"]]
    assert texts[0] == "Pre intro."
    assert "body A" in texts[1]
    assert "body B" in texts[2]


def test_chunk_body_no_headers() -> None:
    sections = chunk_body("just text, no headers")
    assert sections == [([], "just text, no headers")]


def test_chunk_body_empty_section_dropped() -> None:
    body = "## A\n\n## B\n\nbody B"
    sections = chunk_body(body)
    assert sections == [(["B"], "body B")]


def test_chunk_body_subsplits_long_section_on_h3() -> None:
    """A H2 section larger than MAX_CHUNK_CHARS should split on H3."""
    big_a = "lorem ipsum " * 200  # ~2400 chars
    big_b = "dolor sit " * 200
    body = f"## Top\n\nIntro under top.\n\n### Sub A\n\n{big_a}\n\n### Sub B\n\n{big_b}\n"
    sections = chunk_body(body, max_chars=MAX_CHUNK_CHARS)
    # We expect at least the two H3 sub-sections (intro may merge depending on size)
    paths = [p for p, _ in sections]
    assert ["Top", "Sub A"] in paths
    assert ["Top", "Sub B"] in paths


def test_chunk_body_paragraph_split_when_no_h3() -> None:
    """If a section is too big and has no H3, fall back to paragraph split."""
    paragraphs = [f"Paragraph {i}. " + ("foo bar " * 100) for i in range(5)]
    big = "\n\n".join(paragraphs)
    body = f"## Big Section\n\n{big}\n"
    sections = chunk_body(body, max_chars=MAX_CHUNK_CHARS)
    big_section_chunks = [t for p, t in sections if p == ["Big Section"]]
    # Should be split into multiple chunks
    assert len(big_section_chunks) > 1


# ---- extract_wikilinks -----------------------------------------------------


def test_extract_wikilinks_dedup_and_order() -> None:
    text = "See [[A]] and [[B]] and [[A]] again."
    assert extract_wikilinks(text) == ["A", "B"]


def test_extract_wikilinks_strips_pipe_and_anchor() -> None:
    text = "Refs [[Target|Display]] and [[Other#Heading]] and [[Third|Alt#Sec]]."
    assert extract_wikilinks(text) == ["Target", "Other", "Third"]


def test_extract_wikilinks_ignores_code_fences() -> None:
    text = "Real [[A]].\n\n```\n[[InCode]]\n```\n\nMore [[B]]."
    assert extract_wikilinks(text) == ["A", "B"]


# ---- validate_frontmatter --------------------------------------------------


def test_validate_frontmatter_complete() -> None:
    fm = {
        "type": "fact", "status": "active", "tags": [],
        "confidence": "high", "importance": "high", "stability": "stable",
    }
    assert validate_frontmatter(fm) == []


def test_validate_frontmatter_missing_fields() -> None:
    fm = {"type": "fact", "tags": []}
    missing = validate_frontmatter(fm)
    assert "confidence" in missing
    assert "stability" in missing
    assert "type" not in missing


def test_validate_frontmatter_empty() -> None:
    assert validate_frontmatter({}) == sorted(
        ["type", "status", "tags", "confidence", "importance", "stability"]
    )


# ---- parse_note ------------------------------------------------------------


def test_parse_note_full(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    f = write_note(cfg.vault.path, "10_facts/Sample.md", SAMPLE_NOTE)
    chunks, raw_fm, norm = parse_note(f, cfg.vault.path)

    assert raw_fm["type"] == "fact"
    # H1 is now a hard boundary, so we get: H1 "Sample Note" + 2 H2 sections.
    assert len(chunks) == 3
    assert chunks[0].heading == "Sample Note"
    assert chunks[0].heading_path == ["Sample Note"]
    assert chunks[1].heading == "Section One"
    assert chunks[1].heading_path == ["Section One"]
    assert chunks[2].heading == "Section Two"

    # IDs are slug-based, deterministic, NOT positional
    assert chunks[0].id == "10_facts/Sample.md#sample-note"
    assert chunks[1].id == "10_facts/Sample.md#section-one"
    assert chunks[2].id == "10_facts/Sample.md#section-two"
    assert chunks[0].folder == "10_facts"
    assert chunks[0].file == "10_facts/Sample.md"

    # Intro text under H1 should be in the H1 chunk's body.
    assert "Intro text" in chunks[0].text

    # wikilinks: section 1 has Linked Note, plus "Other Note" from frontmatter related
    s1_links = chunks[1].wikilinks
    assert "Linked Note" in s1_links
    assert "Other Note" in s1_links
    assert "Not A Real Link" not in s1_links

    # tags propagated from frontmatter
    assert chunks[0].tags == ["memory", "architecture"]

    # date in frontmatter coerced to string in raw view
    assert chunks[0].frontmatter["last_verified"] == "2026-04-27"

    # normalized view: numeric confidence/importance, ISO date
    assert isinstance(chunks[0].fm_normalized["confidence"], float)
    assert isinstance(chunks[0].fm_normalized["importance"], float)
    assert chunks[0].fm_normalized["last_verified"] == "2026-04-27"

    # length stats
    assert chunks[1].char_len == len(chunks[1].text)
    assert chunks[1].token_estimate >= 1


def test_parse_note_chunk_ids_stable_under_edits(tmp_path: Path) -> None:
    """Editing one section's body must NOT change other sections' IDs.

    This is the critical invariant for Phase-3 caching/citations.
    """
    cfg = make_config(tmp_path)
    note_v1 = "## Alpha\n\noriginal alpha\n\n## Beta\n\noriginal beta\n"
    f = write_note(cfg.vault.path, "10_facts/N.md", note_v1)
    chunks_v1, _, _ = parse_note(f, cfg.vault.path)
    ids_v1 = {c.heading: c.id for c in chunks_v1}

    note_v2 = "## Alpha\n\nedited alpha content\n\n## Beta\n\noriginal beta\n"
    write_note(cfg.vault.path, "10_facts/N.md", note_v2)
    chunks_v2, _, _ = parse_note(f, cfg.vault.path)
    ids_v2 = {c.heading: c.id for c in chunks_v2}

    assert ids_v1 == ids_v2
    # Beta's ID must NOT have changed even though alpha changed.
    assert ids_v1["Beta"] == ids_v2["Beta"]


def test_parse_note_chunk_id_collision_suffix(tmp_path: Path) -> None:
    """Duplicate headings get -2, -3 suffixes."""
    cfg = make_config(tmp_path)
    note = "## Notes\n\nfirst\n\n## Notes\n\nsecond\n\n## Notes\n\nthird\n"
    f = write_note(cfg.vault.path, "10_facts/Dup.md", note)
    chunks, _, _ = parse_note(f, cfg.vault.path)
    ids = [c.id for c in chunks]
    assert ids == [
        "10_facts/Dup.md#notes",
        "10_facts/Dup.md#notes-2",
        "10_facts/Dup.md#notes-3",
    ]


def test_parse_note_tags_as_string(tmp_path: Path) -> None:
    """Frontmatter tags as comma-separated string must coerce to list."""
    note = (
        "---\n"
        "type: fact\n"
        "status: active\n"
        "tags: memory, architecture\n"
        "confidence: high\n"
        "importance: high\n"
        "stability: stable\n"
        "---\n\n# T\n\nbody\n"
    )
    cfg = make_config(tmp_path)
    f = write_note(cfg.vault.path, "10_facts/T.md", note)
    chunks, _, norm = parse_note(f, cfg.vault.path)
    assert sorted(norm.tags) == ["architecture", "memory"]
    assert sorted(chunks[0].tags) == ["architecture", "memory"]


def test_parse_note_numeric_frontmatter_passthrough(tmp_path: Path) -> None:
    note = (
        "---\n"
        "type: fact\n"
        "status: active\n"
        "tags: []\n"
        "confidence: 0.8\n"
        "importance: 4\n"
        "stability: stable\n"
        "---\n\n# T\n\nbody\n"
    )
    cfg = make_config(tmp_path)
    f = write_note(cfg.vault.path, "10_facts/N.md", note)
    chunks, _, norm = parse_note(f, cfg.vault.path)
    assert norm.confidence == pytest.approx(0.8)
    assert norm.importance == pytest.approx(4.0)
    assert chunks[0].fm_normalized["confidence"] == pytest.approx(0.8)


# ---- iter_vault_files ------------------------------------------------------


def test_iter_vault_files_respects_include_exclude(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/A.md", "x")
    write_note(cfg.vault.path, "00_inbox/B.md", "x")
    write_note(cfg.vault.path, "80_templates/C.md", "x")
    write_note(cfg.vault.path, "60_maps/D.md", "x")
    write_note(cfg.vault.path, "raw/articles/Source.md", "x")
    write_note(cfg.vault.path, "SCHEMA.md", "x")
    write_note(cfg.vault.path, "index.md", "x")
    write_note(cfg.vault.path, "log.md", "x")
    write_note(cfg.vault.path, "README.md", "x")

    files = sorted(p.name for p in iter_vault_files(cfg))
    assert files == ["A.md", "D.md"]


def test_iter_vault_files_skips_obsidian_dir(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/A.md", "x")
    write_note(cfg.vault.path, ".obsidian/workspace.md", "internal")
    files = [p.name for p in iter_vault_files(cfg)]
    assert "workspace.md" not in files


# ---- index_vault end-to-end -----------------------------------------------


def test_index_vault_writes_chunks(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/Sample.md", SAMPLE_NOTE)
    write_note(cfg.vault.path, "10_facts/NoFrontmatter.md", "# Bare\n\nNo frontmatter here.")

    report = index_vault(cfg)

    assert report.indexed_files == 2
    assert report.chunks_written >= 3
    assert "10_facts/NoFrontmatter.md" in report.notes_missing_frontmatter

    lines = cfg.index.chunks_path.read_text().splitlines()
    objs = [json.loads(line) for line in lines]
    files = {o["file"] for o in objs}
    assert files == {"10_facts/Sample.md", "10_facts/NoFrontmatter.md"}

    # Every chunk must carry the new schema fields
    for o in objs:
        assert "heading_path" in o
        assert "fm_normalized" in o
        assert "modified_date" in o
        assert "char_len" in o
        assert "token_estimate" in o


def test_index_vault_atomic_write(tmp_path: Path) -> None:
    """No leftover .tmp file after a successful write."""
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/A.md", SAMPLE_NOTE)
    index_vault(cfg)
    tmp_file = cfg.index.chunks_path.with_suffix(cfg.index.chunks_path.suffix + ".tmp")
    assert not tmp_file.exists()
    assert cfg.index.chunks_path.exists()


def test_index_vault_incremental_skips_unchanged(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/A.md", SAMPLE_NOTE)

    r1 = index_vault(cfg)
    assert r1.indexed_files == 1
    assert r1.skipped_unchanged == 0

    r2 = index_vault(cfg)
    assert r2.indexed_files == 0
    assert r2.skipped_unchanged == 1
    assert cfg.index.chunks_path.read_text().strip() != ""


def test_index_vault_crlf_does_not_invalidate_cache(tmp_path: Path) -> None:
    """A file rewritten with CRLF endings must NOT trigger re-indexing.

    Because content_hash is computed on the LF-normalized form.
    """
    cfg = make_config(tmp_path)
    p = write_note(cfg.vault.path, "10_facts/A.md", SAMPLE_NOTE)
    index_vault(cfg)

    # Rewrite same content but with CRLF endings.
    p.write_bytes(SAMPLE_NOTE.replace("\n", "\r\n").encode("utf-8"))
    r2 = index_vault(cfg)
    assert r2.indexed_files == 0
    assert r2.skipped_unchanged == 1


def test_index_vault_detects_changed_file(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    p = write_note(cfg.vault.path, "10_facts/A.md", SAMPLE_NOTE)
    index_vault(cfg)

    p.write_text(SAMPLE_NOTE + "\n## Section Three\n\nNew section.\n")
    r2 = index_vault(cfg)
    assert r2.indexed_files == 1
    assert r2.skipped_unchanged == 0

    objs = [json.loads(line) for line in cfg.index.chunks_path.read_text().splitlines()]
    headings = [o["heading"] for o in objs if o["file"] == "10_facts/A.md"]
    assert "Section Three" in headings


def test_index_vault_detects_deleted_file(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    a = write_note(cfg.vault.path, "10_facts/A.md", SAMPLE_NOTE)
    write_note(cfg.vault.path, "10_facts/B.md", SAMPLE_NOTE)
    index_vault(cfg)

    a.unlink()
    r2 = index_vault(cfg)
    assert r2.removed_files == 1
    objs = [json.loads(line) for line in cfg.index.chunks_path.read_text().splitlines()]
    files = {o["file"] for o in objs}
    assert files == {"10_facts/B.md"}


def test_index_vault_force_reindex(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/A.md", SAMPLE_NOTE)
    index_vault(cfg)

    r2 = index_vault(cfg, force=True)
    assert r2.indexed_files == 1
    assert r2.skipped_unchanged == 0


def test_index_vault_records_frontmatter_warnings(tmp_path: Path) -> None:
    """Unknown enum values produce warnings (not errors) in the report."""
    note = (
        "---\n"
        "type: weird-type-not-in-vocab\n"
        "status: active\n"
        "tags: [a]\n"
        "confidence: high\n"
        "importance: high\n"
        "stability: stable\n"
        "---\n\n# T\n\nbody\n"
    )
    cfg = make_config(tmp_path)
    write_note(cfg.vault.path, "10_facts/W.md", note)
    report = index_vault(cfg)
    files_with_warnings = {f for f, _ in report.notes_with_warnings}
    assert "10_facts/W.md" in files_with_warnings


# ---- write_chunks ---------------------------------------------------------


def test_write_chunks_atomic_no_tmp_leftover(tmp_path: Path) -> None:
    target = tmp_path / "out" / "chunks.jsonl"
    write_chunks(target, [{"id": "a"}, {"id": "b"}])
    assert target.exists()
    assert not (target.parent / (target.name + ".tmp")).exists()
    lines = target.read_text().splitlines()
    assert len(lines) == 2
