from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.backend.services.replay_evaluation_authority_service import (
    REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION,
    REPLAY_EVALUATION_RUBRIC_SCHEMA_VERSION,
    ReplayEvaluationRubric,
)
from src.backend.services import replay_arm_planning_service as arm_planning
from src.backend.services import replay_experiment_observation_service as observations


SERVICE_FILES = [
    Path("src/backend/services/replay_arm_planning_service.py"),
    Path("src/backend/services/replay_experiment_observation_service.py"),
    Path("src/backend/services/replay_evaluation_authority_service.py"),
]


def _test_rubric() -> ReplayEvaluationRubric:
    return ReplayEvaluationRubric(
        concept_id="#V#live_prompt_sampler_replay_evaluation_rubric_v1",
        source_predicate="#V#has_replay_evaluation_rubric_json",
        raw_rubric={
            "schema_version": REPLAY_EVALUATION_RUBRIC_SCHEMA_VERSION,
            "rubric_id": "live_prompt_sampler_replay_evaluation",
            "rubric_version": "test",
            "expected_result_schema_version": (
                REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION
            ),
            "verdict_values": ["pass", "partial", "fail", "inconclusive"],
        },
        diagnostics={},
    )


def _represented_evaluation(verdict: str = "pass") -> dict[str, Any]:
    return {
        "schema_version": REPRESENTED_REPLAY_EVALUATION_RESULT_SCHEMA_VERSION,
        "verdict": verdict,
        "rubric_concept_id": "#V#live_prompt_sampler_replay_evaluation_rubric_v1",
        "rubric_version": "test",
        "evaluation_workflow_id": "#V#live_prompt_sampler_replay_evaluation_workflow",
        "authority_source": "represented_replay_evaluation_workflow",
        "candidate_valid": verdict == "pass",
        "policy_decisions": [
            {
                "decision": "production_prompt_policy_unchanged",
                "authorised": False,
                "source": "represented_replay_evaluation_workflow",
            }
        ],
    }


def test_extracted_services_do_not_encode_triggering_failure_policy() -> None:
    service_text = "\n".join(path.read_text(encoding="utf-8") for path in SERVICE_FILES)

    assert "gemma" not in service_text.lower()
    assert "gmail" not in service_text.lower()
    assert "last six email" not in service_text.lower()


def test_replay_arm_planner_builds_variant_arms_without_prompt_policy() -> None:
    arms = arm_planning.build_replay_arm_plan(
        model_arms=[
            {"arm_id": "model_1", "label": "target", "requested_model": "local-small"},
            {
                "arm_id": "model_2",
                "label": "comparator",
                "requested_model": "frontier-medium",
            },
        ],
        base_prompt_id="#V#base_answer_prompt",
        prompt_variant_ids=["#V#target_answer_prompt_variant"],
        workflow_stage_id="turn_answer",
        target_workflow_id="#V#grounded_answer_workflow",
        replay_set_id=None,
        replay_case_id="case-1",
        default_replay_set_id="#V#replay_set",
    )

    assert [arm["label"] for arm in arms] == [
        "target:base_prompt",
        "target:target_answer_prompt_variant",
        "comparator:base_prompt",
        "comparator:target_answer_prompt_variant",
    ]
    assert {arm["base_prompt_id"] for arm in arms} == {"#V#base_answer_prompt"}
    assert {arm["replay_set_id"] for arm in arms} == {"#V#replay_set"}
    assert all("prompt_text" not in arm for arm in arms)


def test_prompt_variant_evaluation_requires_observed_runtime_selection() -> None:
    evaluation = observations.build_prompt_variant_evaluation(
        llm_debug_data={
            "llm_exchange": {
                "prompt_variant_selection": {
                    "base_prompt_concept_id": "#V#base_answer_prompt",
                    "selected_prompt_concept_id": "#V#target_answer_prompt_variant",
                    "match_reason": "model_family",
                }
            }
        },
        arm_metadata={
            "base_prompt_id": "#V#base_answer_prompt",
            "candidate_prompt_variant_id": "#V#different_variant",
        },
        requested_model="local-small",
    )

    assert evaluation["normal_prompt_variant_resolution_observed"] is True
    assert evaluation["selected_prompt_id"] == "#V#target_answer_prompt_variant"
    assert evaluation["candidate_prompt_variant_selected"] is False
    assert "candidate_prompt_variant_not_selected" in evaluation["promotion_blockers"]
    assert evaluation["policy_update"]["authorised"] is False


