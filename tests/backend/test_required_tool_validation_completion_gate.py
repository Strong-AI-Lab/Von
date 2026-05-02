from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src.backend.services.turn_execution_record_service import (
    build_turn_execution_record,
)
from src.backend.services.required_tool_obligation_service import (
    BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED,
)
from src.backend.workflows.durable.turn_execution_runtime_support import (
    run_turn_execution_completion_gate,
)

_CREATE_CONCEPTS_PARENT_ERROR = (
    "create_concepts: Missing required field 'parent_id' for create_concepts input: "
    "parent_id (str, parent concept_id), concepts (list of {name, kind?, "
    "description?, notes?})."
)

_KR_REQUIRED_TOOLS = [
    "create_concepts",
    "add_relationship",
    "upsert_singleton_text_relation",
    "fetch_concept",
    "get_text_relations_summary",
]


def _validation_failure_aux() -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_contract_attempt",
            "schema_version": "tool_contract_attempt.v1",
            "stage": "tool_calling.validate",
            "tool_calls": [
                {
                    "tool": "create_concepts",
                    "payload": {
                        "concepts": [
                            {"name": "Assistant label", "kind": "type"},
                        ],
                        "namespace": "#V#user",
                    },
                }
            ],
            "validation_error_count": 1,
            "validation_errors": [_CREATE_CONCEPTS_PARENT_ERROR],
            "validation_warnings": [],
            "repair_attempted": True,
            "repair_succeeded": None,
            "diagnostics": [
                {
                    "schema_version": "tool_call_contract_validation.v1",
                    "status": "invalid",
                    "tool": "create_concepts",
                    "error_code": "schema_validation_failed",
                    "message": _CREATE_CONCEPTS_PARENT_ERROR,
                    "payload": {
                        "concepts": [
                            {"name": "Assistant label", "kind": "type"},
                        ],
                        "namespace": "#V#user",
                    },
                }
            ],
        },
        {
            "type": "tool_call_validation_repair_result",
            "stage": "tool_calling.repair",
            "attempt": 1,
            "budget": 1,
            "succeeded": False,
            "repaired_call_count": 0,
            "decision": {
                "schema_version": "tool_call_repair_decision.v1",
                "required": False,
                "attempted": True,
                "succeeded": False,
                "attempts": 1,
                "budget": 1,
                "remaining": 0,
                "error_count": 1,
                "reason": "repair_failed_or_returned_empty",
            },
        },
    ]


def _build_record(*, required_prompt_tools: list[str]) -> dict[str, Any]:
    return build_turn_execution_record(
        request_id="req-required-tool-validation",
        session_id="session-required-tool-validation",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text="Create the represented ontology artefacts.",
        response_text="Tool validation failed.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=[],
        aux_llm_calls=_validation_failure_aux(),
        required_prompt_tools=required_prompt_tools,
    )


