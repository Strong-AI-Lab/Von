from __future__ import annotations

import time
from typing import Any, cast
from unittest.mock import MagicMock, patch

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services.turn_execution_record_service import (
    _derive_completion_gate,
    build_turn_execution_correctness_summary,
)
from src.backend.workflows.action_registry import WorkflowActionRequest, WorkflowEnvironment


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: dict[str, Any]) -> Any:
        raise RuntimeError("invoke should not be called in this test")


class _GatewayResult:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


class _RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, tool_name: str, payload: dict[str, Any]) -> _GatewayResult:
        self.calls.append((tool_name, payload))
        if tool_name == "workflow_create_instance":
            return _GatewayResult(
                {"success": True, "instance_id": "wf-maint-1", "status": "pending"}
            )
        return _GatewayResult({"success": True})


def _build_orchestrator() -> InternalMCPChatOrchestrator:
    # Avoid full orchestrator initialisation in unit tests.
    # Constructor bootstrap can require external workflow registry state that is
    # irrelevant for testing isolated turn-execution actions.
    return cast(
        InternalMCPChatOrchestrator,
        object.__new__(InternalMCPChatOrchestrator),
    )


def _build_request(
    *,
    action_id: str,
    data: dict[str, Any],
) -> WorkflowActionRequest:
    env = WorkflowEnvironment(
        llm_client=object(),
        gateway=cast(Any, _DummyGateway()),
        model="test-model",
        user_namespace="#V#test_user",
    )
    return WorkflowActionRequest(
        action_id=action_id,
        inputs={},
        environment=env,
        data=data,
    )


def _patch_representation_profile_loader(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_representation_domain_profiles_from_vontology",
        lambda: (
            [
                {
                    "profile_id": "paper",
                    "profile_concept_id": "#V#representation_contract_profile_paper",
                    "target_entity_class": "scholarly_paper",
                    "effect_type": "scholarly_representation",
                    "description": "Represent paper metadata.",
                    "intent_patterns": [
                        r"\bpaper\s+representation\b",
                        r"\bcorresponding\s+paper\b",
                    ],
                    "domain_terms": ["paper", "arxiv", "metadata", "abstract"],
                    "required_tools_by_source": {
                        "file_copy": [
                            "materialise_scholarly_representation_for_file_copy"
                        ],
                        "url": [
                            "download_paper",
                            "materialise_scholarly_representation_for_file_copy",
                        ],
                        "mixed": [
                            "download_paper",
                            "materialise_scholarly_representation_for_file_copy",
                        ],
                        "unknown": [
                            "materialise_scholarly_representation_for_file_copy"
                        ],
                    },
                    "required_predicates": [
                        "#V#computer_file_for_propositional_information_thing",
                        "#V#propositional_information_thing_has_computer_file",
                    ],
                    "default_decision_policy": {
                        "completion_block_on_unresolved_effects": True,
                        "fail_closed_on_missing_requirements": True,
                        "auto_apply_low_risk_defaults": True,
                        "requires_explicit_user_decision_for_high_risk": True,
                    },
                }
            ],
            {
                "representation_profile_source": "vontology_concept_text_relations",
                "requested_concept_ids": ["#V#representation_contract_profile_paper"],
                "loaded_concept_ids": ["#V#representation_contract_profile_paper"],
                "loaded_profile_count": 1,
                "profile_version_hash": "hash-paper-profile",
            },
        ),
    )


