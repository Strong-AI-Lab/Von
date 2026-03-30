from __future__ import annotations

from representation_intent_regression_helpers import (
    build_turn_record as _build_record,
)


def _paper_continuation_contract() -> dict[str, object]:
    return {
        "schema_version": "required_effects_contract.v1",
        "intent_class": "representation",
        "domain_profile_id": "paper",
        "artefact_context": {
            "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
        },
        "required_effects": [
            {
                "effect_id": "effect_paper_representation_1",
                "effect_type": "scholarly_representation",
                "targets": ["#V#uploaded_file_copy_abc123"],
                "required_tools": ["interpret_file_copy"],
                "status": "not_executed",
                "status_reason": "No required representation tool execution was observed.",
                "failure_code": "paper_representation_not_executed",
                "failure_codes": ["paper_representation_not_executed"],
            }
        ],
    }


def test_prompt_only_representation_request_does_not_emit_required_effects_contract() -> None:
    record = _build_record(
        prompt_text="Fully represent the corresponding paper from #V#uploaded_file_copy_abc123."
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None

    required_effects = record.get("required_effects") or []
    assert all(
        str(effect.get("effect_type") or "") not in {"scholarly_representation"}
        and not str(effect.get("effect_type") or "").startswith("representation_")
        for effect in required_effects
        if isinstance(effect, dict)
    )


def test_prompt_tool_requirement_telemetry_alone_does_not_emit_required_effects_contract() -> None:
    record = _build_record(
        aux_llm_calls=[
            {
                "type": "prompt_tool_requirements",
                "required_scholarly_representation_for_file_copy_ids": [
                    "#V#uploaded_file_copy_abc123"
                ],
            }
        ]
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None


def test_read_only_representation_tool_activity_does_not_create_representation_contract_without_continuation() -> None:
    record = _build_record(
        prompt_text="Represent this person profile from this CV file #V#uploaded_file_copy_person_1.",
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": True,
                    "concept_id": "#V#uploaded_file_copy_person_1",
                    "person_representation": {
                        "attempted": True,
                        "verified": True,
                        "person_concept_id": "#V#person_jane_doe_1234abcd",
                    },
                },
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None

    required_effects = record.get("required_effects") or []
    assert all(
        str(effect.get("effect_type") or "") not in {"scholarly_representation"}
        and not str(effect.get("effect_type") or "").startswith("representation_")
        for effect in required_effects
        if isinstance(effect, dict)
    )


def test_continuation_context_reuses_representation_contract() -> None:
    prior_contract = _paper_continuation_contract()
    record = _build_record(
        prompt_text="Please proceed.",
        response_text="Still working on it.",
        aux_llm_calls=[
            {
                "type": "workflow_continuation_context",
                "applied": True,
                "context": {
                    "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
                    "requires_follow_up": True,
                    "has_unresolved_required_effects": True,
                    "required_effects_contract": prior_contract,
                },
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert contract == prior_contract

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "scholarly_representation"
    assert effect.get("status") == "not_executed"
    assert effect.get("targets") == ["#V#uploaded_file_copy_abc123"]

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("requires_follow_up") is True
    assert completion_gate.get("safe_to_claim_completion") is False


def test_continuation_context_representation_contract_can_be_satisfied_by_matching_tool_execution() -> None:
    prior_contract = _paper_continuation_contract()
    record = _build_record(
        prompt_text="Please proceed.",
        response_text="Representation completed.",
        aux_llm_calls=[
            {
                "type": "workflow_continuation_context",
                "applied": True,
                "context": {
                    "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
                    "requires_follow_up": True,
                    "has_unresolved_required_effects": True,
                    "required_effects_contract": prior_contract,
                },
            }
        ],
        tool_invocations=[
            {
                "tool": "interpret_file_copy",
                "payload": {
                    "success": True,
                    "concept_id": "#V#uploaded_file_copy_abc123",
                    "scholarly_representation": {
                        "attempted": True,
                        "verified": True,
                        "paper_concept_id": "#V#paper_on_arxiv_abc123",
                    },
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "scholarly_representation"
    assert effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "partial"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_observed_write_tool_activity_emits_generic_kb_mutation_effect_without_prompt_semantics() -> None:
    record = _build_record(
        prompt_text="Tell me about this concept.",
        tool_invocations=[
            {
                "tool": "update_concept",
                "payload": {
                    "success": True,
                    "concept_id": "#V#concept_123",
                },
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "kb_mutation"
    assert effect.get("intent_origin") == "observed_tool_activity"
    assert effect.get("status") == "satisfied"


def test_observed_failed_write_tool_activity_marks_generic_kb_mutation_unresolved() -> None:
    record = _build_record(
        prompt_text="Tell me about this concept.",
        tool_invocations=[
            {
                "tool": "update_concept",
                "payload": {
                    "success": False,
                    "error": "write_failed",
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("effect_type") == "kb_mutation"
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "kb_mutation_write_failed_or_blocked"