def _build_kr_label_record(
    *,
    required_prompt_tools: list[str],
    tool_invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    return build_turn_execution_record(
        request_id="req-represented-label-contract",
        session_id="session-represented-label-contract",
        namespace="#V#user@org",
        actor_concept_id="#V#user",
        user_id="#V#user",
        org_id="#V#org",
        prompt_text=(
            "Create represented workflow labels so Von can avoid repeating "
            "completed KB work, then use the API to create any useful ones."
        ),
        response_text="The requested tool evidence is present.",
        interaction_timestamp_utc="2026-05-02T00:00:00Z",
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        tool_invocations=tool_invocations,
        aux_llm_calls=[],
        required_prompt_tools=required_prompt_tools,
    )


def _kr_write_invocations() -> list[dict[str, Any]]:
    return [
        {
            "tool": "create_concepts",
            "status": "ok",
            "effective_arguments": {
                "parent_id": "#V#workflow_marker",
                "concepts": [{"name": "Already scanned marker"}],
            },
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
        {
            "tool": "add_relationship",
            "status": "ok",
            "effective_arguments": {
                "source_id": "#V#already_scanned_marker",
                "predicate": "#V#hasWorkflow",
                "target": "#V#paper_ingestion_workflow",
            },
            "effective_payload": {"success": True},
        },
        {
            "tool": "upsert_singleton_text_relation",
            "status": "ok",
            "effective_arguments": {
                "concept_id": "#V#already_scanned_marker",
                "predicate_concept_id": "#V#hasDescription",
                "text": "Marker used to avoid repeating completed workflow work.",
            },
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
    ]


def _kr_readback_invocations() -> list[dict[str, Any]]:
    return [
        {
            "tool": "fetch_concept",
            "status": "ok",
            "effective_arguments": {"concept_id": "#V#already_scanned_marker"},
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
        {
            "tool": "get_text_relations_summary",
            "status": "ok",
            "effective_arguments": {"concept_id": "#V#already_scanned_marker"},
            "effective_payload": {
                "success": True,
                "concept_id": "#V#already_scanned_marker",
            },
        },
    ]


def test_turn_record_marks_required_tool_schema_failure_as_unresolved_effect() -> None:
    record = _build_record(
        required_prompt_tools=["create_concepts", "add_relationship"],
    )

    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    create_effect = effect_by_id["effect_prompt_required_mutation_create_concepts_1"]
    assert create_effect["status"] == "not_satisfied"
    assert create_effect["status_reason"] == _CREATE_CONCEPTS_PARENT_ERROR
    assert create_effect["failure_code"] == (
        "prompt_required_mutation_create_concepts_failed"
    )
    assert "schema_validation_failed" in create_effect["failure_codes"]
    assert "tool_call_validation_failed" in create_effect["failure_codes"]
    assert create_effect["tool_call_repair_outcome"] == "repair_failed"
    assert create_effect["tool_call_repair_stop_reason"] == (
        "repair_failed_or_returned_empty"
    )

    relationship_effect = effect_by_id[
        "effect_prompt_required_mutation_add_relationship_2"
    ]
    assert relationship_effect["status"] == "not_executed"

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert gate["requires_follow_up"] is True
    assert gate["evidence_payload"]["required_effect_count"] == 3
    assert "schema_validation_failed" in gate["blocking_failure_codes"]
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in gate["blocking_failure_codes"]

    summary = record["execution"]["summary"]
    ledger = summary["required_tool_obligations"]
    create_obligation = next(
        obligation
        for obligation in ledger["obligations"]
        if obligation["tool_name"] == "create_concepts"
    )
    assert create_obligation["blocking_reason"] == (
        BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED
    )
    assert create_obligation["last_attempt_status"] == "schema_validation_failed"
    assert create_obligation["last_attempt_message"] == _CREATE_CONCEPTS_PARENT_ERROR
    assert create_obligation["tool_call_validation_errors"][0]["message"] == (
        _CREATE_CONCEPTS_PARENT_ERROR
    )
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        summary["required_tool_obligation_blocking_failure_codes"]
    )

    dispatch = record["workflow_routing_diagnostics"]["dispatch"]
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        dispatch["required_tool_obligation_blocking_failure_codes"]
    )


def test_completion_gate_rebuilds_stale_zero_effect_record_with_required_tools() -> (
    None
):
    data: dict[str, Any] = {
        "turn_execution_record": {
            "required_effects": [],
            "completion_gate": {
                "decision": "completed",
                "decision_reason": "No blocking effect detected.",
                "safe_to_claim_completion": True,
                "requires_follow_up": False,
                "evidence_payload": {"required_effect_count": 0},
            },
        },
        "required_prompt_tools": ["create_concepts"],
        "prompt": "Create the represented ontology artefacts.",
        "final_response": "Done.",
        "current_response": "Done.",
        "invocations": [],
        "aux_llm_calls": _validation_failure_aux(),
        "workflow_routing": {
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        "turn_id": "req-stale-gate",
        "conversation_session_id": "session-stale-gate",
        "user_concept_id": "#V#user",
        "org_concept_id": "#V#org",
        "turn_execution_record_generated_at": "2026-05-02T00:00:00Z",
    }
    request = SimpleNamespace(
        data=data,
        inputs={},
        environment=SimpleNamespace(user_namespace="#V#user@org"),
    )

    result = run_turn_execution_completion_gate(
        request,
        annotation_component="test",
        annotation_function="test_completion_gate",
        introspection_auto_apply_env="VON_TEST_UNUSED",
    )

    assert result.outputs["completion_gate_safe_to_claim_completion"] is False
    assert result.outputs["completion_gate_requires_follow_up"] is True
    assert (
        result.outputs["completion_gate_evidence_payload"]["required_effect_count"] == 2
    )
    assert (
        "schema_validation_failed"
        in result.outputs["completion_gate_blocking_failure_codes"]
    )
    assert BLOCKER_REQUIRED_WRITE_PAYLOAD_UNRESOLVED in (
        result.outputs["completion_gate_blocking_failure_codes"]
    )
    assert any(
        entry.get("type") == "completion_gate_record_rebuilt"
        for entry in data["aux_llm_calls"]
    )


def test_kr_required_tools_block_completion_when_write_and_readback_are_absent() -> (
    None
):
    record = _build_kr_label_record(
        required_prompt_tools=_KR_REQUIRED_TOOLS,
        tool_invocations=[],
    )

    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }

    assert (
        effect_by_id["effect_prompt_required_mutation_create_concepts_1"]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id["effect_prompt_required_mutation_add_relationship_2"]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id[
            "effect_prompt_required_mutation_upsert_singleton_text_relation_3"
        ]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id["effect_prompt_required_evidence_fetch_concept_1"]["status"]
        == "not_executed"
    )
    assert (
        effect_by_id["effect_prompt_required_evidence_get_text_relations_summary_2"][
            "status"
        ]
        == "not_executed"
    )
    assert record["completion_gate"]["safe_to_claim_completion"] is False
    assert "prompt_required_mutation_create_concepts_missing" in (
        record["completion_gate"]["blocking_failure_codes"]
    )
    assert "prompt_required_mutation_add_relationship_missing" in (
        record["completion_gate"]["blocking_failure_codes"]
    )
    assert "prompt_required_mutation_upsert_singleton_text_relation_missing" in (
        record["completion_gate"]["blocking_failure_codes"]
    )


def test_kr_required_tools_block_completion_when_write_lacks_readback() -> None:
    record = _build_kr_label_record(
        required_prompt_tools=_KR_REQUIRED_TOOLS,
        tool_invocations=_kr_write_invocations(),
    )

    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    assert (
        effect_by_id["effect_prompt_required_mutation_create_concepts_1"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id["effect_prompt_required_mutation_add_relationship_2"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id[
            "effect_prompt_required_mutation_upsert_singleton_text_relation_3"
        ]["status"]
        == "satisfied"
    )

    gate = record["completion_gate"]
    assert gate["safe_to_claim_completion"] is False
    assert "prompt_required_evidence_fetch_concept_missing" in (
        gate["blocking_failure_codes"]
    )
    assert "prompt_required_evidence_get_text_relations_summary_missing" in (
        gate["blocking_failure_codes"]
    )
    assert any(
        check.get("effect_id") == "mutation_3"
        and check.get("verification_mode") == "state_requery_missing"
        for check in record["postcondition_checks"]
    )


def test_kr_required_tools_allow_completion_after_write_and_readback() -> None:
    record = _build_kr_label_record(
        required_prompt_tools=_KR_REQUIRED_TOOLS,
        tool_invocations=[
            *_kr_write_invocations(),
            *_kr_readback_invocations(),
        ],
    )

    assert record["completion_gate"]["safe_to_claim_completion"] is True
    assert record["completion_gate"]["requires_follow_up"] is False
    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    assert (
        effect_by_id["effect_prompt_required_mutation_create_concepts_1"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id["effect_prompt_required_mutation_add_relationship_2"]["status"]
        == "satisfied"
    )
    assert (
        effect_by_id[
            "effect_prompt_required_mutation_upsert_singleton_text_relation_3"
        ]["status"]
        == "satisfied"
    )
    assert any(
        check.get("effect_id") == "mutation_3"
        and check.get("verification_mode") == "state_requery_observed"
        and check.get("status") == "verified"
        for check in record["postcondition_checks"]
    )
