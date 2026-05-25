from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cortex.cli import main
from cortex.config import (
    Config,
    ContextBuilderConfig,
    EmbeddingsConfig,
    HermesMemoryConfig,
    IndexConfig,
    SearchConfig,
    VaultConfig,
)
from cortex.search_eval import (
    BASELINE_UPDATE_RULE,
    build_report_metadata,
    load_eval_cases,
    missing_expected_files,
    run_search_eval,
)


def make_config(tmp_path: Path) -> Config:
    return Config(
        vault=VaultConfig(path=tmp_path / "vault"),
        hermes_memory=HermesMemoryConfig(),
        index=IndexConfig(
            chunks_path=tmp_path / "chunks.jsonl",
            chroma_path=tmp_path / "chroma",
        ),
        embeddings=EmbeddingsConfig(model="test-model", device="cpu"),
        search=SearchConfig(top_k=10, fetch_multiplier=2),
        context_builder=ContextBuilderConfig(),
    )


def write_config(path: Path, cfg: Config) -> None:
    path.write_text(
        "\n".join(
            [
                "vault:",
                f"  path: {cfg.vault.path}",
                "index:",
                f"  chunks_path: {cfg.index.chunks_path}",
                f"  chroma_path: {cfg.index.chroma_path}",
                "embeddings:",
                "  model: test-model",
                "  device: cpu",
                "search:",
                "  top_k: 10",
                "  fetch_multiplier: 2",
            ]
        ),
        encoding="utf-8",
    )