def test_turn_execution_critic_detects_unresolved_kb_mutation() -> None:
    orchestrator = _build_orchestrator()
    aux_llm_calls: list[dict[str, Any]] = []
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Those relations were not added. Proceed with the predicates.",
            "final_response": "Done.",
            "invocations": [],
            "aux_llm_calls": aux_llm_calls,
            "turn_id": "req-turn-critic-1",
            "conversation_session_id": "session-critic-1",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)

    assert result.ok
    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    assert record.get("request_id") == "req-turn-critic-1"
    assert record.get("actor_concept_id") == "#V#test_user"

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    assert required_effects[0]["status"] == "not_executed"
    assert required_effects[0]["effect_type"] == "tool_execution"
    assert required_effects[0]["failure_code"] == "tool_dispatch_boundary_missing"
    assert required_effects[0]["failure_codes"] == ["tool_dispatch_boundary_missing"]

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("blocking_failure_codes") == [
        "tool_dispatch_boundary_missing"
    ]
    evidence_payload = completion_gate.get("evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("completion_outcome") == "failure"
    unresolved_preconditions = evidence_payload.get("unresolved_preconditions")
    assert isinstance(unresolved_preconditions, list)
    assert unresolved_preconditions

    assert any(
        entry.get("type") == "turn_execution_critic"
        for entry in aux_llm_calls
        if isinstance(entry, dict)
    )


def test_turn_execution_critic_prefers_explicit_actor_concept_id() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Please proceed",
            "final_response": "Done.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-actor",
            "conversation_session_id": "session-critic-actor",
            "actor_concept_id": "#V#github_copilot_instance",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#chat_assistant_workflow",
                "verdict": "plain_response",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok
    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    assert record.get("actor_concept_id") == "#V#github_copilot_instance"


def test_turn_execution_critic_prefers_concrete_verification_reads() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Please add this relation to the knowledge base.",
            "final_response": "Response recorded.",
            "invocations": [
                {"tool": "add_relationship", "payload": {"status": "ok"}},
                {"tool": "fetch_concept", "payload": {"status": "ok"}},
            ],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-verified",
            "conversation_session_id": "session-critic-verified",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok
    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    postcondition_checks = record.get("postcondition_checks")
    assert isinstance(postcondition_checks, list)
    assert postcondition_checks
    check = postcondition_checks[0]
    assert check.get("status") == "verified"
    assert check.get("verification_mode") == "state_requery_observed"
    observed = check.get("observed")
    assert isinstance(observed, dict)
    assert "fetch_concept" in list(observed.get("successful_verification_tools") or [])

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "completed"
    evidence_payload = completion_gate.get("evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("completion_outcome") == "success"


def test_turn_completion_gate_appends_execution_status_for_unresolved_effect() -> None:
    orchestrator = _build_orchestrator()
    aux_llm_calls: list[dict[str, Any]] = []
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "aux_llm_calls": aux_llm_calls,
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                    "blocking_failure_codes": ["worker_unavailable_zero_execution"],
                    "evidence_payload": {
                        "unresolved_preconditions": [
                            {
                                "effect_id": "effect_1",
                                "effect_type": "tool_execution",
                                "status": "not_executed",
                                "status_reason": (
                                    "Tool execution was blocked while workers "
                                    "were unavailable."
                                ),
                                "failure_codes": ["worker_unavailable_zero_execution"],
                            }
                        ]
                    },
                }
            },
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)

    assert result.ok
    assert result.outputs.get("completion_gate_decision") == "escalation_required"
    assert result.outputs.get("completion_gate_safe_to_claim_completion") is False
    assert result.outputs.get("completion_gate_requires_follow_up") is True
    assert result.outputs.get("completion_gate_terminal_outcome") == "retrying"
    assert result.outputs.get("completion_gate_blocking_failure_codes") == [
        "worker_unavailable_zero_execution"
    ]
    unresolved_preconditions = result.outputs.get("completion_gate_unresolved_preconditions")
    assert isinstance(unresolved_preconditions, list)
    assert unresolved_preconditions

    final_response = result.outputs.get("final_response")
    assert isinstance(final_response, str)
    assert "Execution status:" in final_response
    assert "Blocking effect IDs: effect_1." in final_response
    assert "Unresolved preconditions:" in final_response
    assert "Failure codes: worker_unavailable_zero_execution." in final_response

    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "retrying"

    assert any(
        entry.get("type") == "turn_completion_gate"
        for entry in aux_llm_calls
        if isinstance(entry, dict)
    )


def test_turn_completion_gate_backfills_failure_codes_for_unresolved_preconditions() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                    "evidence_payload": {
                        "unresolved_preconditions": [
                            {
                                "effect_id": "effect_1",
                                "effect_type": "kb_mutation",
                                "status": "not_executed",
                                "status_reason": "No write-capable tool invocation was observed.",
                            }
                        ]
                    },
                }
            },
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)

    assert result.ok
    assert result.outputs.get("completion_gate_blocking_failure_codes") == [
        "required_effect_not_executed",
        "required_effects_unresolved",
    ]