def test_observation_builder_retains_unscored_collection_non_authoritatively() -> None:
    observation = observations.build_experiment_observation_from_arm_summary(
        {
            "arm": {
                "arm_id": "arm_2",
                "label": "target:variant",
                "requested_model": "local-small",
                "replay_case_id": "case-1",
            },
            "prompt": {"id": "case-1"},
            "conversation": {
                "request_id": "request-1",
                "history_location": {"session_id": "session-1", "history_index": 4},
            },
            "telemetry": {
                "model": "local-small",
                "selected_workflow_id": "#V#grounded_answer_workflow",
                "selected_execution_mode": "custom_workflow",
                "ordinary_turn_terminal_status": "completed",
                "tool_count": 1,
                "timing": {"elapsed_ms": 1234, "llm_elapsed_ms": 900},
                "tool_invocations": [
                    {
                        "tool": "search_records",
                        "status": "success",
                        "effective_payload": {
                            "effect_id": "effect-1",
                            "readback": {"status": "observed"},
                        },
                    }
                ],
            },
            "response": {"text": "Grounded answer."},
            "prompt_variant_evaluation": {
                "base_prompt_id": "#V#base_answer_prompt",
                "candidate_prompt_variant_id": "#V#target_answer_prompt_variant",
                "selected_prompt_id": "#V#target_answer_prompt_variant",
                "candidate_prompt_variant_selected": True,
                "normal_prompt_variant_resolution_observed": True,
                "promotion_blockers": [],
            },
            "replay_scoring_consistency": {
                "completion_gate_status": "partial",
                "response_surface_status": "inconsistent",
                "non_promotable": True,
            },
        },
        default_replay_set_id="#V#replay_set",
    )

    assert observation["verdict"] == "inconclusive"
    assert observation["recordable"] is True
    assert observation["observed_outcome"]["replay_set_id"] == "#V#replay_set"
    assert observation["observed_outcome"]["response_surface_non_promotable"] is True
    assert observation["observed_outcome"]["ordinary_turn_terminal_status"] == (
        "completed"
    )
    assert observation["observed_outcome"]["timing"]["elapsed_ms"] == 1234
    authority = observation["evidence"]["evaluation_authority"]
    assert authority["authoritative"] is False
    assert authority["status"] == "unscored_observation"
    assert observation["policy_decisions"] == []
    assert observation["candidate_validation"]["candidate_kind"] == "prompt_variant"
    assert observation["tool_invocations"] == [
        {
            "tool": "search_records",
            "status": "success",
            "effective_payload": {
                "effect_id": "effect-1",
                "readback": {"status": "observed"},
            },
        }
    ]


def test_observation_builder_uses_represented_evaluation_result_for_verdict() -> None:
    observation = observations.build_experiment_observation_from_arm_summary(
        {
            "arm": {"arm_id": "arm_1", "requested_model": "local-small"},
            "prompt": {"id": "case-1"},
            "conversation": {"request_id": "request-1"},
            "telemetry": {"model": "local-small"},
            "response": {"text": "Looks useful."},
            "prompt_variant_evaluation": {"promotion_blockers": []},
            "replay_scoring_consistency": {"non_promotable": False},
            "represented_replay_evaluation": _represented_evaluation("fail"),
        },
        default_replay_set_id="#V#replay_set",
        replay_evaluation_rubric=_test_rubric(),
        require_represented_evaluation=True,
    )

    assert observation["verdict"] == "fail"
    assert observation["recordable"] is True
    assert observation["evidence"]["unscored_verdict"] == "inconclusive"
    authority = observation["evidence"]["evaluation_authority"]
    assert authority["authoritative"] is True
    assert authority["status"] == "represented_evaluation_result_accepted"
    assert observation["candidate_validation"]["valid"] is False


@dataclass
class _FakeMCPResult:
    payload: dict[str, Any]
    duration_ms: int = 12


class _FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, tool_name: str, payload: dict[str, Any]) -> _FakeMCPResult:
        self.calls.append((tool_name, payload))
        return _FakeMCPResult(
            {
                "success": True,
                "run_id": payload["run_id"],
                "recorded_observations": payload["observations"],
            }
        )


