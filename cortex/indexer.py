"""Markdown vault indexer for hermes-cortex.

Reads notes from the configured vault, splits them into header-based chunks,
extracts frontmatter + wikilinks, and writes chunks.jsonl atomically.

Pipeline:
    vault/*.md  →  parse_note()  →  chunk_body()  →  chunks.jsonl

Key design decisions (locked for Phase 3 compatibility):

- **Heading boundaries**: H1 (``#``) and H2 (``##``) are HARD chunk
  boundaries. Sections longer than ``MAX_CHUNK_CHARS`` are sub-split on H3
  (``###``); if that still leaves an oversize sub-section, we fall back to
  a paragraph-level split. Code fences are kept intact.

- **Stable chunk IDs**: ``<file>#<heading-path-slug>`` plus an
  occurrence-index suffix (``-2``, ``-3``…) on collisions. The pre-first-
  heading chunk is ``<file>#intro``. IDs do NOT depend on content hashes,
  so small textual edits keep the ID stable.

- **CRLF / BOM tolerant**: all text is normalized to LF and BOM stripped
  before any regex runs.

- **Atomic writes**: ``chunks.jsonl`` is written to ``chunks.jsonl.tmp``
  then renamed, so a crash mid-write can never leave a partial file.

- **Frontmatter normalization** lives in ``cortex.frontmatter`` — this
  module just calls ``normalize()`` and stores both the canonical view
  (typed fields) and the raw dict (for citations).

Incrementality:
    A whole-file SHA-256 of the *normalized* (LF) source decides if a file
    needs re-chunking. Unchanged files keep their existing chunks verbatim.
    Deleted files have their chunks dropped. Changed files are fully
    re-chunked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import yaml

from cortex.config import Config
from cortex.frontmatter import (
    NormalizedFrontmatter,
    missing_required,
    normalize as normalize_frontmatter,
)
from cortex.text import estimate_tokens, slugify

log = logging.getLogger("cortex.indexer")


# ---- Tunables --------------------------------------------------------------

# Soft maximum chars per chunk before we sub-split on H3 / paragraphs.
# 2000 chars ≈ 500-600 tokens, comfortably below most embedding models' limits.
MAX_CHUNK_CHARS = 2000

# When even paragraph-splitting can't get a chunk under the soft cap, we still
# keep the chunk (no hard truncation here — that's the embedder's job to warn
# about), but we flag it in the IndexReport.


# ---- Regex (operate on LF-normalized text) --------------------------------

# Frontmatter delimiter. We accept optional leading whitespace on the closing
# fence line to be lenient with hand-edited files.
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)

# Wikilinks: [[Target]] or [[Target|Display]] or [[Target#Section]] — capture target.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")

# Heading detection. We only treat ATX-style headings at line start (with up
# to 3 leading spaces, per CommonMark) as boundaries. We deliberately ignore
# headings inside code fences by stripping fences first.
_H1_RE = re.compile(r"^ {0,3}#\s+(.+?)\s*$", re.MULTILINE)
_H2_RE = re.compile(r"^ {0,3}##\s+(.+?)\s*$", re.MULTILINE)
_H3_RE = re.compile(r"^ {0,3}###\s+(.+?)\s*$", re.MULTILINE)
# Combined H1+H2 for primary chunk boundaries (we capture level + text).
_H1_OR_H2_RE = re.compile(r"^ {0,3}(#{1,2})\s+(.+?)\s*$", re.MULTILINE)

# Code fences (opening + closing ```), greedy-but-non-overlapping.
_CODE_FENCE_RE = re.compile(r"```.*?\n.*?```", re.DOTALL)


# ---- Data types ------------------------------------------------------------


@dataclass
class Chunk:
    id: str
    file: str               # vault-relative posix path
    folder: str             # top-level folder under vault
    heading_path: list[str] # e.g. ["Section One", "Subsection A"]; [] for intro
    heading: Optional[str]  # leaf heading (last in path); None for intro chunk
    text: str
    tags: list[str] = field(default_factory=list)
    wikilinks: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)  # raw, JSON-safe
    fm_normalized: dict[str, Any] = field(default_factory=dict)  # canonical view
    modified: str = ""        # ISO datetime of file mtime (with seconds!)
    modified_date: str = ""   # ISO date for cheap day-level filtering
    content_hash: str = ""    # sha256 of source file (LF-normalized)
    char_len: int = 0
    token_estimate: int = 0

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Unserializable: {type(o).__name__}")


@dataclass
class IndexReport:
    indexed_files: int = 0
    skipped_unchanged: int = 0
    removed_files: int = 0
    chunks_written: int = 0
    chunks_oversize: int = 0
    notes_missing_frontmatter: list[str] = field(default_factory=list)
    notes_invalid_frontmatter: list[tuple[str, list[str]]] = field(default_factory=list)
    notes_with_warnings: list[tuple[str, list[str]]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Indexed {self.indexed_files} files "
            f"({self.chunks_written} chunks, {self.chunks_oversize} oversize), "
            f"skipped {self.skipped_unchanged} unchanged, "
            f"removed {self.removed_files} deleted. "
            f"Issues: {len(self.notes_missing_frontmatter)} missing frontmatter, "
            f"{len(self.notes_invalid_frontmatter)} incomplete, "
            f"{len(self.notes_with_warnings)} with warnings, "
            f"{len(self.errors)} errors."
        )


# ---- Text normalization ----------------------------------------------------


def _normalize_newlines(text: str) -> str:
    """Strip BOM, convert CRLF/CR to LF. Idempotent."""
    if not text:
        return ""
    if text.startswith("\ufeff"):
        text = text[1:]
    # \r\n → \n, then any remaining lone \r → \n
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


# ---- Parsing ---------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter dict, body). Empty dict if absent.

    Tolerant to BOM and CRLF: caller should pre-normalize, but we also normalize
    here defensively so direct callers (tests) don't have to.
    """
    text = _normalize_newlines(text)
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    body = text[m.end():]
    return fm, body


