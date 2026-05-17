"""Tests for cortex.text — BM25 normalization, slugify, token estimation."""

from __future__ import annotations

from cortex.text import (
    estimate_tokens,
    normalize_for_bm25,
    slugify,
    strip_markdown,
    tokenize_bm25,
)


# ---- strip_markdown -------------------------------------------------------


def test_strip_markdown_removes_code_fences() -> None:
    text = "before\n```python\ndef f():\n    pass\n```\nafter"
    out = strip_markdown(text)
    assert "def f" not in out
    assert "before" in out and "after" in out


def test_strip_markdown_keeps_link_text() -> None:
    text = "See [the docs](https://example.com) please."
    out = strip_markdown(text)
    assert "the docs" in out
    assert "https://example.com" not in out


def test_strip_markdown_keeps_wikilink_target() -> None:
    text = "Refs [[Memory Model|aliased]] and [[Other#Section]]."
    out = strip_markdown(text)
    assert "Memory Model" in out
    assert "Other" in out
    assert "[[" not in out


def test_strip_markdown_drops_heading_markers() -> None:
    out = strip_markdown("## Section One\n\nbody")
    assert out.startswith("Section One")
    assert "##" not in out


def test_strip_markdown_drops_emphasis() -> None:
    out = strip_markdown("**bold** and *italic* and ~~strike~~")
    assert "bold" in out and "italic" in out and "strike" in out
    assert "**" not in out and "~~" not in out


# ---- normalize_for_bm25 ----------------------------------------------------


def test_normalize_lowercases() -> None:
    assert normalize_for_bm25("HELLO World") == "hello world"


def test_normalize_collapses_whitespace() -> None:
    assert normalize_for_bm25("  a   b\n\nc  ") == "a b c"


def test_normalize_unicode_nfkc() -> None:
    """Full-width ASCII and ligatures fold to plain ASCII."""
    # full-width 'A' + ligature 'ﬁ'
    assert normalize_for_bm25("\uff21\ufb01") == "afi"


def test_normalize_handles_german_umlauts() -> None:
    # We don't strip diacritics in BM25 normalization (only in slugify),
    # so ä/ö/ü stay intact and are still searchable.
    out = normalize_for_bm25("Größe und Qualität")
    assert "größe" in out
    assert "qualität" in out


def test_normalize_strips_markdown_first() -> None:
    out = normalize_for_bm25("## Heading\n\n**bold** body with [[Link]]")
    assert "heading" in out
    assert "bold" in out
    assert "link" in out
    assert "#" not in out
    assert "*" not in out


def test_normalize_empty_input() -> None:
    assert normalize_for_bm25("") == ""
    assert normalize_for_bm25(None) == ""  # type: ignore[arg-type]


# ---- tokenize_bm25 --------------------------------------------------------


def test_tokenize_returns_words() -> None:
    assert tokenize_bm25("Hello, world!") == ["hello", "world"]


def test_tokenize_keeps_short_tokens() -> None:
    """Short tokens like '3d' or 'it' must survive."""
    toks = tokenize_bm25("3D printing in IT")
    assert "3d" in toks
    assert "it" in toks


def test_tokenize_empty() -> None:
    assert tokenize_bm25("") == []
    assert tokenize_bm25("   ") == []


def test_tokenize_mixed_de_en() -> None:
    toks = tokenize_bm25("Memory Modell mit Größe und scope")
    assert "memory" in toks
    assert "modell" in toks
    assert "größe" in toks
    assert "scope" in toks


# ---- slugify --------------------------------------------------------------


def test_slugify_basic() -> None:
    assert slugify("Section One") == "section-one"


def test_slugify_drops_punctuation() -> None:
    assert slugify("What's New, Doc?") == "what-s-new-doc"


def test_slugify_handles_german_umlauts() -> None:
    """NFKD + ASCII drop turns ö→o, ü→u, etc. — readable, deterministic."""
    out = slugify("Größe & Qualität")
    # 'ß' has no NFKD ASCII fold; it's dropped → "groe-qualitat"-ish.
    # We don't pin the exact form, just that it's lowercase ASCII.
    assert out == out.lower()
    assert all(c.isascii() for c in out)
    assert "qualitat" in out


def test_slugify_collapses_runs() -> None:
    assert slugify("a   b---c") == "a-b-c"


def test_slugify_empty_returns_fallback() -> None:
    assert slugify("") == "section"
    assert slugify("   !!!  ") == "section"


def test_slugify_truncates_long_text() -> None:
    long_in = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod"
    out = slugify(long_in)
    assert len(out) <= 60
    # Should not end with a stray hyphen
    assert not out.endswith("-")


def test_slugify_deterministic() -> None:
    assert slugify("Hello World") == slugify("Hello World")


# ---- estimate_tokens ------------------------------------------------------


def test_estimate_tokens_zero_for_empty() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_grows_with_length() -> None:
    short = estimate_tokens("hi")
    long = estimate_tokens("x" * 1000)
    assert long > short
    assert long >= 200  # ~1000 / 3.5
