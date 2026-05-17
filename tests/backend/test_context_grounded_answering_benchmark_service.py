from __future__ import annotations

import pytest

from src.backend.services import context_grounded_answering_benchmark_service
from src.backend.services.benchmark_suite_vontology_service import (
    BenchmarkSuiteAuthorityMissingError,
)
from src.backend.services.context_grounded_answering_benchmark_service import (
    build_context_grounded_answering_benchmark_report,
    load_context_grounded_answering_benchmark_cases,
)
from tests.backend.benchmark_suite_test_helpers import (
    represented_suite_case_set_loader,
)


@pytest.fixture(autouse=True)
def _use_represented_context_grounded_answering_benchmark_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        context_grounded_answering_benchmark_service,
        "load_benchmark_suite_case_set",
        represented_suite_case_set_loader,
    )


def test_load_context_grounded_answering_benchmark_cases_reads_represented_suite() -> None:
    result = load_context_grounded_answering_benchmark_cases()

    assert result["source"] == "vontology"
    assert result["case_set"] == "phase1_seed"
    assert (
        result["seed_schema_version"]
        == "context_grounded_answering_benchmark.seed_bundle.v1"
    )

    cases = result["cases"]
    assert len(cases) == 6
    assert any(
        case.case_id == "represented_context_tool_pipeline_answer"
        for case in cases
    )
    assert any(
        case.case_id == "authenticated_entity_information_specialised_workflow"
        for case in cases
    )


def test_build_context_grounded_answering_benchmark_report_from_represented_suite() -> None:
    result = build_context_grounded_answering_benchmark_report()

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["scanned_count"] == 6
    assert metrics["exact_path_case_count"] == 6
    assert metrics["telemetry_check_count"] == 12
    assert metrics["validation_surface_count"] == 6
    assert metrics["authoritative_source_counts"] == {
        "authenticated_user_context": 1,
        "authenticated_org_context": 1,
        "workflow_continuation_context": 1,
        "workflow_result": 2,
        "represented_context": 1,
    }
    assert metrics["execution_mode_counts"] == {
        "direct_response": 2,
        "custom_workflow": 3,
        "tool_pipeline": 1,
    }
    assert metrics["answer_property_counts"]["answer_first"] == 6
    assert (
        metrics["answer_property_counts"]["names_authenticated_user_and_grounded_papers"]
        == 1
    )
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
    assert len(replay_cases) == 6
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
    specialised_entity_case = next(
        case
        for case in replay_cases
        if case["case_id"] == "authenticated_entity_information_specialised_workflow"
    )
    assert specialised_entity_case["expected_execution_mode"] == "custom_workflow"
    assert (
        specialised_entity_case["validation_surfaces"][0]["reference"]
        == "tests/backend/test_von_generate_authenticated_identity_routing.py::test_generate_authenticated_entity_information_turn_routes_to_specialised_workflow"
    )


def test_context_grounded_answering_benchmark_report_fails_closed_when_suite_authority_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing_suite(**_kwargs):
        raise BenchmarkSuiteAuthorityMissingError(
            "benchmark_suite_definition_missing",
            diagnostics={
                "missing_suite_concept_ids": [
                    "#V#context_grounded_answering_benchmark_suite"
                ]
            },
        )

    monkeypatch.setattr(
        context_grounded_answering_benchmark_service,
        "load_benchmark_suite_case_set",
        _missing_suite,
    )

    result = build_context_grounded_answering_benchmark_report()

    assert result["success"] is False
    assert result["error_code"] == "benchmark_suite_authority_missing"
    assert result["corpus"]["source"] == "vontology"