def test_turn_execution_critic_flags_missing_non_kb_mutation_execution() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Please create a Jira task for this regression and assign it to me.",
            "final_response": "Done.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-2",
            "conversation_session_id": "session-critic-2",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    assert required_effects[0]["status"] == "not_executed"
    assert required_effects[0]["effect_type"] == "tool_execution"
    assert required_effects[0]["failure_code"] == "tool_dispatch_boundary_missing"
    assert required_effects[0]["failure_codes"] == ["tool_dispatch_boundary_missing"]

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("blocking_failure_codes") == [
        "tool_dispatch_boundary_missing"
    ]


def test_turn_execution_critic_treats_diagnostic_prompt_as_non_mutating() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": (
                "You didn't actually create the task instance this time. "
                "Inspect the telemetry and explain why."
            ),
            "final_response": "No mutation was attempted in this turn.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-3",
            "conversation_session_id": "session-critic-3",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    assert required_effects[0]["effect_type"] == "tool_execution"
    assert required_effects[0]["status"] == "not_executed"

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_turn_execution_critic_flags_worker_unavailable_zero_execution() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Yes",
            "final_response": "Completed.",
            "invocations": [],
            "aux_llm_calls": [],
            "turn_id": "req-turn-critic-worker-unavailable",
            "conversation_session_id": "session-critic-worker-unavailable",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
            "turn_execution_diagnostics": {
                "latest_progress": {
                    "counters": {"tools_started": 0, "tools_completed": 0},
                    "diagnostic_events": [
                        {
                            "stage": "tool_plan",
                            "phase": "tool_plan",
                            "event_kind": "heartbeat",
                            "liveness_reason": "worker_unavailable",
                        }
                    ],
                }
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    effect = required_effects[0]
    assert effect.get("effect_type") == "tool_execution"
    assert effect.get("status") == "not_executed"
    assert "worker_unavailable_zero_execution" in list(effect.get("failure_codes") or [])

    execution = record.get("execution")
    assert isinstance(execution, dict)
    execution_summary = execution.get("summary")
    assert isinstance(execution_summary, dict)
    assert execution_summary.get("planned_count") == 1
    assert execution_summary.get("executed_count") == 0

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("decision_reason") == "Required tool execution was not observed."
    assert completion_gate.get("safe_to_claim_completion") is False


def test_turn_execution_critic_flags_missing_scholarly_representation(monkeypatch) -> None:
    _patch_representation_profile_loader(monkeypatch)
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Fully represent the corresponding paper.",
            "final_response": "I have represented the paper.",
            "invocations": [],
            "aux_llm_calls": [
                {
                    "type": "prompt_tool_requirements",
                    "required_scholarly_representation_for_file_copy_ids": [
                        "#V#uploaded_file_copy_76c1c13fed0140f496133d008b4cfad7"
                    ],
                }
            ],
            "turn_id": "req-turn-critic-scholarly-1",
            "conversation_session_id": "session-critic-scholarly-1",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    effect = required_effects[0]
    assert effect.get("effect_type") == "tool_execution"
    assert effect.get("status") == "not_executed"
    assert effect.get("failure_code") == "tool_dispatch_boundary_missing"

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("decision_reason") == "Required tool execution was not observed."
    assert completion_gate.get("safe_to_claim_completion") is False


def test_turn_execution_critic_marks_scholarly_representation_satisfied(monkeypatch) -> None:
    _patch_representation_profile_loader(monkeypatch)
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Fully represent the corresponding paper.",
            "final_response": "Paper representation has been produced.",
            "invocations": [
                {
                    "tool": "materialise_scholarly_representation_for_file_copy",
                    "payload": {
                        "success": True,
                        "concept_id": "#V#uploaded_file_copy_76c1c13fed0140f496133d008b4cfad7",
                        "verified": True,
                        "paper_concept_id": "#V#paper_on_arxiv_76c1c13f",
                        "scholarly_representation": {
                            "attempted": True,
                            "verified": True,
                            "paper_concept_id": "#V#paper_on_arxiv_76c1c13f",
                        },
                    },
                }
            ],
            "aux_llm_calls": [
                {
                    "type": "prompt_tool_requirements",
                    "required_scholarly_representation_for_file_copy_ids": [
                        "#V#uploaded_file_copy_76c1c13fed0140f496133d008b4cfad7"
                    ],
                }
            ],
            "turn_id": "req-turn-critic-scholarly-2",
            "conversation_session_id": "session-critic-scholarly-2",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    assert all(effect.get("status") == "satisfied" for effect in required_effects)
    assert any(
        effect.get("effect_type") == "scholarly_representation"
        for effect in required_effects
    )

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_turn_execution_critic_blocks_failed_custom_workflow_dispatch() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Show me all the SAIL PhD students.",
            "final_response": "I reviewed the student list.",
            "invocations": [],
            "aux_llm_calls": [
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "execution_mode_selected",
                    "status": "selected",
                    "selected_execution_mode": "custom_workflow",
                    "selected_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                    "dispatch_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                },
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_handoff",
                    "status": "started",
                    "selected_execution_mode": "custom_workflow",
                    "selected_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                    "dispatch_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                },
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_terminal",
                    "status": "failed",
                    "selected_execution_mode": "custom_workflow",
                    "selected_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                    "dispatch_workflow_id": "#V#sail_phd_student_onboarding_workflow",
                    "completed": False,
                    "reason": (
                        "metadata_validation_failed:"
                        "metadata_write_context_key_missing:"
                        "#V#onboarding_step_collect_student_info:"
                        "#V#workflow_context_key_validated_type_name"
                    ),
                    "detail": (
                        "Workflow metadata validation failed before any action could "
                        "start because context key "
                        "#V#workflow_context_key_validated_type_name was missing."
                    ),
                    "failing_state_id": "#V#onboarding_step_collect_student_info",
                    "failing_action_id": "tool.collect_student_info",
                },
                {
                    "type": "workflow_execution",
                    "workflow_id": "#V#sail_phd_student_onboarding_workflow",
                    "final_state": "#V#onboarding_step_collect_student_info",
                    "completed": False,
                    "execution_summary": {
                        "schema_version": "workflow_execution_summary.v1",
                        "workflow_id": "#V#sail_phd_student_onboarding_workflow",
                        "completed": False,
                        "final_state": "#V#onboarding_step_collect_student_info",
                        "step_result_envelope_count": 0,
                        "action_started_count": 0,
                        "action_completed_count": 0,
                        "action_success_count": 0,
                        "action_failure_count": 0,
                        "action_unknown_count": 0,
                        "first_failing_state_id": "#V#onboarding_step_collect_student_info",
                        "first_failing_action_id": "tool.collect_student_info",
                        "runtime_event_count": 0,
                        "terminal_effect_count": 0,
                        "terminal_effects": [],
                        "durable_side_effect_count": 0,
                        "durable_side_effects": [],
                    },
                },
            ],
            "turn_id": "req-turn-critic-custom-workflow-fail",
            "conversation_session_id": "session-critic-custom-workflow-fail",
            "workflow_discovery_result": {
                "matches": [
                    {"concept_id": "#V#sail_phd_student_onboarding_workflow"}
                ]
            },
            "workflow_routing": {
                "workflow_id": "#V#sail_phd_student_onboarding_workflow",
                "verdict": "rag_selected",
                "source": "selector",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 1
    effect = required_effects[0]
    assert effect.get("effect_type") == "workflow_execution"
    assert effect.get("status") == "not_executed"
    assert effect.get("failure_code") == (
        "metadata_validation_failed:"
        "metadata_write_context_key_missing:"
        "#V#onboarding_step_collect_student_info:"
        "#V#workflow_context_key_validated_type_name"
    )

    postcondition_checks = record.get("postcondition_checks")
    assert isinstance(postcondition_checks, list)
    assert len(postcondition_checks) == 1
    assert postcondition_checks[0].get("check_type") == "workflow_execution_observed"
    assert postcondition_checks[0].get("status") == "not_verified"

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert completion_gate.get("blocking_failure_codes") == [
        "custom_workflow_failed_before_tool_invocation",
        "metadata_validation_failed:"
        "metadata_write_context_key_missing:"
        "#V#onboarding_step_collect_student_info:"
        "#V#workflow_context_key_validated_type_name",
    ]
    assert "before any action could start" in str(
        completion_gate.get("decision_reason") or ""
    )


def test_turn_execution_critic_blocks_planned_tool_run_without_success() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.critic",
        data={
            "prompt": "Explain the failure in more detail from the telemetry.",
            "final_response": "I checked the telemetry and everything completed normally.",
            "invocations": [
                {
                    "tool": "conversation_telemetry_get_locator",
                    "error": "PERMISSION_DENIED",
                    "payload": {
                        "status": "error",
                        "summary": "Error: PERMISSION_DENIED",
                    },
                },
                {
                    "tool": "chat_history_get_segments",
                    "error": "PERMISSION_DENIED",
                    "payload": {
                        "status": "error",
                        "summary": "Error: PERMISSION_DENIED",
                    },
                },
            ],
            "aux_llm_calls": [
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "execution_mode_selected",
                    "status": "selected",
                    "selected_execution_mode": "tool_pipeline",
                },
                {
                    "type": "workflow_dispatch_boundary",
                    "boundary": "workflow_handoff",
                    "status": "started",
                    "selected_execution_mode": "tool_pipeline",
                },
            ],
            "turn_id": "req-turn-critic-read-fail",
            "conversation_session_id": "session-critic-read-fail",
            "workflow_discovery_result": None,
            "workflow_routing": {
                "workflow_id": "#V#tool_calling_workflow",
                "verdict": "tool_seeking",
            },
        },
    )

    result = orchestrator._action_turn_execution_critic(request)
    assert result.ok

    record = result.outputs.get("turn_execution_record")
    assert isinstance(record, dict)
    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects == []

    completion_gate = record.get("completion_gate")
    assert isinstance(completion_gate, dict)
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert completion_gate.get("repeat_eligible") is False
    assert completion_gate.get("blocking_failure_codes") == [
        "planned_tool_execution_without_success"
    ]
    evidence_payload = completion_gate.get("evidence_payload")
    assert isinstance(evidence_payload, dict)
    blocker = evidence_payload.get("execution_signal_blocker")
    assert isinstance(blocker, dict)
    assert blocker.get("effect_type") == "tool_execution"
    assert blocker.get("status") == "not_satisfied"


