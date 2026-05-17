"""Shared text utilities for hermes-cortex.

Single source of truth for:
- normalize_for_bm25(text)  → tokens used by Phase-3 BM25 (lowercase + Unicode NFKC + markdown strip)
- tokenize_bm25(text)       → token list (whitespace split after normalize)
- slugify(text)             → stable, ASCII-friendly slug for chunk IDs
- estimate_tokens(text)     → cheap heuristic for chunks-vs-model-context warnings

Design notes:
- BM25 normalization is intentionally minimal (lowercase + NFKC + markdown strip).
  No stemming, no stopwords. Vault is DE/EN mixed; either choice loses recall on
  the other language. A future config toggle can layer Snowball on top.
- Slugify must be deterministic and stable across runs of the same text.
"""

from __future__ import annotations

import re
import unicodedata


# ---- BM25 normalization ----------------------------------------------------

# Markdown syntax we want to strip BEFORE tokenizing for BM25.
# Order matters: code fences first, then inline code, then links/images,
# then heading/list/emphasis markers.
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
# Wikilink: keep only the target text (the part before | or #), drop brackets.
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_LIST_BULLET_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_NUM_LIST_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|~~)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Punctuation-ish runs we collapse to a single space at the end.
_NON_WORD_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def strip_markdown(text: str) -> str:
    """Remove markdown syntax, preserving the readable surface text.

    Used as the pre-step before BM25 normalization. Idempotent.
    """
    if not text:
        return ""
    t = _CODE_FENCE_RE.sub(" ", text)
    t = _INLINE_CODE_RE.sub(" ", t)
    t = _IMAGE_RE.sub(r" \1 ", t)
    t = _LINK_RE.sub(r" \1 ", t)
    t = _WIKILINK_RE.sub(r" \1 ", t)
    t = _HEADING_RE.sub("", t)
    t = _LIST_BULLET_RE.sub("", t)
    t = _NUM_LIST_RE.sub("", t)
    t = _EMPHASIS_RE.sub("", t)
    t = _HTML_TAG_RE.sub(" ", t)
    return t


def normalize_for_bm25(text: str) -> str:
    """Apply the canonical BM25 normalization.

    Steps:
      1. Unicode NFKC (folds compatibility forms, normalizes width)
      2. strip markdown
      3. lowercase
      4. collapse non-word runs to space
      5. collapse whitespace
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = strip_markdown(t)
    t = t.lower()
    t = _NON_WORD_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def tokenize_bm25(text: str) -> list[str]:
    """Return whitespace-separated tokens after BM25 normalization.

    Empty tokens are filtered out. Single-char tokens are kept (matters for
    short German tokens like 'IT', '3D' once lowercased).
    """
    norm = normalize_for_bm25(text)
    if not norm:
        return []
    return [tok for tok in norm.split(" ") if tok]


# ---- Slugify ---------------------------------------------------------------

# Limit slug length so chunk IDs stay readable in logs / citations.
_SLUG_MAX_LEN = 60
_SLUG_FALLBACK = "section"


def slugify(text: str, *, max_len: int = _SLUG_MAX_LEN) -> str:
    """Stable, deterministic slug for use in chunk IDs.

    Lowercases, transliterates Unicode to ASCII via NFKD decomposition (drops
    combining marks), then keeps [a-z0-9] and turns everything else into '-'.
    Empty/all-stripped input returns SLUG_FALLBACK so IDs remain valid.

    Note: callers should disambiguate collisions themselves (e.g. by appending
    '-2', '-3', ...). This function does NOT track state.
    """
    if not text:
        return _SLUG_FALLBACK
    # NFKD then drop combining chars → loses diacritics ("Größe" → "grosse"-ish)
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    ascii_only = ascii_only.lower()
    # Replace any run of non-alphanumeric with a single hyphen
    out = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    if not out:
        return _SLUG_FALLBACK
    if len(out) > max_len:
        # Trim at last hyphen before max_len so we don't cut a word in half
        cut = out[:max_len].rsplit("-", 1)[0] or out[:max_len]
        out = cut.rstrip("-") or out[:max_len]
    return out or _SLUG_FALLBACK


# ---- Token estimation ------------------------------------------------------

# Rough heuristic: ~4 chars per token for English, ~3 for German with
# compound words. We pick 3.5 as a midpoint. Used only for warnings, not
# for any actual truncation decision.
_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate.

    Used to warn when chunks likely exceed an embedding model's context window
    (most sentence-transformers cap at 256–512 tokens). Not a real tokenizer —
    if exact counts matter, the caller should use the model's tokenizer.
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))
