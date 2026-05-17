"""Unit tests for cortex.filters — SearchFilters spec + where-builder + predicate."""

from __future__ import annotations

import pytest

from cortex.filters import SearchFilters, build_chroma_where, chunk_matches


# ---- chunk fixture helper --------------------------------------------------


def _chunk(**over) -> dict:
    base = {
        "id": "f.md::0",
        "file": "10_facts/Foo.md",
        "folder": "10_facts",
        "text": "...",
        "tags": ["jarvis", "memory"],
        "wikilinks": ["Bar", "Baz"],
        "frontmatter": {
            "type": "fact",
            "status": "active",
            "domain": "infra",
            "project": "hermes-cortex",
            "importance": 4,
            "confidence": 0.8,
        },
        "fm_normalized": {
            "type": "fact",
            "status": "active",
            "domain": "infra",
            "project": "hermes-cortex",
            "importance": 4.0,
            "confidence": 0.8,
            "last_verified": "2026-04-01",
        },
        "modified_date": "2026-04-15",
        "modified": "2026-04-15T10:00:00",
    }
    base.update(over)
    return base


# ---- SearchFilters.validate -------------------------------------------------


def test_filters_empty_is_empty():
    f = SearchFilters()
    assert f.is_empty()


def test_filters_validate_rejects_bad_iso_date():
    f = SearchFilters(modified_after="2026/04/01")
    with pytest.raises(ValueError, match="modified_after"):
        f.validate()


def test_filters_validate_rejects_impossible_date():
    f = SearchFilters(modified_after="2026-02-30")  # Feb 30 doesn't exist
    with pytest.raises(ValueError, match="modified_after"):
        f.validate()


def test_filters_validate_rejects_inverted_date_range():
    f = SearchFilters(modified_after="2026-05-01", modified_before="2026-01-01")
    with pytest.raises(ValueError, match="after"):
        f.validate()


def test_filters_validate_rejects_inverted_numeric_range():
    f = SearchFilters(importance_min=5.0, importance_max=1.0)
    with pytest.raises(ValueError, match="importance_min"):
        f.validate()


def test_filters_validate_rejects_non_list_field():
    f = SearchFilters(type="fact")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="type"):
        f.validate()


def test_filters_validate_rejects_empty_string_in_list():
    f = SearchFilters(tags_any=["jarvis", ""])
    with pytest.raises(ValueError, match="tags_any"):
        f.validate()


def test_filters_validate_idempotent():
    f = SearchFilters(type=["fact"])
    f.validate()
    f.validate()  # no raise on second call


# ---- build_chroma_where ----------------------------------------------------


def test_where_empty_when_no_filters():
    assert build_chroma_where(SearchFilters()) == {}


def test_where_single_value_uses_equality():
    w = build_chroma_where(SearchFilters(type=["fact"]))
    assert w == {"type": "fact"}


def test_where_multiple_values_use_in():
    w = build_chroma_where(SearchFilters(type=["fact", "decision"]))
    assert w == {"type": {"$in": ["fact", "decision"]}}


def test_where_combines_fields_with_and():
    w = build_chroma_where(
        SearchFilters(type=["fact"], importance_min=4)
    )
    assert "$and" in w
    clauses = w["$and"]
    assert {"type": "fact"} in clauses
    assert {"importance": {"$gte": 4.0}} in clauses


def test_where_numeric_range_both_bounds():
    w = build_chroma_where(SearchFilters(importance_min=2, importance_max=4))
    assert w == {"$and": [
        {"importance": {"$gte": 2.0}},
        {"importance": {"$lte": 4.0}},
    ]}


def test_where_date_range_uses_modified_date():
    w = build_chroma_where(
        SearchFilters(modified_after="2026-01-01", modified_before="2026-12-31")
    )
    assert w == {"$and": [
        {"modified_date": {"$gte": "2026-01-01"}},
        {"modified_date": {"$lte": "2026-12-31"}},
    ]}


def test_where_excludes_membership_filters():
    """tags/wikilinks must NOT appear in Chroma where (handled post-fetch)."""
    w = build_chroma_where(
        SearchFilters(tags_any=["jarvis"], wikilinks_all=["Bar"])
    )
    assert w == {}


def test_where_uses_folder_singular_for_chroma():
    w = build_chroma_where(SearchFilters(folders=["10_facts", "20_decisions"]))
    assert w == {"folder": {"$in": ["10_facts", "20_decisions"]}}


# ---- chunk_matches predicate -----------------------------------------------


def test_predicate_passes_when_no_filter():
    assert chunk_matches(_chunk(), SearchFilters()) is True


def test_predicate_type_match():
    assert chunk_matches(_chunk(), SearchFilters(type=["fact"])) is True
    assert chunk_matches(_chunk(), SearchFilters(type=["decision"])) is False


def test_predicate_type_or_within_list():
    assert chunk_matches(
        _chunk(), SearchFilters(type=["decision", "fact"])
    ) is True


def test_predicate_combines_fields_with_and():
    f = SearchFilters(type=["fact"], status=["draft"])
    # status "active" ≠ "draft" → AND fails even though type matches
    assert chunk_matches(_chunk(), f) is False


def test_predicate_importance_range():
    assert chunk_matches(_chunk(), SearchFilters(importance_min=4)) is True
    assert chunk_matches(_chunk(), SearchFilters(importance_min=5)) is False
    assert chunk_matches(_chunk(), SearchFilters(importance_max=3)) is False


def test_predicate_missing_importance_fails_range():
    """Missing importance → range filters reject (cannot prove bound)."""
    c = _chunk(fm_normalized={}, frontmatter={})
    assert chunk_matches(c, SearchFilters(importance_min=1)) is False


def test_predicate_date_range_inclusive():
    f = SearchFilters(modified_after="2026-04-15", modified_before="2026-04-15")
    assert chunk_matches(_chunk(), f) is True


def test_predicate_date_range_excludes():
    assert chunk_matches(
        _chunk(), SearchFilters(modified_after="2026-05-01")
    ) is False


def test_predicate_date_missing_modified_date_rejects():
    c = _chunk(modified_date="")
    assert chunk_matches(
        c, SearchFilters(modified_after="2026-01-01")
    ) is False


def test_predicate_tags_any():
    assert chunk_matches(
        _chunk(), SearchFilters(tags_any=["jarvis", "nope"])
    ) is True
    assert chunk_matches(
        _chunk(), SearchFilters(tags_any=["nope"])
    ) is False


def test_predicate_tags_all():
    assert chunk_matches(
        _chunk(), SearchFilters(tags_all=["jarvis", "memory"])
    ) is True
    assert chunk_matches(
        _chunk(), SearchFilters(tags_all=["jarvis", "missing"])
    ) is False


def test_predicate_wikilinks_any_and_all():
    assert chunk_matches(
        _chunk(), SearchFilters(wikilinks_any=["Bar"])
    ) is True
    assert chunk_matches(
        _chunk(), SearchFilters(wikilinks_all=["Bar", "Baz"])
    ) is True
    assert chunk_matches(
        _chunk(), SearchFilters(wikilinks_all=["Bar", "Quux"])
    ) is False


def test_predicate_missing_tags_array_fails_membership():
    c = _chunk(tags=[])
    assert chunk_matches(c, SearchFilters(tags_any=["jarvis"])) is False
    assert chunk_matches(c, SearchFilters(tags_all=["jarvis"])) is False


def test_predicate_folder_filter():
    assert chunk_matches(
        _chunk(), SearchFilters(folders=["10_facts"])
    ) is True
    assert chunk_matches(
        _chunk(), SearchFilters(folders=["20_decisions"])
    ) is False