def test_turn_execution_correctness_flags_completed_planned_tool_run_without_success_as_false_success() -> None:
    correctness = build_turn_execution_correctness_summary(
        completion_gate={
            "decision": "completed",
            "safe_to_claim_completion": True,
            "requires_follow_up": False,
        },
        required_effects=[],
        critic_summary={"not_verified_count": 0},
        final_response={
            "completion_claim_detected": False,
            "completion_claim_validated": True,
        },
        workflow_selection={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "tool_seeking",
        },
        workflow_routing_diagnostics={
            "dispatch": {
                "selected_execution_mode": "tool_pipeline",
                "planned_count": 2,
                "executed_count": 2,
                "successful_invocation_count": 0,
                "failed_invocation_count": 2,
                "blocked_invocation_count": 0,
                "failure_codes": [],
            }
        },
    )

    assert correctness["failure_mode"] == "false_completion_gate_state"
    assert correctness["overall_outcome"] == "false_success"
    assert correctness["metric_labels"]["false_success"] is True


def test_derive_completion_gate_blocks_workflow_terminal_failure_without_required_effects() -> None:
    completion_gate = _derive_completion_gate(
        required_effects=[],
        postcondition_checks=[],
        completion_claim_validated=True,
        execution_summary={
            "selected_execution_mode": "custom_workflow",
            "dispatch_workflow_id": "#V#diagnostic_workflow",
            "dispatch_terminal_status": "failed",
            "dispatch_terminal_failure_reason": "workflow_runtime_failed",
            "dispatch_terminal_failure_detail": (
                "Diagnostic workflow failed before any workflow action could start."
            ),
            "planned_count": 0,
            "executed_count": 0,
            "successful_invocation_count": 0,
            "failed_invocation_count": 0,
            "blocked_invocation_count": 0,
            "failure_codes": ["workflow_runtime_failed"],
            "custom_workflow_execution": {
                "action_started_count": 0,
                "action_completed_count": 0,
                "action_failure_count": 0,
                "durable_side_effect_count": 0,
                "terminal_effect_count": 0,
            },
        },
    )

    assert completion_gate["decision"] == "escalation_required"
    assert completion_gate["safe_to_claim_completion"] is False
    assert completion_gate["requires_follow_up"] is True
    assert completion_gate["repeat_eligible"] is False
    assert completion_gate["blocking_failure_codes"] == ["workflow_runtime_failed"]
    evidence_payload = completion_gate["evidence_payload"]
    assert evidence_payload["evaluation_basis"] == (
        "required_effects_postcondition_checks_and_execution_signals"
    )