def write_chunks(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = [
        {
            "id": "10_facts/Alpha.md#intro",
            "file": "10_facts/Alpha.md",
            "folder": "10_facts",
            "heading_path": ["Intro"],
            "text": "alpha ranking bm25 vector rrf diagnostics",
            "tags": [],
            "wikilinks": [],
            "frontmatter": {"importance": 5},
            "fm_normalized": {"type": "fact", "importance": 5.0, "last_verified": ""},
            "modified_date": "2026-04-15",
        },
        {
            "id": "10_facts/Beta.md#intro",
            "file": "10_facts/Beta.md",
            "folder": "10_facts",
            "heading_path": ["Intro"],
            "text": "beta unrelated fallback text",
            "tags": [],
            "wikilinks": [],
            "frontmatter": {},
            "fm_normalized": {"type": "fact", "importance": 3.0, "last_verified": ""},
            "modified_date": "2026-01-01",
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk) + "\n")


def write_cases(path: Path) -> None:
    cases = [
        {
            "id": f"case-{i}",
            "query": "alpha ranking bm25 vector rrf diagnostics",
            "expected_files": ["10_facts/Alpha.md"],
            "expected_top_k": 1,
        }
        for i in range(10)
    ]
    path.write_text("cases:\n" + "\n".join(
        [
            f"  - id: {c['id']}\n"
            f"    query: {c['query']}\n"
            "    expected_files:\n"
            f"      - {c['expected_files'][0]}\n"
            f"    expected_top_k: {c['expected_top_k']}"
            for c in cases
        ]
    ) + "\n", encoding="utf-8")


@pytest.fixture
def fake_vector_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_collection = MagicMock()
    fake_collection.query.return_value = {"ids": [[]], "distances": [[]]}
    fake_chromadb = MagicMock()
    fake_chromadb.PersistentClient.return_value.get_or_create_collection.return_value = fake_collection
    monkeypatch.setitem(__import__("sys").modules, "chromadb", fake_chromadb)

    fake_st = MagicMock()
    fake_model = MagicMock()
    import numpy as np

    fake_model.encode.side_effect = lambda texts, **kw: np.array([[0.1, 0.2, 0.3]] * len(texts))
    fake_model.get_sentence_embedding_dimension.return_value = 3
    fake_st.SentenceTransformer.return_value = fake_model
    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", fake_st)


def test_default_eval_cases_are_real_suite() -> None:
    cases = load_eval_cases()
    assert len(cases) >= 10
    assert any(c.id == "cortex-scoring-keywords" for c in cases)
    assert all(c.expected_files for c in cases)
    assert all(isinstance(file, str) for c in cases for file in c.expected_files)


def test_load_eval_cases_rejects_non_string_expected_files(tmp_path: Path) -> None:
    cases_path = tmp_path / "bad_cases.yaml"
    cases_path.write_text(
        """
cases:
  - id: bad-mapping
    query: alpha
    expected_files:
      - 20_decisions/Decision - Cortex Plugin: Entry-Point durch Standalone-Plugin ersetzen.md
    expected_top_k: 1
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected_files entries must be non-empty strings"):
        load_eval_cases(cases_path)


def test_missing_expected_files_reports_absent_notes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "10_facts").mkdir(parents=True)
    (vault / "10_facts" / "Alpha.md").write_text("alpha", encoding="utf-8")
    cases_path = tmp_path / "cases.yaml"
    write_cases(cases_path)
    text = cases_path.read_text(encoding="utf-8")
    text = text.replace("      - 10_facts/Alpha.md\n", "      - 10_facts/Alpha.md\n      - 10_facts/Missing.md\n", 1)
    cases_path.write_text(text, encoding="utf-8")

    cases = load_eval_cases(cases_path)

    assert missing_expected_files(cases, vault) == {"case-0": ["10_facts/Missing.md"]}


def test_search_eval_report_contains_required_diagnostics(
    tmp_path: Path, fake_vector_empty: None
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path)
    cases_path = tmp_path / "cases.yaml"
    write_cases(cases_path)
    metadata = build_report_metadata(
        cfg,
        config_path=tmp_path / "config.yaml",
        cases_path=cases_path,
        generated_at="2026-05-24T00:00:00Z",
    )

    report = run_search_eval(cfg, load_eval_cases(cases_path), top_k=3, metadata=metadata)

    assert report["case_count"] == 10
    assert report["passed"] == 10
    assert report["metadata"]["generated_at"] == "2026-05-24T00:00:00Z"
    assert report["metadata"]["config_path"].endswith("config.yaml")
    assert report["metadata"]["vault_path"].endswith("vault")
    assert report["metadata"]["chunks_path"].endswith("chunks.jsonl")
    assert report["metadata"]["chroma_path"].endswith("chroma")
    assert report["metadata"]["cases_path"] == str(cases_path)
    assert report["metadata"]["cortex_version"]
    assert report["metadata"]["index_artifact"]["exists"] is True
    assert report["metadata"]["index_artifact"]["sha256"]
    assert report["metadata"]["cases_artifact"]["sha256"]
    assert report["metadata"]["chroma_artifact"]["exists"] is False
    assert report["metadata"]["baseline_update_rule"] == BASELINE_UPDATE_RULE
    first_hit = report["cases"][0]["hits"][0]
    assert {
        "final_score",
        "rrf_score",
        "final",
        "rrf",
        "bm25_rank",
        "vector_rank",
        "graph_rank",
        "raw_boost_multiplier",
        "boost_multiplier",
        "boost_capped",
        "quality_factor",
        "quality_reason",
    } <= set(first_hit)
    assert first_hit["final_score"] == first_hit["final"]
    assert first_hit["rrf_score"] == first_hit["rrf"]
    assert report["cases"][0]["boost_rank_delta"] == 0


def test_search_eval_baseline_comparison_reports_rank_delta(
    tmp_path: Path, fake_vector_empty: None
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path)
    cases_path = tmp_path / "cases.yaml"
    write_cases(cases_path)
    cases = load_eval_cases(cases_path)
    baseline = {
        "cases": [
            {
                "id": "case-0",
                "best_expected_rank": 2,
                "expected_top_k": 1,
                "hits": [{"rank": 2, "file": "10_facts/Alpha.md"}],
            }
        ]
    }

    report = run_search_eval(cfg, cases, top_k=3, baseline=baseline)

    assert report["cases"][0]["baseline_best_expected_rank"] == 2
    assert report["cases"][0]["baseline_passed"] is False
    assert report["cases"][0]["baseline_pass_delta"] == 1
    assert report["cases"][0]["baseline_status_change"] == "improvement"
    assert report["cases"][0]["baseline_rank_delta"] == -1
    assert report["cases"][0]["per_file_rank_delta"] == {"10_facts/Alpha.md": -1}
    assert report["compare_summary"]["matched_case_count"] == 1
    assert report["compare_summary"]["missing_in_baseline"] == [
        f"case-{i}" for i in range(1, 10)
    ]
    assert report["compare_summary"]["pass_improvements"] == ["case-0"]
    assert report["compare_summary"]["rank_improvements"] == [{"id": "case-0", "delta": -1}]


def test_cli_search_eval_json_and_output_file(
    tmp_path: Path,
    fake_vector_empty: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path)
    cfg_path = tmp_path / "config.yaml"
    write_config(cfg_path, cfg)
    cases_path = tmp_path / "cases.yaml"
    write_cases(cases_path)
    output = tmp_path / "report.json"

    rc = main([
        "search-eval",
        "--config",
        str(cfg_path),
        "--cases",
        str(cases_path),
        "--top-k",
        "3",
        "--output",
        str(output),
        "--json",
    ])

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert printed["case_count"] == 10
    assert written["metadata"]["config_path"] == str(cfg_path)
    assert written["metadata"]["cases_path"] == str(cases_path)
    assert written["cases"][0]["hits"][0]["file"] == "10_facts/Alpha.md"


def test_search_eval_baseline_does_not_change_ranking_payload(
    tmp_path: Path, fake_vector_empty: None
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path)
    cases_path = tmp_path / "cases.yaml"
    write_cases(cases_path)
    cases = load_eval_cases(cases_path)

    without_baseline = run_search_eval(cfg, cases, top_k=3)
    with_baseline = run_search_eval(cfg, cases, top_k=3, baseline=without_baseline)

    assert with_baseline["passed"] == without_baseline["passed"]
    assert with_baseline["failed"] == without_baseline["failed"]
    assert with_baseline["cases"][0]["hits"] == without_baseline["cases"][0]["hits"]
    assert with_baseline["cases"][0]["best_expected_rank"] == without_baseline["cases"][0]["best_expected_rank"]


def test_cli_search_eval_writes_baseline_compare_summary(
    tmp_path: Path,
    fake_vector_empty: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = make_config(tmp_path)
    write_chunks(cfg.index.chunks_path)
    cfg_path = tmp_path / "config.yaml"
    write_config(cfg_path, cfg)
    cases_path = tmp_path / "cases.yaml"
    write_cases(cases_path)
    baseline = tmp_path / "baseline.json"
    output = tmp_path / "current.json"
    baseline.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "metadata": {"generated_at": "2026-05-23T00:00:00Z"},
                "cases": [
                    {
                        "id": "case-0",
                        "best_expected_rank": 2,
                        "passed": False,
                        "hits": [{"rank": 2, "file": "10_facts/Alpha.md"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rc = main([
        "search-eval",
        "--config",
        str(cfg_path),
        "--cases",
        str(cases_path),
        "--top-k",
        "3",
        "--baseline",
        str(baseline),
        "--output",
        str(output),
        "--json",
    ])

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert printed["compare_summary"]["pass_improvements"] == ["case-0"]
    assert written["compare_summary"] == printed["compare_summary"]
    assert written["cases"][0]["baseline_status_change"] == "improvement"