def extract_wikilinks(text: str) -> list[str]:
    """Return wikilink targets, deduplicated, in order of first appearance.

    Code fences are stripped first so ``[[Foo]]`` inside a code block doesn't
    become a graph edge.
    """
    stripped = _CODE_FENCE_RE.sub("", text)
    seen: dict[str, None] = {}
    for m in _WIKILINK_RE.finditer(stripped):
        target = m.group(1).strip()
        if target and target not in seen:
            seen[target] = None
    return list(seen.keys())


# ---- Chunking --------------------------------------------------------------


def _mask_code_fences(text: str) -> str:
    """Replace code-fence regions with same-length whitespace.

    Preserves offsets so we can run heading regexes on the masked string and
    then slice the *original* text by the matched positions. We zero out the
    fence content (replacing every char with a space, keeping newlines) so
    headings inside ```...``` blocks aren't picked up as chunk boundaries.
    """
    if "```" not in text:
        return text
    out = list(text)
    for m in _CODE_FENCE_RE.finditer(text):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def _split_on_heading(body: str, heading_re: re.Pattern[str]) -> list[tuple[Optional[str], str]]:
    """Generic split: returns (heading or None, section_text) pairs.

    The pre-first-heading prefix gets heading=None. Empty sections are dropped.
    Code fences are masked so headings inside them never act as boundaries.
    """
    masked = _mask_code_fences(body)
    matches = list(heading_re.finditer(masked))
    if not matches:
        text = body.strip()
        return [(None, text)] if text else []

    out: list[tuple[Optional[str], str]] = []
    pre = body[: matches[0].start()].strip()
    if pre:
        out.append((None, pre))
    for i, m in enumerate(matches):
        # Use the LAST capture group (heading text). Works for both _H1_RE-style
        # (1 group) and _H1_OR_H2_RE-style (2 groups; level + text).
        heading = m.group(m.lastindex or 1).strip() if m.lastindex else m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        # Slice from ORIGINAL body so code-fence contents are preserved.
        text = body[start:end].strip()
        if text:
            out.append((heading, text))
    return out