def test_turn_completion_gate_does_not_repeat_terminal_execution_failure() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I checked the telemetry.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "failed",
                    "decision_reason": (
                        "Planned tool execution completed without any successful tool result."
                    ),
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "repeat_eligible": False,
                    "blocking_effect_ids": ["effect_execution_signal_1"],
                    "blocking_failure_codes": [
                        "planned_tool_execution_without_success"
                    ],
                    "evidence_payload": {
                        "unresolved_preconditions": [
                            {
                                "effect_id": "effect_execution_signal_1",
                                "effect_type": "tool_execution",
                                "status": "not_satisfied",
                                "status_reason": (
                                    "Planned tool execution completed without any successful tool result."
                                ),
                                "failure_codes": [
                                    "planned_tool_execution_without_success"
                                ],
                            }
                        ]
                    },
                }
            },
            "completion_gate_loop_attempts": 0,
            "completion_gate_loop_max_attempts": 3,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert result.outputs.get("completion_gate_repeat_eligible") is False
    assert result.outputs.get("completion_gate_loop_stop_reason") == (
        "terminal_execution_failure"
    )
    assert result.outputs.get("completion_gate_terminal_outcome") == "failed"
    assert result.outputs.get("completion_gate_escalation_signal") is True
    assert result.outputs.get("completion_gate_escalation_reason") == (
        "terminal_execution_failure"
    )