def test_record_experiment_observations_uses_injected_gateway() -> None:
    gateway = _FakeGateway()
    result = observations.record_experiment_observations(
        run_id="#V#experiment_run",
        arm_summaries=[
            {
                "arm": {"arm_id": "arm_1", "requested_model": "local-small"},
                "prompt": {"id": "case-1"},
                "conversation": {"request_id": "request-1"},
                "telemetry": {"model": "local-small"},
                "response": {"text": ""},
                "prompt_variant_evaluation": {"promotion_blockers": []},
                "replay_scoring_consistency": {"non_promotable": False},
                "represented_replay_evaluation": _represented_evaluation("fail"),
            }
        ],
        default_replay_set_id="#V#replay_set",
        gateway=gateway,
        replay_evaluation_rubric=_test_rubric(),
    )

    assert result["success"] is True
    assert result["recorded_observation_count"] == 1
    assert result["turn_execution_request_ids"] == ["request-1"]
    assert result["mcp_tool"] == "experiment_record_observation"
    assert gateway.calls[0][0] == "experiment_record_observation"
    assert gateway.calls[0][1]["run_id"] == "#V#experiment_run"
    recorded_observation = gateway.calls[0][1]["observations"][0]
    assert recorded_observation["verdict"] == "fail"
    assert (
        recorded_observation["evidence"]["evaluation_authority"]["authoritative"]
        is True
    )


def test_record_experiment_observations_can_require_represented_result() -> None:
    gateway = _FakeGateway()
    result = observations.record_experiment_observations(
        run_id="#V#experiment_run",
        arm_summaries=[
            {
                "arm": {"arm_id": "arm_1", "requested_model": "local-small"},
                "prompt": {"id": "case-1"},
                "conversation": {"request_id": "request-1"},
                "telemetry": {"model": "local-small"},
                "response": {"text": "Looks useful."},
                "prompt_variant_evaluation": {"promotion_blockers": []},
                "replay_scoring_consistency": {"non_promotable": False},
            }
        ],
        default_replay_set_id="#V#replay_set",
        gateway=gateway,
        replay_evaluation_rubric=_test_rubric(),
        require_represented_evaluation=True,
    )

    assert result["success"] is False
    assert result["error"] == "replay_evaluation_authority_unavailable"
    assert result["recorded_observation_count"] == 0
    assert result["blocked_observation_count"] == 1
    assert result["blockers"] == ["represented_replay_evaluation_result_missing"]
    assert result["turn_execution_request_ids"] == ["request-1"]
    assert gateway.calls == []


def test_record_experiment_observations_defaults_to_unscored_evidence() -> None:
    gateway = _FakeGateway()
    result = observations.record_experiment_observations(
        run_id="#V#experiment_run",
        arm_summaries=[
            {
                "status": "ok",
                "arm": {"arm_id": "arm_1", "requested_model": "frontier-medium"},
                "prompt": {"id": "case-1"},
                "conversation": {"request_id": "request-1"},
                "telemetry": {
                    "model": "frontier-medium",
                    "ordinary_turn_terminal_status": "completed",
                    "tool_invocations": [
                        {
                            "tool": "relation_upsert",
                            "effective_payload": {
                                "effect_id": "effect-1",
                                "readback": {"status": "observed"},
                            },
                        }
                    ],
                },
                "response": {"text": "Collected answer."},
            }
        ],
        default_replay_set_id="#V#replay_set",
        gateway=gateway,
    )

    assert result["success"] is True
    recorded = gateway.calls[0][1]["observations"][0]
    assert recorded["verdict"] == "inconclusive"
    assert recorded["candidate_validation"]["candidate_kind"] == "replay_arm"
    assert recorded["assertion_classes"][0] == "model_replay_arm_observation"
    assert recorded["evidence"]["evaluation_authority"]["status"] == (
        "unscored_observation"
    )
    assert recorded["tool_invocations"][0]["effective_payload"]["readback"] == {
        "status": "observed"
    }


def test_completion_gate_is_observed_but_not_a_scoring_blocker() -> None:
    consistency = observations.build_replay_scoring_consistency(
        llm_debug_data={
            "completion_gate_verdict": {
                "status": "partial",
                "safe_to_claim_completion": False,
            }
        }
    )

    assert consistency["completion_gate_status"] == "partial"
    assert consistency["promotion_blockers"] == []
    assert consistency["structural_blockers"] == []
    assert consistency["non_promotable"] is False
