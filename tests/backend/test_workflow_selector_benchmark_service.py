from __future__ import annotations

from src.backend.services.workflow_selector_benchmark_service import (
    build_selector_routing_benchmark_report,
    load_selector_routing_benchmark_cases,
)
from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)


def test_load_selector_routing_benchmark_cases_reads_default_seed_bundle() -> None:
    result = load_selector_routing_benchmark_cases()

    assert result["source"] == "seed_bundle"
    assert result["case_set"] == "phase1_seed"
    assert result["seed_schema_version"] == "selector_routing_benchmark.seed_bundle.v1"

    cases = result["cases"]
    assert len(cases) >= 5
    assert any(case.case_id == "known_plain_response_misroute" for case in cases)


def test_build_selector_routing_benchmark_report_from_seed_bundle() -> None:
    result = build_selector_routing_benchmark_report()

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["scanned_count"] == 5
    assert metrics["matched_case_count"] == 4
    assert metrics["selector_accuracy_pct"] == 80.0
    assert metrics["baseline_accuracy_pct"] == 20.0
    assert metrics["accuracy_improvement_pct"] == 60.0
    assert metrics["abstain_case_count"] == 1
    assert metrics["abstain_matched_count"] == 1
    assert metrics["misrouting_count"] == 1
    assert metrics["outcome_label_counts"]["successful_completion"] == 3
    assert metrics["outcome_label_counts"]["abstain_escalate_no_safe_route"] == 1
    assert metrics["outcome_label_counts"]["tool_or_workflow_misrouting"] == 1

    replay_cases = result["replay_cases"]
    assert len(replay_cases) == 5
    misroute_case = next(case for case in replay_cases if case["case_id"] == "known_plain_response_misroute")
    assert misroute_case["overall_outcome"] == "tool_or_workflow_misrouting"
    assert misroute_case["selected_workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID


def test_build_selector_routing_benchmark_report_supports_prompt_failure_cases() -> None:
    result = build_selector_routing_benchmark_report(
        cases=[
            {
                "case_id": "prompt_fail_safe_route",
                "turn_text": "Do a risky thing without a safe route.",
                "candidate_workflows": [
                    {
                        "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                        "name": "Chat assistant workflow",
                    },
                    {
                        "concept_id": TOOL_CALLING_WORKFLOW_ID,
                        "name": "Tool calling workflow",
                    },
                ],
                "expected_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
                "expected_routing_outcome": "abstain_escalate_no_safe_route",
                "baseline_workflow_id": TOOL_CALLING_WORKFLOW_ID,
                "prompt_failure_reason": "selector_prompt_unavailable",
            }
        ]
    )

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["scanned_count"] == 1
    assert metrics["abstain_case_count"] == 1
    assert metrics["abstain_matched_count"] == 1
    assert metrics["outcome_label_counts"]["abstain_escalate_no_safe_route"] == 1

    replay_case = result["replay_cases"][0]
    assert replay_case["selection_source"] == "selector_fail_closed"
    assert replay_case["selected_workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert replay_case["overall_outcome"] == "abstain_escalate_no_safe_route"


def test_selector_benchmark_preserves_candidate_metadata_for_generic_review() -> None:
    result = build_selector_routing_benchmark_report(
        cases=[
            {
                "case_id": "generic_workflow_execute_candidate_review",
                "turn_text": "Represent the most recent grounded artefact.",
                "candidate_workflows": [
                    {
                        "concept_id": "#V#grounded_artifact_representation_workflow",
                        "name": "Grounded artefact representation workflow",
                        "description": "Represents a grounded artefact.",
                        "candidate_source": "workflow_discovery",
                        "routing_eligible": True,
                        "is_executable": True,
                        "is_policy_safe": True,
                        "turn_launchable": True,
                    },
                    {
                        "concept_id": TOOL_CALLING_WORKFLOW_ID,
                        "name": "Tool calling workflow",
                        "description": "Generic tool workflow.",
                        "candidate_source": "selector_default",
                        "candidate_reason": "builtin_selector_candidate",
                    },
                ],
                "expected_workflow_id": TOOL_CALLING_WORKFLOW_ID,
                "baseline_workflow_id": TOOL_CALLING_WORKFLOW_ID,
                "selector_response": {
                    "workflow_id": TOOL_CALLING_WORKFLOW_ID,
                    "confidence": 0.91,
                    "reasoning": "The request needs tools.",
                },
            }
        ]
    )

    replay_case = result["replay_cases"][0]
    assert replay_case["candidate_workflows"][0]["candidate_source"] == (
        "workflow_discovery"
    )
    assert replay_case["candidate_workflows"][0]["turn_launchable"] is True
    assert replay_case["selection_metadata"].get(
        "generic_builtin_selection_has_eligible_discovered_candidates"
    ) is True
    assert replay_case["selection_metadata"].get(
        "eligible_specialised_candidate_ids"
    ) == ["#V#grounded_artifact_representation_workflow"]


def test_load_selector_routing_benchmark_cases_supports_entity_representation_case_set() -> None:
    result = load_selector_routing_benchmark_cases(
        case_set="entity_representation_generalisation"
    )

    assert result["source"] == "seed_bundle"
    assert result["case_set"] == "entity_representation_generalisation"

    cases = result["cases"]
    assert len(cases) == 11
    assert any(
        case.case_id == "entity_people_follow_up_reasoning_override"
        for case in cases
    )
    assert any(
        case.case_id == "entity_event_write_with_storage_verb"
        for case in cases
    )
    assert any(
        case.case_id == "entity_event_write_with_capture_verb"
        for case in cases
    )
    assert any(
        case.case_id == "unsupported_domain_safe_authoring_recovery"
        for case in cases
    )


def test_build_selector_routing_benchmark_report_for_entity_representation_case_set() -> None:
    result = build_selector_routing_benchmark_report(
        case_set="entity_representation_generalisation"
    )

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["scanned_count"] == 11
    assert metrics["matched_case_count"] == 11
    assert metrics["selector_accuracy_pct"] == 100.0
    assert metrics["baseline_accuracy_pct"] == 0.0
    assert metrics["accuracy_improvement_pct"] == 100.0
    assert metrics["abstain_case_count"] == 1
    assert metrics["abstain_matched_count"] == 1
    assert metrics["misrouting_count"] == 0
    assert metrics["outcome_label_counts"]["successful_completion"] == 10
    assert metrics["outcome_label_counts"]["abstain_escalate_no_safe_route"] == 1
    assert metrics["outcome_label_counts"]["tool_or_workflow_misrouting"] == 0

    replay_cases = result["replay_cases"]
    assert len(replay_cases) == 11
    follow_up_case = next(
        case
        for case in replay_cases
        if case["case_id"] == "entity_people_follow_up_reasoning_override"
    )
    assert follow_up_case["selected_workflow_id"] == "#V#entity_representation_workflow"
    assert follow_up_case["overall_outcome"] == "successful_completion"

    recovery_case = next(
        case
        for case in replay_cases
        if case["case_id"] == "unsupported_domain_safe_authoring_recovery"
    )
    assert (
        recovery_case["selected_workflow_id"]
        == "#V#workflow_discovery_gap_recovery_workflow"
    )
    assert recovery_case["overall_outcome"] == "abstain_escalate_no_safe_route"

    signal_by_id = {
        signal["signal_id"]: signal for signal in result["benchmark_signals"]
    }
    assert signal_by_id["selector_benchmark_corpus_present"]["status"] == "pass"
    assert (
        signal_by_id["selector_accuracy_not_worse_than_baseline"]["status"] == "pass"
    )
    assert signal_by_id["abstain_cases_routed_safely"]["status"] == "pass"
    assert (
        signal_by_id["selector_misrouting_examples_detected"]["status"]
        == "not_evaluated"
    )


def test_load_selector_routing_benchmark_cases_supports_corrective_evidence_failure_family_case_set() -> None:
    result = load_selector_routing_benchmark_cases(
        case_set="corrective_evidence_failure_family"
    )

    assert result["source"] == "seed_bundle"
    assert result["case_set"] == "corrective_evidence_failure_family"

    cases = result["cases"]
    assert len(cases) == 2
    assert cases[0].replay_family_id == "strong_ai_lab_su_yuchen_corrective_evidence"
    assert cases[0].source_session_id == "e1ca7cde-9341-420f-8cc4-908db1218bd8"
    assert (
        "ab7ca424-373a-4b0f-9d26-f8075c549546" in cases[0].source_request_ids
    )
    assert "follow_up_turn" in cases[1].case_tags


def test_build_selector_routing_benchmark_report_for_corrective_evidence_failure_family_case_set() -> None:
    result = build_selector_routing_benchmark_report(
        case_set="corrective_evidence_failure_family"
    )

    assert result["success"] is True
    metrics = result["metrics"]
    assert metrics["scanned_count"] == 2
    assert metrics["matched_case_count"] == 2
    assert metrics["selector_accuracy_pct"] == 100.0
    assert metrics["baseline_accuracy_pct"] == 0.0
    assert metrics["outcome_label_counts"]["successful_completion"] == 2

    replay_cases = result["replay_cases"]
    assert len(replay_cases) == 2
    initial_case = next(
        case
        for case in replay_cases
        if case["case_id"] == "su_yuchen_initial_specialised_vontology_route"
    )
    assert (
        initial_case["selected_workflow_id"]
        == "#V#specialised_vontology_search_workflow"
    )
    assert (
        initial_case["replay_family_id"]
        == "strong_ai_lab_su_yuchen_corrective_evidence"
    )
    assert (
        initial_case["source_session_id"] == "e1ca7cde-9341-420f-8cc4-908db1218bd8"
    )
    assert "selector_dispatch_divergence" in initial_case["case_tags"]

    follow_up_case = next(
        case
        for case in replay_cases
        if case["case_id"] == "su_yuchen_follow_up_requires_stronger_verification"
    )
    assert "follow_up_turn" in follow_up_case["case_tags"]
    assert (
        "d4328819-168c-4381-989c-f5a7cd1a639d"
        in follow_up_case["source_request_ids"]
    )
