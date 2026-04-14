from __future__ import annotations

from src.backend.services.context_grounded_answering_benchmark_service import (
    build_context_grounded_answering_benchmark_report,
    load_context_grounded_answering_benchmark_cases,
)


def test_load_context_grounded_answering_benchmark_cases_reads_default_seed_bundle() -> None:
    result = load_context_grounded_answering_benchmark_cases()

    assert result["source"] == "seed_bundle"
    assert result["case_set"] == "phase1_seed"
    assert (
        result["seed_schema_version"]
        == "context_grounded_answering_benchmark.seed_bundle.v1"
    )

    cases = result["cases"]
    assert len(cases) == 5
    assert any(
        case.case_id == "represented_context_tool_pipeline_answer"
        for case in cases
    )


def test_build_context_grounded_answering_benchmark_report_from_seed_bundle() -> None:
    result = build_context_grounded_answering_benchmark_report()

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["scanned_count"] == 5
    assert metrics["exact_path_case_count"] == 5
    assert metrics["telemetry_check_count"] == 10
    assert metrics["validation_surface_count"] == 5
    assert metrics["authoritative_source_counts"] == {
        "authenticated_user_context": 1,
        "authenticated_org_context": 1,
        "workflow_continuation_context": 1,
        "workflow_result": 1,
        "represented_context": 1,
    }
    assert metrics["execution_mode_counts"] == {
        "direct_response": 2,
        "custom_workflow": 2,
        "tool_pipeline": 1,
    }
    assert metrics["answer_property_counts"]["answer_first"] == 5
    assert (
        metrics["coverage_tag_counts"]["represented_context_factual_answering"] == 1
    )

    signal_by_id = {
        signal["signal_id"]: signal for signal in result["benchmark_signals"]
    }
    assert (
        signal_by_id["context_grounded_benchmark_corpus_present"]["status"] == "pass"
    )
    assert (
        signal_by_id["required_context_grounding_classes_present"]["status"]
        == "pass"
    )
    assert signal_by_id["authoritative_sources_represented"]["status"] == "pass"
    assert (
        signal_by_id["execution_modes_cover_direct_tool_and_workflow_paths"]["status"]
        == "pass"
    )
    assert (
        signal_by_id["answer_properties_and_telemetry_checks_declared"]["status"]
        == "pass"
    )
    assert (
        signal_by_id["benchmark_backed_by_exact_path_validation"]["status"] == "pass"
    )

    replay_cases = result["replay_cases"]
    assert len(replay_cases) == 5
    represented_case = next(
        case
        for case in replay_cases
        if case["case_id"] == "represented_context_tool_pipeline_answer"
    )
    assert represented_case["expected_execution_mode"] == "tool_pipeline"
    assert (
        represented_case["validation_surfaces"][0]["reference"]
        == "tests/backend/test_orchestrator_structured_calling.py::test_structured_tool_pipeline_preserves_answer_first_represented_context_response"
    )
