"""Tests for cortex.frontmatter — normalization of YAML frontmatter."""

from __future__ import annotations

from datetime import date

from cortex.frontmatter import (
    KNOWN_TYPES,
    missing_required,
    normalize,
)


# ---- tags -----------------------------------------------------------------


def test_tags_list_passthrough() -> None:
    n = normalize({"tags": ["a", "b", "c"]})
    assert n.tags == ["a", "b", "c"]


def test_tags_csv_string() -> None:
    n = normalize({"tags": "memory, architecture, ops"})
    assert n.tags == ["memory", "architecture", "ops"]


def test_tags_single_string() -> None:
    n = normalize({"tags": "lonely"})
    assert n.tags == ["lonely"]


def test_tags_none() -> None:
    n = normalize({"tags": None})
    assert n.tags == []


def test_tags_dedup_preserve_order() -> None:
    n = normalize({"tags": ["a", "b", "a", "c"]})
    assert n.tags == ["a", "b", "c"]


# ---- numeric fields -------------------------------------------------------


def test_confidence_string_high() -> None:
    n = normalize({"confidence": "high"})
    assert n.confidence == 0.85


def test_confidence_numeric_in_range() -> None:
    n = normalize({"confidence": 0.7})
    assert n.confidence == 0.7


def test_confidence_numeric_5_scale_rescaled() -> None:
    """Value of 4 (on 1..5 scale) becomes 0.8 in 0..1 space."""
    n = normalize({"confidence": 4})
    assert 0.0 <= n.confidence <= 1.0


def test_confidence_unknown_string_defaults() -> None:
    n = normalize({"confidence": "vague"})
    assert n.confidence == 0.5
    assert any("unrecognized confidence" in w for w in n.warnings)


def test_importance_string_low() -> None:
    n = normalize({"importance": "low"})
    assert n.importance == 1.0


def test_importance_numeric() -> None:
    n = normalize({"importance": 4.5})
    assert n.importance == 4.5


def test_importance_in_unit_interval_rescaled() -> None:
    n = normalize({"importance": 0.5})
    assert 1.0 <= n.importance <= 5.0


# ---- enums (warn but keep) ------------------------------------------------


def test_unknown_type_keeps_value_with_warning() -> None:
    n = normalize({"type": "weirdtype"})
    assert n.type == "weirdtype"
    assert any("type" in w and "weirdtype" in w for w in n.warnings)


def test_known_type_no_warning() -> None:
    for known in KNOWN_TYPES:
        n = normalize({"type": known})
        assert n.type == known
        assert not any("unknown type" in w for w in n.warnings)


def test_unknown_status_keeps_value_with_warning() -> None:
    n = normalize({"status": "in-progress"})
    assert n.status == "in-progress"
    assert any("status" in w for w in n.warnings)


# ---- dates ----------------------------------------------------------------


def test_last_verified_iso_string() -> None:
    n = normalize({"last_verified": "2026-04-27"})
    assert n.last_verified == "2026-04-27"


def test_last_verified_date_object() -> None:
    n = normalize({"last_verified": date(2026, 4, 27)})
    assert n.last_verified == "2026-04-27"


def test_last_verified_garbage() -> None:
    n = normalize({"last_verified": "not-a-date"})
    assert n.last_verified == ""
    assert any("last_verified" in w for w in n.warnings)


# ---- empty / missing -------------------------------------------------------


def test_normalize_none_input() -> None:
    n = normalize(None)
    assert n.tags == []
    assert n.confidence == 0.5
    assert n.importance == 3.0


def test_normalize_empty_dict() -> None:
    n = normalize({})
    assert n.type == ""
    assert n.tags == []
    assert n.warnings == []


# ---- missing_required -----------------------------------------------------


def test_missing_required_empty() -> None:
    assert sorted(missing_required({})) == sorted(
        ["type", "status", "tags", "confidence", "importance", "stability"]
    )


def test_missing_required_partial() -> None:
    fm = {"type": "fact", "tags": [], "confidence": "high"}
    missing = missing_required(fm)
    assert "status" in missing
    assert "type" not in missing
    assert "tags" not in missing


def test_missing_required_complete() -> None:
    fm = {
        "type": "fact", "status": "active", "tags": [],
        "confidence": "high", "importance": "high", "stability": "stable",
    }
    assert missing_required(fm) == []


# ---- raw preservation ------------------------------------------------------


def test_raw_dict_is_json_safe() -> None:
    """Dates in raw dict must be coerced to strings (so JSON serializes)."""
    n = normalize({"last_verified": date(2026, 4, 27), "tags": ["a"]})
    assert n.raw["last_verified"] == "2026-04-27"
    assert n.raw["tags"] == ["a"]
