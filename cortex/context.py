"""Context builder for hermes-cortex — Phase 4.

Turns a list of :class:`SearchResult` (from :class:`HybridSearcher`) into a
single Markdown blob suitable for LLM injection, under a token budget.

Design decisions (locked):

* **Token counting**: cheap char-based estimate via
  :func:`cortex.text.estimate_tokens` (≈ ``len(text) / 4``). No external
  tokenizer dependency; ~10–20% off in either direction. The
  :class:`ContextBuilderConfig` tells us the budget; we treat it as an
  *upper bound* — overshoot is forbidden, undershoot is normal.
* **Budget enforcement**: smart skip, no truncation. Walk the
  search-result list in (final_score desc, chunk_id asc) order. For each
  candidate, if it fits the remaining budget include it; otherwise skip
  it and try the next one. Order of *included* chunks preserves the
  ranking — we never re-shuffle to "fit better".
* **Citation placement**: a citation marker (``[^N]``) is rendered
  **only in the chunk's header line**, never injected into the body.
  The original ``chunk["text"]`` is emitted verbatim. The bibliography
  at the end maps marker → ``file :: heading_path`` (and optional
  metadata like ``score``).
* **Hermes-memory integration**: when
  ``cfg.context_builder.include_hermes_memory`` is true, MEMORY.md and
  USER.md (paths from :class:`HermesMemoryConfig`) are read and emitted
  in dedicated sections at the **top** of the output. Their token cost
  is deducted from the same budget *before* vault chunks are
  considered. SOUL.md is intentionally excluded — it's persona config,
  not retrieval context.

Output shape::

    # Context

    ## Hermes Memory                  (only if include_hermes_memory)
    <MEMORY.md verbatim>

    ## Hermes User Profile            (only if include_hermes_memory)
    <USER.md verbatim>

    ## Vault Hits

    ### [^1] 10_facts/Foo.md :: Section A / Subsection
    <chunk text verbatim>

    ### [^2] 20_decisions/Bar.md :: Decision header
    <chunk text verbatim>

    ## Citations
    [^1]: 10_facts/Foo.md  ::  Section A / Subsection  (score=0.0421)
    [^2]: 20_decisions/Bar.md  ::  Decision header  (score=0.0398)

The Hermes sections do NOT receive citation markers — they are framed
as ambient context, not retrieved evidence. They also do not appear in
the bibliography.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from cortex.config import Config
from cortex.text import estimate_tokens

log = logging.getLogger("cortex.context")


# ---- Result ----------------------------------------------------------------


@dataclass
class BuiltContext:
    """The materialized context blob plus diagnostic info.

    ``chunks_included`` are the chunk_ids that made it into the output,
    in the order they appear. ``chunks_skipped_oversize`` lists ids
    dropped purely for budget reasons (the chunk's token cost exceeded
    the *remaining* budget at evaluation time).

    ``tokens_used`` is the running estimate of what was actually
    rendered. ``tokens_budget`` is the configured ceiling.
    """

    text: str
    tokens_used: int
    tokens_budget: int
    chunks_included: list[str] = field(default_factory=list)
    chunks_skipped_oversize: list[str] = field(default_factory=list)
    hermes_memory_included: bool = False
    hermes_user_included: bool = False
    citation_count: int = 0


# ---- Helpers ---------------------------------------------------------------


def _read_text_file(path: Optional[Path]) -> Optional[str]:
    """Read a text file. Returns None on missing / unreadable / empty."""
    if path is None:
        return None
    try:
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("Could not read %s: %s", path, e)
        return None
    text = text.strip()
    return text or None


def _heading_label(chunk: dict[str, Any]) -> str:
    """Render the heading_path joined with ' / ', or '(intro)' for top chunks."""
    hp = chunk.get("heading_path") or []
    parts = [p for p in hp if p]
    return " / ".join(parts) if parts else "(intro)"


def _chunk_token_cost(chunk: dict[str, Any], header_overhead: int) -> int:
    """Estimated tokens this chunk would contribute if included.

    We re-estimate the body rather than trusting ``chunk["token_estimate"]``
    so the budget math stays honest even if the indexer's estimate
    drifts. ``header_overhead`` accounts for the markdown header line and
    the citation marker glue.
    """
    body = (chunk.get("text") or "")
    return estimate_tokens(body) + header_overhead


# ---- Builder ---------------------------------------------------------------


class ContextBuilder:
    """Build a Markdown context blob from search results, under budget.

    Stateless; safe to instantiate per call.
    """

    # Constants used to estimate per-element overhead. Worst-case-ish so the
    # builder *under*-fills the budget rather than overflowing it.
    _CHUNK_HEADER_TOKENS = 12      # "### [^N] file :: heading\n\n"
    _CITATION_LINE_TOKENS = 14     # one bibliography entry
    _SECTION_HEADING_TOKENS = 4    # "## ...\n\n"
    _DOC_HEADING_TOKENS = 3        # "# Context\n\n"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    # ---- Public API ------------------------------------------------------

    def build(
        self,
        results: Sequence[Any],
        *,
        budget_override: Optional[int] = None,
    ) -> BuiltContext:
        """Render a context blob.

        ``results``: iterable of :class:`cortex.search.SearchResult` (we
        only access ``.chunk_id``, ``.chunk``, ``.final_score``). Empty
        list is fine — we still emit the doc heading and Hermes sections
        (if configured).

        ``budget_override``: optional caller-supplied budget. Useful for
        testing or for callers that want to allocate from a parent
        budget. Defaults to ``cfg.context_builder.token_budget``.
        """
        cb_cfg = self.cfg.context_builder
        budget = int(budget_override if budget_override is not None else cb_cfg.token_budget)
        if budget <= 0:
            return BuiltContext(text="", tokens_used=0, tokens_budget=budget)

        tokens_used = self._DOC_HEADING_TOKENS
        body_lines: list[str] = ["# Context", ""]

        ctx = BuiltContext(text="", tokens_used=0, tokens_budget=budget)

        # ---- Hermes sections (top, deduct from same budget) ----
        if cb_cfg.include_hermes_memory:
            tokens_used = self._maybe_emit_hermes_section(
                title="Hermes Memory",
                path=self.cfg.hermes_memory.memory_path,
                budget=budget,
                tokens_used=tokens_used,
                lines=body_lines,
                set_flag=lambda: setattr(ctx, "hermes_memory_included", True),
            )
            tokens_used = self._maybe_emit_hermes_section(
                title="Hermes User Profile",
                path=self.cfg.hermes_memory.user_path,
                budget=budget,
                tokens_used=tokens_used,
                lines=body_lines,
                set_flag=lambda: setattr(ctx, "hermes_user_included", True),
            )

        # ---- Vault hits ----
        # Keep the input order — caller is responsible for ranking. We only
        # *skip* oversized chunks; we never re-sort.
        # Reserve overhead for the two section headings ("## Vault Hits",
        # "## Citations") up-front so the budget arithmetic stays correct
        # for the very first chunk we consider.
        section_overhead_pending = 2 * self._SECTION_HEADING_TOKENS
        rendered_chunks: list[tuple[int, Any]] = []  # (citation_id, result)
        for res in results:
            chunk = res.chunk if hasattr(res, "chunk") else res.get("chunk", {})
            cid = res.chunk_id if hasattr(res, "chunk_id") else res.get("chunk_id", "")
            cost = _chunk_token_cost(chunk, self._CHUNK_HEADER_TOKENS)
            # Reserve room for the bibliography line plus, on the first
            # chunk, the two section headings.
            reserve = self._CITATION_LINE_TOKENS + section_overhead_pending
            if tokens_used + cost + reserve > budget:
                ctx.chunks_skipped_oversize.append(cid)
                continue
            citation_id = len(rendered_chunks) + 1
            rendered_chunks.append((citation_id, res))
            # Book both the chunk body+header AND its eventual bibliography
            # line in tokens_used immediately, so subsequent reserve checks
            # see the running total correctly.
            tokens_used += cost + self._CITATION_LINE_TOKENS
            ctx.chunks_included.append(cid)
            # Section headings only need to be reserved once.
            section_overhead_pending = 0

        if rendered_chunks:
            body_lines.append("## Vault Hits")
            body_lines.append("")
            tokens_used += self._SECTION_HEADING_TOKENS
            for citation_id, res in rendered_chunks:
                chunk = res.chunk
                heading = _heading_label(chunk)
                file_ = chunk.get("file") or "(unknown)"
                body_lines.append(f"### [^{citation_id}] {file_} :: {heading}")
                body_lines.append("")
                # Verbatim chunk text — no marker injection inside the body.
                body_lines.append((chunk.get("text") or "").rstrip())
                body_lines.append("")

            # Bibliography (citation-line cost was already booked at
            # acceptance time; we only add the section heading here).
            body_lines.append("## Citations")
            body_lines.append("")
            tokens_used += self._SECTION_HEADING_TOKENS
            for citation_id, res in rendered_chunks:
                chunk = res.chunk
                heading = _heading_label(chunk)
                file_ = chunk.get("file") or "(unknown)"
                score = getattr(res, "final_score", None)
                if score is None:
                    body_lines.append(
                        f"[^{citation_id}]: {file_}  ::  {heading}"
                    )
                else:
                    body_lines.append(
                        f"[^{citation_id}]: {file_}  ::  {heading}  (score={score:.4f})"
                    )
            body_lines.append("")
            ctx.citation_count = len(rendered_chunks)

        ctx.text = "\n".join(body_lines).rstrip() + "\n"
        ctx.tokens_used = tokens_used
        return ctx

    # ---- Internals -------------------------------------------------------

    def _maybe_emit_hermes_section(
        self,
        *,
        title: str,
        path: Optional[Path],
        budget: int,
        tokens_used: int,
        lines: list[str],
        set_flag: Any,
    ) -> int:
        """Append a ``## <title>`` section if the file exists and fits.

        The section is *atomic*: either it fits whole or it's skipped.
        We never partially render Hermes memory — half a memory blob is
        worse than none.
        """
        text = _read_text_file(path)
        if not text:
            return tokens_used
        cost = estimate_tokens(text) + self._SECTION_HEADING_TOKENS
        if tokens_used + cost > budget:
            log.info(
                "Hermes section %r skipped: would exceed token budget "
                "(used=%d cost=%d budget=%d)",
                title, tokens_used, cost, budget,
            )
            return tokens_used
        lines.append(f"## {title}")
        lines.append("")
        lines.append(text)
        lines.append("")
        set_flag()
        return tokens_used + cost
