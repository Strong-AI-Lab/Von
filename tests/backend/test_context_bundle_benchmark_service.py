from __future__ import annotations

from src.backend.services.context_bundle_benchmark_service import (
    build_context_bundle_benchmark_report,
    load_context_bundle_benchmark_cases,
)


def test_load_context_bundle_benchmark_cases_reads_default_seed_bundle() -> None:
    result = load_context_bundle_benchmark_cases()

    assert result["source"] == "seed_bundle"
    assert result["case_set"] == "phase2_seed"
    assert result["seed_schema_version"] == "context_bundle_benchmark.seed_bundle.v1"
    assert len(result["cases"]) == 4


def test_build_context_bundle_benchmark_report_exposes_retained_pattern_counts() -> None:
    result = build_context_bundle_benchmark_report()

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["case_count"] == 4
    assert metrics["retained_case_count"] == 4
    assert metrics["retained_case_pattern_counts"] == {
        "contains_success_pattern": 4,
        "contains_failure_pattern": 4,
    }

    strategy_metrics = metrics["strategy_metrics"]
    assert strategy_metrics["bundles_only"]["success_rate_pct"] == 0.0
    assert (
        strategy_metrics["bundles_plus_dossier_reconstruction_plus_report_revision"][
            "average_efficiency_score"
        ]
        > strategy_metrics["bundles_plus_dossier_reconstruction"][
            "average_efficiency_score"
        ]
    )
    assert any(
        gap["gap_id"] == "missing_retained_success_patterns"
        for gap in result["capability_gaps"]
    ) is False
    assert any(
        gap["gap_id"] == "missing_retained_failure_patterns"
        for gap in result["capability_gaps"]
    ) is False


def test_build_context_bundle_benchmark_report_supports_inline_cases() -> None:
    result = build_context_bundle_benchmark_report(
        case_set="inline_demo",
        cases=[
            {
                "case_id": "demo_case",
                "workflow_class": "repo_document_analysis",
                "task_summary": "Compare context strategies for one retained case.",
                "expected_best_strategy": "bundles_plus_dossier_reconstruction_plus_report_revision",
                "retention_priority": "high",
                "strategies": {
                    "bundles_only": {
                        "success": False,
                        "efficiency_score": 0.2,
                        "interaction_depth": 5,
                        "compression_events": 0,
                        "guardrail_hits": 1,
                        "failure_modes": ["lost_context"],
                    },
                    "bundles_plus_dossier_reconstruction": {
                        "success": True,
                        "efficiency_score": 0.7,
                        "interaction_depth": 3,
                        "compression_events": 1,
                        "guardrail_hits": 0,
                        "failure_modes": [],
                    },
                    "bundles_plus_dossier_reconstruction_plus_report_revision": {
                        "success": True,
                        "efficiency_score": 0.9,
                        "interaction_depth": 2,
                        "compression_events": 2,
                        "guardrail_hits": 0,
                        "failure_modes": [],
                    },
                },
            }
        ],
    )

    assert result["success"] is True
    assert result["corpus"]["source"] == "inline"
    assert result["metrics"]["case_count"] == 1
    assert result["retained_cases"][0]["contains_failure_pattern"] is True