def _paragraph_split(text: str, max_chars: int) -> list[str]:
    """Last-resort split: by blank-line paragraphs, accumulating up to max_chars."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return [text] if text.strip() else []
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for p in paras:
        plen = len(p)
        if buf and buf_len + plen + 2 > max_chars:
            out.append("\n\n".join(buf))
            buf, buf_len = [p], plen
        else:
            buf.append(p)
            buf_len += plen + 2
    if buf:
        out.append("\n\n".join(buf))
    return out


def chunk_body(body: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[tuple[list[str], str]]:
    """Split body into (heading_path, section_text) pairs.

    Heading path is a list of strings; ``[]`` means the pre-first-heading
    intro chunk. Splitting strategy:

      1. Split on H1+H2 (hard boundaries).
      2. Any section larger than ``max_chars`` is sub-split on H3.
      3. Any H3 sub-section still over ``max_chars`` is paragraph-split.

    Empty sections are dropped.
    """
    primary = _split_on_heading(body, _H1_OR_H2_RE)
    out: list[tuple[list[str], str]] = []
    for heading, section in primary:
        path = [heading] if heading else []
        if len(section) <= max_chars:
            out.append((path, section))
            continue
        # Need to sub-split. Try H3 first.
        h3_parts = _split_on_heading(section, _H3_RE)
        # If H3 split didn't change anything (no H3 in section), fall straight
        # to paragraph split.
        if len(h3_parts) <= 1:
            for sub in _paragraph_split(section, max_chars):
                out.append((path, sub))
            continue
        for sub_heading, sub_text in h3_parts:
            sub_path = path + ([sub_heading] if sub_heading else [])
            if len(sub_text) <= max_chars:
                out.append((sub_path, sub_text))
            else:
                for piece in _paragraph_split(sub_text, max_chars):
                    out.append((sub_path, piece))
    return out


def validate_frontmatter(fm: dict[str, Any]) -> list[str]:
    """Return list of missing required fields. Empty list = valid.

    Thin wrapper around ``cortex.frontmatter.missing_required`` — kept here
    for backward compatibility with existing imports.
    """
    return missing_required(fm)


# ---- ID assignment ---------------------------------------------------------


def _build_chunk_id(file_rel: str, heading_path: list[str], occurrence: int) -> str:
    """Build a stable chunk ID: ``<file>#<slug>`` (+ ``-N`` on collision).

    ``occurrence`` is 1-based. The first occurrence is unsuffixed; the second
    becomes ``-2``, etc. Guarantees:
      - Independent of chunk content (no content hash).
      - Independent of chunk index in the file (insertions above don't shift IDs).
      - Stable across runs for the same heading-path occurrence.
    """
    if not heading_path:
        slug = "intro"
    else:
        slug = "/".join(slugify(h) for h in heading_path)
    suffix = "" if occurrence <= 1 else f"-{occurrence}"
    return f"{file_rel}#{slug}{suffix}"


def _assign_ids(file_rel: str, sections: list[tuple[list[str], str]]) -> list[str]:
    """Map each (heading_path, text) to a unique chunk ID with collision suffix."""
    counts: dict[str, int] = {}
    ids: list[str] = []
    for path, _ in sections:
        # Use heading-path slug (without occurrence) as the dedup key.
        if not path:
            base_slug = "intro"
        else:
            base_slug = "/".join(slugify(h) for h in path)
        counts[base_slug] = counts.get(base_slug, 0) + 1
        ids.append(_build_chunk_id(file_rel, path, counts[base_slug]))
    return ids


# ---- Note → chunks ---------------------------------------------------------


def parse_note(file: Path, vault_root: Path) -> tuple[list[Chunk], dict[str, Any], NormalizedFrontmatter]:
    """Read a single note file and convert to chunks.

    Returns (chunks, raw_frontmatter, normalized_frontmatter). Caller decides
    what to do with notes that have missing/invalid frontmatter — we still
    produce chunks so partially annotated notes remain searchable.
    """
    raw_text = _normalize_newlines(file.read_text(encoding="utf-8"))
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    fm, body = parse_frontmatter(raw_text)
    norm = normalize_frontmatter(fm)

    rel = file.relative_to(vault_root).as_posix()
    folder = rel.split("/", 1)[0] if "/" in rel else ""
    stat = file.stat()
    mtime_dt = datetime.fromtimestamp(stat.st_mtime)
    mtime_iso = mtime_dt.replace(microsecond=0).isoformat()
    mtime_date = mtime_dt.date().isoformat()

    sections = chunk_body(body)
    ids = _assign_ids(rel, sections)

    # Frontmatter-derived signals propagated to every chunk.
    fm_tags = list(norm.tags)
    fm_related_links: list[str] = []
    for r in norm.related:
        # `related` entries can themselves be wikilinks; pull targets out.
        for link in extract_wikilinks(r):
            if link not in fm_related_links:
                fm_related_links.append(link)
        # If it's a bare string (no [[...]]), keep as-is.
        if "[[" not in r and r not in fm_related_links:
            fm_related_links.append(r.strip())

    chunks: list[Chunk] = []
    for cid, (path, text) in zip(ids, sections):
        wikilinks = extract_wikilinks(text)
        for link in fm_related_links:
            if link not in wikilinks:
                wikilinks.append(link)
        chunks.append(
            Chunk(
                id=cid,
                file=rel,
                folder=folder,
                heading_path=list(path),
                heading=path[-1] if path else None,
                text=text,
                tags=list(fm_tags),
                wikilinks=wikilinks,
                frontmatter=dict(norm.raw),
                fm_normalized=_normalized_to_dict(norm),
                modified=mtime_iso,
                modified_date=mtime_date,
                content_hash=content_hash,
                char_len=len(text),
                token_estimate=estimate_tokens(text),
            )
        )
    return chunks, fm, norm


def _normalized_to_dict(n: NormalizedFrontmatter) -> dict[str, Any]:
    """Project NormalizedFrontmatter into a plain dict (for chunks.jsonl)."""
    return {
        "type": n.type,
        "status": n.status,
        "domain": n.domain,
        "project": n.project,
        "stability": n.stability,
        "tags": list(n.tags),
        "confidence": n.confidence,
        "importance": n.importance,
        "last_verified": n.last_verified,
        "created": n.created,
        "related": list(n.related),
    }


# ---- Vault traversal -------------------------------------------------------


# Folders we always skip regardless of include/exclude config.
_ALWAYS_SKIP_DIRS = {".obsidian", ".git", ".trash", "node_modules", ".venv"}


def iter_vault_files(cfg: Config) -> Iterator[Path]:
    """Yield .md files honoring include_folders / exclude_folders.

    Always skips dotfiles (``.foo.md``), the vault README, and any file under
    ``_ALWAYS_SKIP_DIRS`` (.obsidian, .git, ...).
    """
    vault = cfg.vault.path
    include = set(cfg.vault.include_folders)
    exclude = set(cfg.vault.exclude_folders)

    if not vault.exists():
        log.warning("Vault path does not exist: %s", vault)
        return

    for md in sorted(vault.rglob("*.md")):
        rel = md.relative_to(vault).as_posix()
        if rel == "README.md":
            continue
        # Skip dotfiles and any segment in the always-skip set.
        parts = rel.split("/")
        if any(p.startswith(".") for p in parts):
            continue
        if any(p in _ALWAYS_SKIP_DIRS for p in parts[:-1]):
            continue
        top = parts[0] if len(parts) > 1 else ""
        if include and top not in include:
            continue
        if top in exclude:
            continue
        yield md


# ---- Index store (chunks.jsonl) -------------------------------------------


def load_existing_chunks(chunks_path: Path) -> list[dict[str, Any]]:
    if not chunks_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with chunks_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_chunks(chunks_path: Path, chunks: Iterable[Chunk | dict[str, Any]]) -> int:
    """Atomic write: serialize to ``<path>.tmp``, then rename over the target.

    Returns the number of records written. A crash mid-write leaves either
    the previous file intact or no file at all — never a partial one.
    """
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = chunks_path.with_suffix(chunks_path.suffix + ".tmp")
    n = 0
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            for c in chunks:
                if isinstance(c, Chunk):
                    f.write(c.to_json_line())
                else:
                    f.write(json.dumps(c, ensure_ascii=False, default=_json_default))
                f.write("\n")
                n += 1
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, chunks_path)
    finally:
        # If something blew up before the rename, clean up the tmp file.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return n


# ---- Top-level: index a vault ---------------------------------------------


def index_vault(cfg: Config, *, force: bool = False) -> IndexReport:
    """Index the vault into chunks.jsonl. Incremental by default.

    ``force=True`` ignores existing hashes and re-indexes everything.
    """
    report = IndexReport()
    chunks_path = cfg.index.chunks_path

    existing_chunks = [] if force else load_existing_chunks(chunks_path)
    existing_hashes: dict[str, str] = {}
    if not force:
        for c in existing_chunks:
            f_ = c.get("file")
            h = c.get("content_hash")
            if f_ and h:
                existing_hashes[f_] = h

    seen_files: set[str] = set()
    new_chunks: list[Chunk] = []
    kept_chunks: list[dict[str, Any]] = []

    existing_by_file: dict[str, list[dict[str, Any]]] = {}
    for c in existing_chunks:
        existing_by_file.setdefault(c.get("file", ""), []).append(c)

    for md in iter_vault_files(cfg):
        rel = md.relative_to(cfg.vault.path).as_posix()
        seen_files.add(rel)
        try:
            # Compute hash on the LF-normalized form so CRLF↔LF edits don't
            # spuriously invalidate caches.
            current_hash = hashlib.sha256(
                _normalize_newlines(md.read_text(encoding="utf-8")).encode("utf-8")
            ).hexdigest()
            if not force and existing_hashes.get(rel) == current_hash:
                kept_chunks.extend(existing_by_file.get(rel, []))
                report.skipped_unchanged += 1
                continue

            chunks, raw_fm, norm = parse_note(md, cfg.vault.path)
            missing = missing_required(raw_fm)
            if not raw_fm:
                report.notes_missing_frontmatter.append(rel)
            elif missing:
                report.notes_invalid_frontmatter.append((rel, missing))
            if norm.warnings:
                report.notes_with_warnings.append((rel, list(norm.warnings)))

            for c in chunks:
                if c.char_len > MAX_CHUNK_CHARS:
                    report.chunks_oversize += 1

            new_chunks.extend(chunks)
            report.indexed_files += 1
        except Exception as e:  # noqa: BLE001
            report.errors.append((rel, str(e)))
            log.exception("Failed to index %s", rel)

    # Detect deletions: files in existing but not seen this run.
    for old_file in existing_by_file:
        if old_file not in seen_files:
            report.removed_files += 1

    all_chunks: list[Chunk | dict[str, Any]] = list(kept_chunks) + list(new_chunks)
    report.chunks_written = write_chunks(chunks_path, all_chunks)
    return report
