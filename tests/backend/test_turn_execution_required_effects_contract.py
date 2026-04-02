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
                "required_tools": [
                    "materialise_scholarly_representation_for_file_copy"
                ],
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
                "tool": "materialise_scholarly_representation_for_file_copy",
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
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_observed_write_tool_activity_emits_generic_kb_mutation_effect_without_prompt_semantics() -> None:
    """When a write tool invocation carries payload metadata (concept_id etc.),
    the effect should be *tool-authored* rather than a coarse generic fallback."""
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
    # Tool-authored: derived from invocation metadata, not generic observation.
    assert effect.get("intent_origin") == "tool_authored"
    assert effect.get("status") == "satisfied"
    # Tool-authored effects include targets extracted from the tool payload.
    assert "#V#concept_123" in (effect.get("targets") or [])


def test_observed_failed_write_tool_activity_marks_generic_kb_mutation_unresolved() -> None:
    """Failed write tool invocations produce tool-authored effects with
    not_satisfied status and tool-specific failure codes."""
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
    assert effect.get("intent_origin") == "tool_authored"
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "kb_mutation_update_concept_failed"


def test_tool_authored_mutation_effect_includes_predicate_from_arguments() -> None:
    """When a write tool invocation carries predicate_concept_id in its
    arguments, the tool-authored effect should surface those predicates."""
    record = _build_record(
        prompt_text="Add a relationship.",
        tool_invocations=[
            {
                "tool": "add_relationship",
                "arguments": {
                    "concept_id": "#V#concept_A",
                    "predicate_concept_id": "#V#is_a_type_of",
                    "object_concept_id": "#V#concept_B",
                },
                "payload": {
                    "success": True,
                    "concept_id": "#V#concept_A",
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects
    effect = required_effects[0]
    assert effect.get("intent_origin") == "tool_authored"
    assert "#V#concept_A" in (effect.get("targets") or [])
    assert "#V#is_a_type_of" in (effect.get("required_predicates") or [])
    assert effect.get("status") == "satisfied"


def test_multiple_write_tools_produce_grouped_tool_authored_effects() -> None:
    """Multiple distinct write tools should each produce their own
    tool-authored mutation effect."""
    record = _build_record(
        prompt_text="Update and link concepts.",
        tool_invocations=[
            {
                "tool": "update_concept",
                "payload": {
                    "success": True,
                    "concept_id": "#V#concept_X",
                },
            },
            {
                "tool": "add_relationship",
                "arguments": {
                    "concept_id": "#V#concept_X",
                    "predicate_concept_id": "#V#related_to",
                    "object_concept_id": "#V#concept_Y",
                },
                "payload": {
                    "success": True,
                    "concept_id": "#V#concept_X",
                },
            },
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert len(required_effects) == 2
    tool_names = [e.get("required_tools", [None])[0] for e in required_effects]
    assert "update_concept" in tool_names
    assert "add_relationship" in tool_names
    for effect in required_effects:
        assert effect.get("intent_origin") == "tool_authored"
        assert effect.get("status") == "satisfied"


def test_summary_only_write_activity_does_not_emit_generic_mutation_effect() -> None:
    """Summary-only write activity must not recreate the removed Python fallback."""
    record = _build_record(
        aux_llm_calls=[
            {
                "type": "write_tool_policy",
                "allowed_write_tools": ["add_relationship"],
            }
        ],
    )
    required_effects = record.get("required_effects") or []
    assert all(
        isinstance(effect, dict) and effect.get("effect_type") != "kb_mutation"
        for effect in required_effects
    )