def test_turn_completion_gate_requests_repeat_when_budget_available() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 0,
            "completion_gate_loop_max_attempts": 1,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is True
    assert result.outputs.get("completion_gate_loop_attempts") == 1
    assert result.outputs.get("completion_gate_loop_stop_reason") is None
    assert result.outputs.get("completion_gate_requires_follow_up") is True
    assert result.outputs.get("completion_gate_terminal_outcome") == "retrying"
    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "retrying"


def test_turn_completion_gate_stops_repeat_when_attempt_budget_exhausted() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 1,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert (
        result.outputs.get("completion_gate_loop_stop_reason")
        == "attempt_budget_exhausted"
    )
    assert (
        result.outputs.get("completion_gate_terminal_outcome")
        == "attempt_budget_exhausted"
    )
    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "attempt_budget_exhausted"


def test_turn_completion_gate_stops_repeat_when_no_progress_guard_triggers() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 3,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 1,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "escalation_required:effect_1",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert (
        result.outputs.get("completion_gate_loop_stop_reason")
        == "no_progress_guard_triggered"
    )
    assert (
        result.outputs.get("completion_gate_terminal_outcome")
        == "no_progress_guard_triggered"
    )
    assert result.outputs.get("completion_gate_escalation_signal") is True
    assert (
        result.outputs.get("completion_gate_escalation_reason")
        == "no_progress_guard_triggered"
    )


