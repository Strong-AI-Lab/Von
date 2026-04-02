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
