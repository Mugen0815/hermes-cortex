"""Reproducible search-ranking evaluation harness for hermes-cortex.

The harness intentionally measures the current ranking stack without changing
scoring semantics. It runs fixed real-vault queries, records expected-hit ranks,
and emits per-hit diagnostics useful for later scoring changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cortex.config import Config
from cortex.search import HybridSearcher, SearchResult


@dataclass(frozen=True)
class EvalCase:
    """One ranking evaluation query.

    expected_files are matched against ``SearchResult.chunk["file"]``.
    expected_top_k is the target rank threshold for a pass.
    """

    id: str
    query: str
    expected_files: tuple[str, ...]
    expected_top_k: int = 5
    notes: str = ""


def default_cases_path() -> Path:
    """Return the repo-local default eval cases path.

    The project currently runs this harness from a checkout. Keeping the cases
    in tests/fixtures makes them visible to tests and future scoring work.
    """

    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "search_eval_cases.yaml"


def load_eval_cases(path: str | Path | None = None) -> list[EvalCase]:
    """Load eval cases from YAML.

    Expected shape:

    cases:
      - id: short-name
        query: "..."
        expected_files: ["30_projects/..."]
        expected_top_k: 5
    """

    p = Path(path).expanduser().resolve() if path is not None else default_cases_path()
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    items = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError(f"{p}: expected a list or a mapping with key 'cases'")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{p}: case #{idx} must be a mapping")
        case_id = str(item.get("id") or "").strip()
        query = str(item.get("query") or "").strip()
        expected = item.get("expected_files") or item.get("expected") or []
        if isinstance(expected, str):
            expected = [expected]
        expected_top_k = int(item.get("expected_top_k", 5))
        if not case_id:
            raise ValueError(f"{p}: case #{idx} is missing id")
        if case_id in seen:
            raise ValueError(f"{p}: duplicate case id {case_id!r}")
        if not query:
            raise ValueError(f"{p}: case {case_id!r} is missing query")
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"{p}: case {case_id!r} must define expected_files")
        invalid_expected = [x for x in expected if not isinstance(x, str) or not x.strip()]
        if invalid_expected:
            raise ValueError(
                f"{p}: case {case_id!r} expected_files entries must be non-empty strings"
            )
        if expected_top_k <= 0:
            raise ValueError(f"{p}: case {case_id!r} expected_top_k must be > 0")
        seen.add(case_id)
        cases.append(
            EvalCase(
                id=case_id,
                query=query,
                expected_files=tuple(str(x).strip() for x in expected),
                expected_top_k=expected_top_k,
                notes=str(item.get("notes") or ""),
            )
        )
    if len(cases) < 10:
        raise ValueError(f"{p}: expected at least 10 eval cases, got {len(cases)}")
    return cases


def missing_expected_files(cases: list[EvalCase], vault_path: str | Path) -> dict[str, list[str]]:
    """Return expected note files that are absent from a concrete vault checkout.

    This is deliberately opt-in for CLI use because the real-vault suite is tied
    to a mutable local vault. Unit tests can validate fixture shape without
    requiring that vault to exist.
    """

    root = Path(vault_path).expanduser().resolve()
    missing: dict[str, list[str]] = {}
    for case in cases:
        missing_files = [file for file in case.expected_files if not (root / file).is_file()]
        if missing_files:
            missing[case.id] = missing_files
    return missing


def _hit_payload(rank: int, r: SearchResult) -> dict[str, Any]:
    return {
        "rank": rank,
        "chunk_id": r.chunk_id,
        "file": r.chunk.get("file"),
        "heading_path": r.chunk.get("heading_path") or [],
        "final_score": r.final_score,
        "rrf_score": r.rrf_score,
        # Backward-compatible aliases for older local baselines.
        "final": r.final_score,
        "rrf": r.rrf_score,
        "bm25_rank": r.bm25_rank,
        "vector_rank": r.vector_rank,
        "graph_rank": r.graph_rank,
        "boost_multiplier": r.debug.get("boost_multiplier", 1.0),
        "raw_boost_multiplier": r.debug.get("raw_boost_multiplier", r.debug.get("boost_multiplier", 1.0)),
        "boost_capped": r.debug.get("boost_capped", False),
        "quality_factor": r.debug.get("quality_factor", 1.0),
        "quality_reason": r.debug.get("quality_reason", ""),
    }


def _best_expected_rank(hits: list[dict[str, Any]], expected_files: tuple[str, ...]) -> int | None:
    expected = set(expected_files)
    ranks = [int(h["rank"]) for h in hits if h.get("file") in expected]
    return min(ranks) if ranks else None


def _rank_by_file(hits: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in hits:
        file = h.get("file")
        if file and file not in out:
            out[str(file)] = int(h["rank"])
    return out


def run_search_eval(
    cfg: Config,
    cases: list[EvalCase],
    *,
    top_k: int = 10,
    compare_unboosted: bool = True,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run all eval cases and return a JSON-serializable report."""

    if top_k <= 0:
        raise ValueError("top_k must be > 0")

    searcher = HybridSearcher(cfg)
    baseline_cases = {
        str(c.get("id")): c for c in (baseline or {}).get("cases", []) if isinstance(c, dict)
    }
    report_cases: list[dict[str, Any]] = []
    passed = 0

    for case in cases:
        boosted = [_hit_payload(i, r) for i, r in enumerate(searcher.search(case.query, top_k=top_k), 1)]
        best_rank = _best_expected_rank(boosted, case.expected_files)
        ok = best_rank is not None and best_rank <= case.expected_top_k
        if ok:
            passed += 1

        case_payload: dict[str, Any] = {
            "id": case.id,
            "query": case.query,
            "expected_files": list(case.expected_files),
            "expected_top_k": case.expected_top_k,
            "best_expected_rank": best_rank,
            "passed": ok,
            "hits": boosted,
        }
        if case.notes:
            case_payload["notes"] = case.notes

        if compare_unboosted:
            unboosted = [
                _hit_payload(i, r)
                for i, r in enumerate(
                    searcher.search(case.query, top_k=top_k, apply_boost=False), 1
                )
            ]
            unboosted_best = _best_expected_rank(unboosted, case.expected_files)
            case_payload["unboosted_best_expected_rank"] = unboosted_best
            case_payload["boost_rank_delta"] = (
                None
                if best_rank is None or unboosted_best is None
                else best_rank - unboosted_best
            )

        base = baseline_cases.get(case.id)
        if base is not None:
            base_rank = base.get("best_expected_rank")
            case_payload["baseline_best_expected_rank"] = base_rank
            case_payload["baseline_rank_delta"] = (
                None if best_rank is None or base_rank is None else best_rank - int(base_rank)
            )
            base_hits = base.get("hits") if isinstance(base.get("hits"), list) else []
            current_by_file = _rank_by_file(boosted)
            baseline_by_file = _rank_by_file(base_hits)
            common_files = sorted(set(current_by_file) & set(baseline_by_file))
            case_payload["per_file_rank_delta"] = {
                file: current_by_file[file] - baseline_by_file[file] for file in common_files
            }

        report_cases.append(case_payload)

    return {
        "schema_version": 1,
        "top_k": top_k,
        "case_count": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "cases": report_cases,
    }


def load_baseline(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    p = Path(path).expanduser().resolve()
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")


def summarize_report(report: dict[str, Any]) -> str:
    """Compact human-readable summary for terminal use."""

    lines = [
        f"Search eval: {report['passed']}/{report['case_count']} passed "
        f"(top_k={report['top_k']})"
    ]
    for case in report["cases"]:
        rank = case.get("best_expected_rank")
        status = "PASS" if case.get("passed") else "FAIL"
        delta = case.get("baseline_rank_delta")
        delta_s = "" if delta is None else f", baseline_delta={delta:+d}"
        boost_delta = case.get("boost_rank_delta")
        boost_s = "" if boost_delta is None else f", boost_delta={boost_delta:+d}"
        lines.append(
            f"  {status} {case['id']}: expected_rank={rank}"
            f" <= {case['expected_top_k']}{delta_s}{boost_s}"
        )
    return "\n".join(lines)