def test_turn_completion_gate_stops_repeat_when_stall_latency_budget_exhausted() -> None:
    orchestrator = _build_orchestrator()
    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "final_response": "I have completed the update.",
            "invocations": [],
            "turn_execution_record": {
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Required mutation was not executed.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                }
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 5,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 5,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "escalation_required:effect_1",
            "completion_gate_loop_stall_started_monotonic": time.monotonic() - 2.0,
            "completion_gate_loop_stall_elapsed_ms": 0,
            "completion_gate_loop_stall_max_elapsed_ms": 500,
            "completion_gate_loop_stall_events": 0,
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok
    assert result.outputs.get("completion_gate_repeat_iteration") is False
    assert (
        result.outputs.get("completion_gate_loop_stop_reason")
        == "stall_latency_budget_exhausted"
    )
    assert (
        result.outputs.get("completion_gate_terminal_outcome")
        == "stall_latency_budget_exhausted"
    )
    assert result.outputs.get("completion_gate_escalation_signal") is True
    assert (
        result.outputs.get("completion_gate_escalation_reason")
        == "stall_latency_budget_exhausted"
    )
    assert int(result.outputs.get("completion_gate_loop_stall_events") or 0) >= 1
    assert int(result.outputs.get("completion_gate_loop_stall_elapsed_ms") or 0) >= 500
    final_response = result.outputs.get("final_response")
    assert isinstance(final_response, str)
    assert "Execution status:" in final_response
    assert "Escalation trigger:" not in final_response
    assert "stall_latency_budget_exhausted" not in final_response
    evidence_payload = result.outputs.get("completion_gate_evidence_payload")
    assert isinstance(evidence_payload, dict)
    assert evidence_payload.get("terminal_outcome") == "stall_latency_budget_exhausted"
    assert evidence_payload.get("escalation_signal") is True
    assert evidence_payload.get("escalation_reason") == "stall_latency_budget_exhausted"


@patch(
    "src.backend.services.workflow_event_integration_service.maybe_launch_episode_evaluation_for_turn_completion_gate"
)
def test_turn_completion_gate_autotriggers_episode_evaluation(
    mock_launch_episode_evaluation: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_WORKFLOW_INTROSPECTION_AUTOTRIGGER_ENABLE", "1")
    monkeypatch.setenv("VON_WORKFLOW_INTROSPECTION_AUTO_APPLY", "1")

    orchestrator = _build_orchestrator()
    mock_launch_episode_evaluation.return_value = {
        "success": True,
        "triggered": True,
        "workflow_id": "#V#episode_evaluation_workflow",
        "instance_id": "wf-episode-1",
        "status": "pending",
        "event_type": "turn_execution.completion_gate",
        "event_id": "req-introspection-1",
        "episode_evaluation_depth": 1,
    }

    request = _build_request(
        action_id="turn_execution.completion_gate",
        data={
            "prompt": "Why did this conflate task tooling and concepts?",
            "turn_id": "req-introspection-1",
            "conversation_session_id": "session-introspection-1",
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "turn_execution_record": {
                "workflow_selection": {
                    "selected_workflow_id": "#V#tool_calling_workflow",
                },
                "completion_gate": {
                    "decision": "escalation_required",
                    "decision_reason": "Conflation suspected.",
                    "safe_to_claim_completion": False,
                    "requires_follow_up": True,
                    "blocking_effect_ids": ["effect_1"],
                },
            },
            "completion_gate_loop_attempts": 1,
            "completion_gate_loop_max_attempts": 1,
            "completion_gate_loop_max_elapsed_ms": 60_000,
            "completion_gate_loop_no_progress_streak": 0,
            "completion_gate_loop_no_progress_limit": 2,
            "completion_gate_loop_last_invocation_count": 0,
            "completion_gate_loop_last_blocking_signature": "",
        },
    )

    result = orchestrator._action_turn_execution_completion_gate(request)
    assert result.ok

    autotrigger = result.outputs.get("workflow_introspection_autotrigger")
    assert isinstance(autotrigger, dict)
    assert autotrigger.get("success") is True
    assert autotrigger.get("instance_id") == "wf-episode-1"
    assert autotrigger.get("workflow_id") == "#V#episode_evaluation_workflow"
    assert result.outputs.get("episode_evaluation_autotrigger") == autotrigger

    called_args = mock_launch_episode_evaluation.call_args
    assert called_args is not None
    assert called_args.kwargs["request_id"] == "req-introspection-1"
    assert called_args.kwargs["session_id"] == "session-introspection-1"
    assert called_args.kwargs["namespace"] == "#V#test_user"
    assert called_args.kwargs["selected_workflow_id"] == "#V#tool_calling_workflow"
