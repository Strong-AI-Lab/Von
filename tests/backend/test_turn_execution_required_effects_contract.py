from __future__ import annotations

import pytest

from representation_intent_regression_helpers import (
    assert_low_risk_default_policy,
    build_turn_record as _build_record,
    patch_representation_profile_loader,
)
from src.backend.workflows.engine import WorkflowDefinition
from src.backend.workflows.required_effects_contracts import (
    normalise_workflow_required_effects_contract,
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


def _workflow_required_effects_contract() -> dict[str, object]:
    return {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "conversation_diagnostics",
        "required_effects": [
            {
                "effect_id": "conversation_locator",
                "effect_type": "diagnostic_evidence",
                "required_tools": ["conversation_telemetry_get_locator"],
                "activation_required_tools": [
                    "conversation_telemetry_get_locator",
                    "chat_history_get_segments",
                    "chat_history_get_debug_entry",
                ],
                "activation_required_tools_match": "any",
                "missing_failure_code": "conversation_locator_missing",
                "failed_failure_code": "conversation_locator_failed",
            },
            {
                "effect_id": "conversation_history",
                "effect_type": "diagnostic_evidence",
                "required_tools": [
                    "chat_history_get_segments",
                    "chat_history_get_debug_entry",
                ],
                "required_tools_match": "any",
                "activation_required_tools": [
                    "conversation_telemetry_get_locator",
                    "chat_history_get_segments",
                    "chat_history_get_debug_entry",
                ],
                "activation_required_tools_match": "any",
                "missing_failure_code": "conversation_history_missing",
                "failed_failure_code": "conversation_history_failed",
            },
        ],
    }


def _entity_information_workflow_required_effects_contract() -> dict[str, object]:
    return {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "grounded_entity_information_retrieval_evidence",
        "required_effects": [
            {
                "effect_id": "grounded_entity_information_evidence",
                "effect_type": "grounded_evidence",
                "required_tools": [
                    "fetch_concept",
                    "get_text_relations_summary",
                    "get_predicate_incidence",
                    "find_relations_with_argument",
                    "list_uncertain_relationship_assertions",
                ],
                "required_tools_match": "all",
                "recovery_strategies": [
                    {
                        "strategy_id": "recover_text_relations_for_focal_entity",
                        "tool": "get_text_relations_summary",
                        "recovers_tools": ["get_text_relations_summary"],
                        "target_concept_source": "required_fetch_or_focal_concept",
                        "target_concept_argument_name": "concept_id",
                        "target_concept_max_count": 2,
                    }
                ],
                "missing_failure_code": "entity_information_evidence_missing",
                "failed_failure_code": "entity_information_evidence_failed",
                "not_executed_reason": (
                    "Required grounded entity-information evidence was not retrieved."
                ),
            }
        ],
    }


def _patch_workflow_required_effects_contract(monkeypatch) -> None:
    contract = _workflow_required_effects_contract()
    definition = WorkflowDefinition(
        workflow_id="#V#tool_calling_workflow",
        initial_state="done",
        states={},
        metadata={
            "required_effects_contract": contract,
            "required_effects_contract_source": (
                "text_relation:#V#hasWorkflowRequiredEffectsContractJson"
            ),
        },
    )

    class _Registration:
        def __init__(self, definition: WorkflowDefinition):
            self.definition = definition

    class _Registry:
        def get_registration(self, workflow_id: str):
            if workflow_id == "#V#tool_calling_workflow":
                return _Registration(definition)
            return None

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        lambda defer_parity_work=True: _Registry(),
    )


def test_workflow_required_effects_contract_normalises_recovery_strategies() -> None:
    contract = normalise_workflow_required_effects_contract(
        {
            "contract_id": "grounded_entity_information_retrieval_evidence",
            "required_effects": [
                {
                    "effect_id": "grounded_entity_information_evidence",
                    "effect_type": "grounded_evidence",
                    "required_tools": ["get_text_relations_summary"],
                    "recovery_strategies": [
                        {
                            "id": "recover_text_relations_for_focal_entity",
                            "recover_tool": "get_text_relations_summary",
                            "missing_tools": ["get_text_relations_summary"],
                            "bind_target_concept_from": (
                                "required_fetch_or_focal_concept"
                            ),
                            "target_argument": "concept_id",
                            "target_concept_max_count": "2",
                            "default_payload": {
                                "max_relation_ids_per_group": 25,
                            },
                        }
                    ],
                }
            ],
        }
    )

    assert contract is not None
    effect = contract["required_effects"][0]
    assert effect["recovery_strategies"] == [
        {
            "strategy_id": "recover_text_relations_for_focal_entity",
            "tool": "get_text_relations_summary",
            "recovers_tools": ["get_text_relations_summary"],
            "target_concept_source": "required_fetch_or_focal_concept",
            "target_concept_argument_name": "concept_id",
            "target_concept_max_count": 2,
            "default_payload": {
                "max_relation_ids_per_group": 25,
            },
        }
    ]


@pytest.fixture(autouse=True)
def _stub_shared_workflow_registry(monkeypatch):
    class _EmptyRegistry:
        def get(self, workflow_id: str):
            return None

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        lambda defer_parity_work=True: _EmptyRegistry(),
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_conversation_turn_stage_model_snapshot",
        lambda: {
            "schema_version": "conversation_turn_stage_model.v1",
            "workflow_representation_id": "#V#conversation_turn_execution_workflow",
            "stages": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.build_conversation_turn_stage_path",
        lambda runtime_stages, workflow_id=None, selected_workflow_id=None: {
            "schema_version": "conversation_turn_stage_path.v1",
            "workflow_id": workflow_id,
            "workflow_id_source": "test_stub",
            "path": [],
            "unmapped_runtime_stages": [],
            "observed_workflow_ids": [],
        },
    )


@pytest.fixture(autouse=True)
def _patch_representation_profiles(monkeypatch):
    patch_representation_profile_loader(monkeypatch)


def test_prompt_text_only_representation_request_does_not_emit_required_effects_contract() -> (
    None
):
    record = _build_record(
        prompt_text="Fully represent the corresponding paper from #V#uploaded_file_copy_abc123."
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None
    required_effects = record.get("required_effects") or []
    assert all(
        not isinstance(effect, dict)
        or effect.get("effect_type") != "scholarly_representation"
        for effect in required_effects
    )


def test_structured_scholarly_prompt_requirement_emits_required_effects_contract() -> (
    None
):
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
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("domain_profile_id") == "paper"
    assert contract.get("artefact_context", {}).get("file_copy_ids") == [
        "#V#uploaded_file_copy_abc123"
    ]
    assert_low_risk_default_policy(contract)
    required_effects = record.get("required_effects") or []
    assert len(required_effects) == 1
    assert required_effects[0].get("targets") == ["#V#uploaded_file_copy_abc123"]


def test_prompt_text_only_person_representation_request_does_not_emit_required_effects_contract() -> (
    None
):
    record = _build_record(
        prompt_text="Represent this person profile from this CV file #V#uploaded_file_copy_person_1.",
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None
    required_effects = record.get("required_effects") or []
    assert all(
        not isinstance(effect, dict)
        or effect.get("effect_type") != "representation_person"
        for effect in required_effects
    )


def test_prompt_arxiv_list_does_not_emit_representation_contract_without_authority_surface() -> (
    None
):
    record = _build_record(
        prompt_text=(
            "eprint version: https://arxiv.org/abs/2310.03714\n"
            "arXiv preprint version: https://arxiv.org/abs/2308.03688"
        ),
        response_text="Still working on it.",
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None
    required_effects = record.get("required_effects") or []
    assert all(
        not isinstance(effect, dict)
        or effect.get("effect_type") != "scholarly_representation"
        for effect in required_effects
    )


def test_structured_generic_representation_target_fails_closed_without_profile_binding() -> (
    None
):
    record = _build_record(
        aux_llm_calls=[
            {
                "type": "prompt_tool_requirements",
                "required_representation_for_file_copy_ids": [
                    "#V#uploaded_file_copy_person_1"
                ],
            }
        ]
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("domain_profile_id") == "representation"
    assert contract.get("default_decision_policy") is None
    assert contract.get("artefact_context", {}).get("file_copy_ids") == [
        "#V#uploaded_file_copy_person_1"
    ]
    profile_resolution = contract.get("profile_resolution") or {}
    assert profile_resolution.get("fail_closed") is True
    assert (
        profile_resolution.get("fail_closed_reason")
        == "representation_profile_unmatched"
    )

    required_effects = record.get("required_effects") or []
    assert len(required_effects) == 1
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_contract_guard"
    assert effect.get("failure_code") == "representation_profile_unmatched"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False


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


def test_continuation_context_representation_contract_can_be_satisfied_by_matching_tool_execution() -> (
    None
):
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
                        "file_copy_concept_id": "#V#uploaded_file_copy_abc123",
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


def test_continuation_context_representation_contract_requires_readback_artefact_ids() -> (
    None
):
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
    effect = required_effects[0]
    assert effect.get("effect_type") == "scholarly_representation"
    assert effect.get("status") == "not_satisfied"
    assert "paper_representation_readback_missing" in (
        effect.get("failure_codes") or []
    )
    assert "file_copy_concept_id" in (effect.get("status_reason") or "")

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False


def test_workflow_required_representation_contract_expands_arxiv_targets() -> None:
    workflow_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "arxiv_paper_representation_readback",
        "required_effects": [
            {
                "effect_id": "arxiv_paper_representation",
                "effect_type": "scholarly_representation",
                "required_tools": ["scholarly_paper.verify_representation"],
                "targets_source_expressions": [
                    "selected_workflow_trace.expected_outcome_contract_state.fields.summary"
                ],
                "targets_extractor": "arxiv_id_list",
                "missing_failure_code": "arxiv_paper_representation_not_executed",
                "wrong_target_failure_code": "arxiv_paper_representation_wrong_target",
                "required_payload_fields": [
                    "paper_concept_id",
                    "file_copy_concept_id",
                ],
            }
        ],
    }
    record = _build_record(
        prompt_text=(
            "Represent https://arxiv.org/abs/2406.15341 and "
            "https://arxiv.org/abs/2507.21035"
        ),
        response_text="Only the first paper was represented.",
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        selected_workflow_trace={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "workflow_required_effects_contract": workflow_contract,
            "workflow_required_effects_contract_source": "definition_metadata",
            "expected_outcome_contract_state": {
                "fields": {
                    "summary": (
                        "Represent arXiv:2406.15341 and arXiv:2507.21035."
                    )
                }
            },
        },
        tool_invocations=[
            {
                "tool": "scholarly_paper.verify_representation",
                "effective_payload": {
                    "success": True,
                    "arxiv_id": "2406.15341",
                    "scholarly_representation_verified": True,
                    "paper_concept_id": "#V#paper_2406_15341",
                    "file_copy_concept_id": "#V#file_copy_2406_15341",
                },
            }
        ],
    )

    required_effects = [
        effect
        for effect in (record.get("required_effects") or [])
        if isinstance(effect, dict)
        and effect.get("intent_origin") == "workflow_authored"
    ]
    assert [effect.get("targets") for effect in required_effects] == [
        ["2406.15341"],
        ["2507.21035"],
    ]
    assert [effect.get("status") for effect in required_effects] == [
        "satisfied",
        "not_executed",
    ]
    assert required_effects[1].get("failure_code") == (
        "arxiv_paper_representation_wrong_target"
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "arxiv_paper_representation_wrong_target" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_workflow_required_contract_uses_dispatch_workflow_id_when_routing_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "arxiv_paper_representation_readback",
        "required_effects": [
            {
                "effect_id": "arxiv_paper_representation",
                "effect_type": "scholarly_representation",
                "required_tools": ["scholarly_paper.verify_representation"],
                "missing_failure_code": "arxiv_paper_representation_not_executed",
                "required_payload_fields": ["paper_concept_id"],
            }
        ],
    }
    loaded_workflow_ids: list[str | None] = []

    def _fake_load_workflow_required_effects_contract(
        *,
        workflow_id: str | None,
    ) -> tuple[dict[str, object] | None, str | None]:
        loaded_workflow_ids.append(workflow_id)
        if workflow_id == "#V#arxiv_paper_representation_workflow":
            return dict(workflow_contract), "definition_metadata"
        return None, None

    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service._load_workflow_required_effects_contract",
        _fake_load_workflow_required_effects_contract,
    )

    record = _build_record(
        prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
        response_text="The paper has been successfully represented.",
        workflow_routing={
            "verdict": "rag_selected",
            "source": "selector",
        },
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "execution_mode_selected",
                "status": "selected",
                "selected_execution_mode": "custom_workflow",
                "dispatch_workflow_id": "#V#arxiv_paper_representation_workflow",
            },
            {
                "type": "workflow_dispatch_boundary",
                "boundary": "workflow_terminal",
                "status": "completed",
                "selected_execution_mode": "custom_workflow",
                "dispatch_workflow_id": "#V#arxiv_paper_representation_workflow",
                "completed": True,
                "final_state": "completed",
            },
        ],
        tool_invocations=[
            {
                "tool": "scholarly_paper.verify_representation",
                "effective_payload": {
                    "success": True,
                    "arxiv_id": "2106.03245",
                    "scholarly_representation_verified": True,
                    "paper_concept_id": (
                        "#V#verification_in_the_loop_correct_by_construction_"
                        "control_learning_with_reach_avoid_guarantees"
                    ),
                    "file_copy_concept_id": "#V#file_copy_2106_03245",
                },
            }
        ],
    )

    assert loaded_workflow_ids == ["#V#arxiv_paper_representation_workflow"]
    workflow_effects = [
        effect
        for effect in (record.get("required_effects") or [])
        if isinstance(effect, dict)
        and effect.get("intent_origin") == "workflow_authored"
    ]
    assert len(workflow_effects) == 1
    effect = workflow_effects[0]
    assert effect.get("effect_id") == "arxiv_paper_representation"
    assert effect.get("status") == "satisfied"

    execution_summary = (record.get("execution") or {}).get("summary") or {}
    assert execution_summary.get("selected_workflow_id") == (
        "#V#arxiv_paper_representation_workflow"
    )
    assert execution_summary.get("selected_workflow_id_source") == (
        "dispatch_workflow_id"
    )
    assert execution_summary.get("workflow_required_effects_contract_id") == (
        "arxiv_paper_representation_readback"
    )
    assert execution_summary.get("workflow_required_effects_materialised_count") == 1
    assert execution_summary.get("required_effects_unresolved_effect_ids") == []

    dispatch = ((record.get("workflow_routing_diagnostics") or {}).get("dispatch")) or {}
    assert dispatch.get("workflow_required_effects_contract_id") == (
        "arxiv_paper_representation_readback"
    )
    assert dispatch.get("workflow_required_effects_materialised_count") == 1
    assert dispatch.get("required_effects_unresolved_effect_ids") == []

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_observed_write_tool_activity_emits_generic_kb_mutation_effect_without_prompt_semantics() -> (
    None
):
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


def test_observed_failed_write_tool_activity_marks_generic_kb_mutation_unresolved() -> (
    None
):
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


def test_workflow_authored_required_evidence_contract_blocks_missing_locator(
    monkeypatch,
) -> None:
    _patch_workflow_required_effects_contract(monkeypatch)

    record = _build_record(
        prompt_text="Explain the failure in more detail from the telemetry.",
        tool_invocations=[
            {
                "tool": "chat_history_get_segments",
                "payload": {"success": True},
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    workflow_contract = execution.get("workflow_required_effects_contract")
    assert isinstance(workflow_contract, dict)
    assert workflow_contract.get("contract_id") == "conversation_diagnostics"

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    locator_effect = next(
        effect
        for effect in required_effects
        if effect.get("effect_id") == "conversation_locator"
    )
    history_effect = next(
        effect
        for effect in required_effects
        if effect.get("effect_id") == "conversation_history"
    )
    assert locator_effect.get("intent_origin") == "workflow_authored"
    assert locator_effect.get("status") == "not_executed"
    assert locator_effect.get("failure_code") == "conversation_locator_missing"
    assert history_effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert "conversation_locator_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_runtime_selected_workflow_trace_required_evidence_contract_blocks_missing_tools() -> (
    None
):
    contract = _entity_information_workflow_required_effects_contract()

    record = _build_record(
        prompt_text="What collaborators of mine are explicitly represented here?",
        response_text="No explicit collaborators are found in the current representation.",
        workflow_routing={
            "workflow_id": "#V#entity_information_retrieval_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        selected_workflow_trace={
            "selected_workflow_id": "#V#entity_information_retrieval_workflow",
            "selected_execution_mode": "custom_workflow",
            "workflow_required_effects_contract": contract,
            "workflow_required_effects_contract_source": "definition_metadata",
        },
        tool_invocations=[],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("workflow_required_effects_contract") == contract
    assert (
        execution.get("summary", {}).get("workflow_required_effects_contract_source")
        == "definition_metadata"
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    effect = next(
        item
        for item in required_effects
        if item.get("effect_id") == "grounded_entity_information_evidence"
    )
    assert effect.get("intent_origin") == "workflow_authored"
    assert effect.get("status") == "not_executed"
    assert effect.get("failure_code") == "entity_information_evidence_missing"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "entity_information_evidence_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_custom_workflow_actions_do_not_make_missing_required_evidence_tools_expected() -> (
    None
):
    contract = _entity_information_workflow_required_effects_contract()

    record = _build_record(
        prompt_text="Who am I?",
        response_text="No explicit profile information was found.",
        workflow_routing={
            "workflow_id": "#V#entity_information_retrieval_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
        selected_workflow_trace={
            "selected_workflow_id": "#V#entity_information_retrieval_workflow",
            "selected_execution_mode": "custom_workflow",
            "workflow_required_effects_contract": contract,
            "workflow_required_effects_contract_source": "definition_metadata",
        },
        aux_llm_calls=[
            {
                "type": "workflow_execution",
                "workflow_id": "#V#entity_information_retrieval_workflow",
                "completed": True,
                "execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": "#V#entity_information_retrieval_workflow",
                    "completed": True,
                    "terminal_status": "completed",
                    "final_state": "completed",
                    "step_result_envelope_count": 2,
                    "action_started_count": 2,
                    "action_completed_count": 2,
                    "action_success_count": 2,
                    "action_failure_count": 0,
                    "action_unknown_count": 0,
                    "terminal_effect_count": 1,
                    "terminal_effects": [],
                    "durable_side_effect_count": 0,
                    "durable_side_effects": [],
                },
            }
        ],
        tool_invocations=[],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    summary = execution.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("zero_tools_executed") is True
    assert summary.get("zero_tool_execution_expected") is False
    assert summary.get("zero_tool_reason_code") == "required_effect_tools_missing"
    assert summary.get("required_effects_missing_required_tools") == [
        "fetch_concept",
        "get_text_relations_summary",
        "get_predicate_incidence",
        "find_relations_with_argument",
        "list_uncertain_relationship_assertions",
    ]

    routing_diagnostics = record.get("workflow_routing_diagnostics")
    assert isinstance(routing_diagnostics, dict)
    dispatch = routing_diagnostics.get("dispatch")
    assert isinstance(dispatch, dict)
    assert dispatch.get("zero_tool_execution_expected") is False
    assert dispatch.get("zero_tool_reason_code") == "required_effect_tools_missing"
    assert dispatch.get("required_effects_missing_required_tool_count") == 5

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "entity_information_evidence_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_prompt_required_evidence_contract_blocks_missing_surface() -> None:
    record = _build_record(
        prompt_text=(
            "Prepare a short research briefing for me: my represented papers, "
            "relevant recent arXiv work, and any linked Jira tasks."
        ),
        response_text=(
            "### User Papers\nNo user papers found.\n\n"
            "### Recent arXiv\nNo recent arXiv papers found.\n\n"
            "### Jira Tasks\nNo Jira tasks found."
        ),
        required_prompt_tools=[
            "search_knowledge_base",
            "search_concepts",
            "search_arxiv",
            "jira_search",
        ],
        tool_invocations=[
            {"tool": "search_knowledge_base", "payload": {"success": True}},
            {"tool": "search_concepts", "payload": {"success": True}},
            {"tool": "search_arxiv", "payload": {"success": True}},
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("intent_class") == "evidence"
    assert contract.get("domain_profile_id") == "prompt_required_evidence"
    assert contract.get("default_decision_policy") is None

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    jira_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["jira_search"]
    )
    assert jira_effect.get("status") == "not_executed"
    assert (
        jira_effect.get("failure_code")
        == "prompt_required_evidence_jira_search_missing"
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "prompt_required_evidence_jira_search_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_turn_contract_required_tools_block_when_preflight_state_omitted() -> None:
    record = _build_record(
        prompt_text="What are key predicates for scientific papers in Vontology?",
        response_text="Loaded: michael_witbrock",
        turn_expected_outcome_contract={
            "summary": "Identify key predicates for scientific papers.",
            "required_tools": [
                "resolve_concept_by_name",
                "get_predicate_incidence",
            ],
        },
        completion_report={
            "response_text": "Loaded: michael_witbrock",
            "required_prompt_tools": [
                "resolve_concept_by_name",
                "get_predicate_incidence",
            ],
            "missing_prompt_tools": ["resolve_concept_by_name"],
        },
        selected_workflow_trace={
            "selected_workflow_id": "#V#tool_calling_workflow",
            "required_prompt_tools": [
                "resolve_concept_by_name",
                "get_predicate_incidence",
            ],
            "missing_prompt_tools": ["resolve_concept_by_name"],
        },
        tool_invocations=[
            {"tool": "get_predicate_incidence", "payload": {"success": True}}
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_prompt_tools") == [
        "resolve_concept_by_name",
        "get_predicate_incidence",
    ]
    assert execution.get("missing_prompt_tools") == [
        "resolve_concept_by_name",
        "get_predicate_incidence",
    ]

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    resolve_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["resolve_concept_by_name"]
    )
    assert resolve_effect.get("status") == "not_executed"
    assert (
        resolve_effect.get("failure_code")
        == "prompt_required_evidence_resolve_concept_by_name_missing"
    )
    incidence_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["get_predicate_incidence"]
    )
    assert incidence_effect.get("status") == "not_executed"
    assert (
        incidence_effect.get("failure_code")
        == "prompt_required_evidence_get_predicate_incidence_missing"
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "prompt_required_evidence_resolve_concept_by_name_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_turn_contract_search_bound_predicate_incidence_requires_searched_target() -> (
    None
):
    record = _build_record(
        prompt_text="What are key predicates for scientific papers in Vontology?",
        response_text="Loaded: michael_witbrock",
        turn_expected_outcome_contract={
            "summary": "Identify key predicates for scientific papers.",
            "required_tools": [
                "search_concepts",
                "get_predicate_incidence",
            ],
        },
        tool_invocations=[
            {
                "tool": "search_concepts",
                "payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#scientific_paper",
                            "name": "scientific paper",
                            "kind": "type",
                        }
                    ],
                },
            },
            {
                "tool": "get_predicate_incidence",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "payload": {"success": True},
            },
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_prompt_tools") == [
        "search_concepts",
        "get_predicate_incidence",
    ]
    assert execution.get("missing_prompt_tools") == ["get_predicate_incidence"]

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    search_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["search_concepts"]
    )
    assert search_effect.get("status") == "satisfied"
    incidence_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["get_predicate_incidence"]
    )
    assert incidence_effect.get("status") == "not_executed"
    assert (
        incidence_effect.get("failure_code")
        == "prompt_required_evidence_get_predicate_incidence_missing"
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "prompt_required_evidence_get_predicate_incidence_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_turn_contract_fetch_bound_predicate_incidence_requires_fetched_target() -> (
    None
):
    record = _build_record(
        prompt_text="What are key predicates for scientific papers in Vontology?",
        response_text="Loaded: michael_witbrock",
        turn_expected_outcome_contract={
            "summary": "Identify key predicates for scientific papers.",
            "required_tools": [
                "search_concepts",
                "fetch_concept",
                "get_predicate_incidence",
            ],
        },
        tool_invocations=[
            {
                "tool": "search_concepts",
                "arguments": {"query": "scientific paper"},
                "payload": {"success": True},
            },
            {
                "tool": "fetch_concept",
                "arguments": {"concept_id": "#V#paper_on_arxiv_2603_01896"},
                "payload": {"success": True},
            },
            {
                "tool": "get_predicate_incidence",
                "arguments": {"concept_id": "#V#michael_witbrock"},
                "payload": {"success": True},
            },
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("missing_prompt_tools") == ["get_predicate_incidence"]

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    incidence_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["get_predicate_incidence"]
    )
    assert incidence_effect.get("status") == "not_executed"


def test_turn_contract_treats_vontology_search_as_search_concepts_alias() -> None:
    record = _build_record(
        prompt_text="What are key predicates for scientific papers in Vontology?",
        response_text="I still need relation summary evidence.",
        turn_expected_outcome_contract={
            "summary": "Identify key predicates for scientific papers.",
            "required_tools": [
                "search_concepts",
                "get_text_relations_summary",
            ],
        },
        tool_invocations=[
            {
                "tool": "vontology_concept_search",
                "payload": {
                    "success": True,
                    "results": [
                        {
                            "concept_id": "#V#scientific_paper",
                            "name": "scientific paper",
                            "kind": "type",
                        }
                    ],
                },
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("missing_prompt_tools") == ["get_text_relations_summary"]

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    search_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["search_concepts"]
    )
    assert search_effect.get("status") == "satisfied"
    relation_summary_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["get_text_relations_summary"]
    )
    assert relation_summary_effect.get("status") == "not_executed"


def test_prompt_required_evidence_contract_uses_metadata_authority_for_unknown_tool(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.is_tool_prompt_required_evidence",
        lambda tool_name: tool_name == "custom_source_query",
    )

    record = _build_record(
        prompt_text="Check the custom external source before answering.",
        response_text="I could not verify the custom source result.",
        required_prompt_tools=["custom_source_query"],
        tool_invocations=[],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    custom_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["custom_source_query"]
    )
    assert custom_effect.get("status") == "not_executed"
    assert custom_effect.get("failure_code") == (
        "prompt_required_evidence_custom_source_query_missing"
    )


def test_prompt_required_evidence_contract_does_not_block_false_empty_jira_answer_without_authoritative_critic_verdict() -> (
    None
):
    record = _build_record(
        prompt_text=(
            "Which of my open Jira tasks seem most closely connected to the papers "
            "and projects you know about me?"
        ),
        response_text=(
            "I couldn't identify any connections because no open Jira tasks were found."
        ),
        required_prompt_tools=["jira_search"],
        tool_invocations=[
            {
                "tool": "jira_search",
                "arguments": {
                    "jql": (
                        'statusCategory != Done AND (text ~ "\\"#V#michael_witbrock\\"" '
                        'OR text ~ "\\"Michael Witbrock\\"") ORDER BY updated DESC'
                    )
                },
                "effective_payload": {
                    "success": True,
                    "total": 1,
                    "issues": [
                        {
                            "key": "JVNAUTOSCI-1902",
                            "fields": {"summary": "Replay open-Jira task grounding"},
                        }
                    ],
                },
                "result_summary": "Found 1 issue",
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_blocked")
        is False
    )
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_source")
        is None
    )

    completion_gate = record.get("completion_gate") or {}
    assert (
        "prompt_required_evidence_jira_search_nonempty_results_contradict_empty_answer"
        not in (completion_gate.get("blocking_failure_codes") or [])
    )

    evidence_payload = completion_gate.get("evidence_payload") or {}
    blocker = evidence_payload.get("required_evidence_answer_consistency_blocker") or {}
    assert blocker == {}
    unresolved = evidence_payload.get("unresolved_preconditions") or []
    assert not any(
        isinstance(item, dict)
        and item.get("effect_type") == "required_evidence_answer_consistency"
        for item in unresolved
    )


def test_prompt_required_evidence_contract_does_not_block_without_authoritative_critic_verdict() -> (
    None
):
    record = _build_record(
        prompt_text="List grounded represented records linked to the current user.",
        response_text="2 results",
        required_prompt_tools=["search_knowledge_base"],
        tool_invocations=[
            {
                "tool": "search_knowledge_base",
                "arguments": {"query": "current user represented links"},
                "effective_payload": {
                    "success": True,
                    "count": 1,
                    "results": [
                        {
                            "id": "result:1",
                            "text": "Example Record is linked to Test User.",
                        }
                    ],
                },
                "result_summary": "Found 1 result",
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_blocked")
        is False
    )
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_source")
        is None
    )

    completion_gate = record.get("completion_gate") or {}
    assert (
        "prompt_required_evidence_positive_results_contradict_low_information_answer"
        not in (completion_gate.get("blocking_failure_codes") or [])
    )

    blocker = (completion_gate.get("evidence_payload") or {}).get(
        "required_evidence_answer_consistency_blocker"
    ) or {}
    assert blocker == {}


def test_prompt_required_evidence_contract_prefers_authoritative_critic_blocker() -> (
    None
):
    record = _build_record(
        prompt_text="List grounded represented records linked to the current user.",
        response_text="The answer needs manual review before completion is claimed.",
        required_prompt_tools=["search_knowledge_base"],
        tool_invocations=[
            {
                "tool": "search_knowledge_base",
                "arguments": {"query": "current user represented links"},
                "effective_payload": {
                    "success": True,
                    "count": 1,
                    "results": [
                        {
                            "id": "result:1",
                            "text": "Example Record is linked to Test User.",
                        }
                    ],
                },
                "result_summary": "Found 1 result",
            }
        ],
        critic_verdict={
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
            "required_evidence_answer_consistency_blocker": {
                "effect_type": "required_evidence_answer_consistency",
                "status": "not_satisfied",
                "status_reason": (
                    "Authoritative critic marked the answer as inconsistent with the retrieved evidence."
                ),
                "failure_code": (
                    "authoritative_required_evidence_answer_consistency_mismatch"
                ),
                "decision": "partial",
                "decision_reason": (
                    "Authoritative critic marked the answer as inconsistent with the retrieved evidence."
                ),
                "repeat_eligible": True,
            },
        },
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_blocked")
        is True
    )
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_source")
        == "critic_verdict"
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "partial"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert "authoritative_required_evidence_answer_consistency_mismatch" in (
        completion_gate.get("blocking_failure_codes") or []
    )

    blocker = (completion_gate.get("evidence_payload") or {}).get(
        "required_evidence_answer_consistency_blocker"
    ) or {}
    assert blocker.get("blocker_source") == "critic_verdict"
    assert blocker.get("status_reason") == (
        "Authoritative critic marked the answer as inconsistent with the retrieved evidence."
    )


def test_prompt_required_evidence_contract_does_not_block_degraded_retrieval_without_authoritative_critic_verdict() -> (
    None
):
    record = _build_record(
        prompt_text="List grounded represented records linked to the current user.",
        response_text="I couldn't find any grounded represented links.",
        required_prompt_tools=["search_knowledge_base"],
        tool_invocations=[
            {
                "tool": "search_knowledge_base",
                "arguments": {"query": "current user represented links"},
                "effective_payload": {
                    "success": True,
                    "fallback_used": True,
                    "fallback_reason": "rag_query_degraded:RateLimitError",
                    "count": 0,
                    "results": [],
                },
                "result_summary": "0 results after degraded fallback",
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_blocked")
        is False
    )
    assert (
        execution.get("summary", {}).get("required_evidence_answer_consistency_source")
        is None
    )

    completion_gate = record.get("completion_gate") or {}
    assert (
        "prompt_required_evidence_degraded_retrieval_not_safe_for_low_information_answer"
        not in (completion_gate.get("blocking_failure_codes") or [])
    )

    blocker = (completion_gate.get("evidence_payload") or {}).get(
        "required_evidence_answer_consistency_blocker"
    ) or {}
    assert blocker == {}


def test_prompt_required_mutation_contract_blocks_missing_task_create() -> None:
    record = _build_record(
        prompt_text=(
            "If I don't have one, please create me a diary entry for today. "
            "It should record the work I've already done."
        ),
        response_text=(
            "I couldn't confirm the creation of your diary entry because the "
            "verification of the details was inconclusive."
        ),
        required_prompt_tools=["task_create"],
        tool_invocations=[
            {
                "tool": "jira_search",
                "payload": {"success": True},
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("intent_class") == "mutation"
    assert contract.get("domain_profile_id") == "prompt_required_mutation"
    assert contract.get("default_decision_policy") is None

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    task_create_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["task_create"]
    )
    assert task_create_effect.get("status") == "not_executed"
    assert task_create_effect.get("failure_code") == (
        "prompt_required_mutation_task_create_missing"
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "prompt_required_mutation_task_create_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_prompt_required_mutation_contract_is_satisfied_by_task_create_execution() -> (
    None
):
    record = _build_record(
        prompt_text="Create a Von task for me titled 'Review replay results'.",
        response_text="Review replay results (#V#task_123).",
        required_prompt_tools=["task_create"],
        tool_invocations=[
            {
                "tool": "task_create",
                "payload": {
                    "success": True,
                    "task_concept_id": "#V#task_123",
                    "title": "Review replay results",
                },
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    task_create_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["task_create"]
    )
    assert task_create_effect.get("status") == "satisfied"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") != "escalation_required"
    assert "prompt_required_mutation_task_create_missing" not in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_prompt_required_workflow_execute_is_satisfied_by_custom_workflow_success() -> (
    None
):
    workflow_id = "#V#example_custom_workflow"
    workflow_summary = {
        "schema_version": "workflow_execution_summary.v1",
        "workflow_id": workflow_id,
        "completed": True,
        "effective_completed": True,
        "terminal_status": "completed",
        "final_state": "#V#workflow_step_example_custom_workflow_completed",
        "step_result_envelope_count": 3,
        "action_started_count": 3,
        "action_completed_count": 3,
        "action_success_count": 3,
        "action_failure_count": 0,
        "action_unknown_count": 0,
        "terminal_effect_count": 1,
        "terminal_effects": [],
        "durable_side_effect_count": 1,
        "durable_side_effects": [],
        "terminal_success_evaluation": {"success": True, "failure_codes": []},
    }
    record = _build_record(
        prompt_text="Execute the selected workflow for this request.",
        response_text="The selected workflow produced the requested result.",
        workflow_routing={
            "workflow_id": workflow_id,
            "verdict": "rag_selected",
            "source": "selector",
        },
        required_prompt_tools=["workflow_execute"],
        selected_workflow_trace={
            "selected_workflow_id": workflow_id,
            "selected_execution_mode": "custom_workflow",
            "child_workflow_completed": True,
            "child_workflow_final_state": (
                "#V#workflow_step_example_custom_workflow_completed"
            ),
            "workflow_execution_summary": workflow_summary,
        },
        aux_llm_calls=[
            {
                "type": "workflow_execution",
                "workflow_id": workflow_id,
                "completed": True,
                "execution_summary": workflow_summary,
            }
        ],
        tool_invocations=[],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    workflow_execute_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["workflow_execute"]
    )
    assert workflow_execute_effect.get("status") == "satisfied"

    execution = record.get("execution")
    assert isinstance(execution, dict)
    summary = execution.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("execution_surface_successful_tool_names") == [
        "workflow_execute"
    ]
    assert summary.get("required_effects_missing_required_tools") == []
    assert summary.get("missing_prompt_tools") == []

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True
    assert "prompt_required_mutation_workflow_execute_missing" not in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_selected_workflow_instance_evidence_satisfies_workflow_instance_readback() -> (
    None
):
    workflow_id = "#V#example_custom_workflow"
    workflow_summary = {
        "schema_version": "workflow_execution_summary.v1",
        "workflow_id": workflow_id,
        "completed": True,
        "effective_completed": True,
        "terminal_status": "completed",
        "final_state": "#V#workflow_step_example_custom_workflow_completed",
        "step_result_envelope_count": 2,
        "action_started_count": 2,
        "action_completed_count": 2,
        "action_success_count": 2,
        "action_failure_count": 0,
        "action_unknown_count": 0,
        "terminal_success_evaluation": {"success": True, "failure_codes": []},
    }

    record = _build_record(
        prompt_text="Execute the selected workflow and inspect its durable instance.",
        response_text="The selected workflow completed and its instance was inspected.",
        workflow_routing={
            "workflow_id": workflow_id,
            "verdict": "rag_selected",
            "source": "selector",
        },
        required_prompt_tools=["workflow_execute", "workflow_get_instance"],
        selected_workflow_trace={
            "selected_workflow_id": workflow_id,
            "selected_execution_mode": "custom_workflow",
            "child_workflow_completed": True,
            "child_workflow_final_state": (
                "#V#workflow_step_example_custom_workflow_completed"
            ),
            "child_result_snapshot": {
                "workflow_instance": {
                    "instance_id": "wf-instance-123",
                    "workflow_id": workflow_id,
                    "status": "completed",
                    "retry_count": 1,
                    "max_retries": 3,
                }
            },
            "workflow_execution_summary": workflow_summary,
        },
        tool_invocations=[],
    )

    summary = record["execution"]["summary"]
    assert summary["execution_surface_successful_tool_names"] == [
        "workflow_execute",
        "workflow_get_instance",
    ]
    assert summary["missing_prompt_tools"] == []
    assert summary["required_effects_missing_required_tools"] == []
    assert summary["required_tool_obligations"]["unsatisfied_required_tools"] == []
    assert summary["custom_workflow_execution"]["workflow_instance_id"] == (
        "wf-instance-123"
    )
    assert summary["custom_workflow_execution"]["workflow_instance_retry_count"] == 1

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("safe_to_claim_completion") is True
    assert "workflow_get_instance" not in (
        summary.get("required_effects_missing_required_tools") or []
    )


def test_prompt_required_workflow_execute_fails_closed_on_custom_workflow_failure() -> (
    None
):
    workflow_id = "#V#example_custom_workflow"
    workflow_summary = {
        "schema_version": "workflow_execution_summary.v1",
        "workflow_id": workflow_id,
        "completed": False,
        "effective_completed": False,
        "terminal_status": "failed",
        "final_state": "#V#workflow_step_example_custom_workflow_failed",
        "step_result_envelope_count": 2,
        "action_started_count": 2,
        "action_completed_count": 2,
        "action_success_count": 1,
        "action_failure_count": 1,
        "action_unknown_count": 0,
        "first_failing_state_id": "#V#workflow_step_example_custom_workflow_failed",
        "first_failing_action_id": "example_action",
        "terminal_effect_count": 0,
        "terminal_effects": [],
        "durable_side_effect_count": 0,
        "durable_side_effects": [],
        "terminal_success_evaluation": {
            "success": False,
            "failure_codes": ["example_terminal_contract_failed"],
            "decision_reason": "The workflow terminal-success contract failed.",
        },
    }
    record = _build_record(
        prompt_text="Execute the selected workflow for this request.",
        response_text="The selected workflow could not complete.",
        workflow_routing={
            "workflow_id": workflow_id,
            "verdict": "rag_selected",
            "source": "selector",
        },
        required_prompt_tools=["workflow_execute"],
        selected_workflow_trace={
            "selected_workflow_id": workflow_id,
            "selected_execution_mode": "custom_workflow",
            "child_workflow_completed": False,
            "child_workflow_final_state": (
                "#V#workflow_step_example_custom_workflow_failed"
            ),
            "child_workflow_error": "example terminal failure",
            "workflow_execution_summary": workflow_summary,
        },
        aux_llm_calls=[
            {
                "type": "workflow_execution",
                "workflow_id": workflow_id,
                "completed": False,
                "execution_summary": workflow_summary,
            }
        ],
        tool_invocations=[],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    workflow_execute_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["workflow_execute"]
    )
    assert workflow_execute_effect.get("status") == "not_satisfied"
    assert workflow_execute_effect.get("failure_code") == (
        "prompt_required_mutation_workflow_execute_failed"
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    summary = execution.get("summary")
    assert isinstance(summary, dict)
    assert summary.get("execution_surface_failed_tool_names") == ["workflow_execute"]
    assert summary.get("required_effects_missing_required_tools") == []

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "prompt_required_mutation_workflow_execute_failed" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_prompt_required_mutation_contract_uses_metadata_authority_for_unknown_tool(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.is_tool_prompt_required_mutation",
        lambda tool_name: tool_name == "custom_write_tool",
    )

    record = _build_record(
        prompt_text="Use the custom write tool to persist the prepared note.",
        response_text="The custom write action was not confirmed.",
        required_prompt_tools=["custom_write_tool"],
        tool_invocations=[],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    custom_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["custom_write_tool"]
    )
    assert custom_effect.get("status") == "not_executed"
    assert custom_effect.get("failure_code") == (
        "prompt_required_mutation_custom_write_tool_missing"
    )


def test_prompt_required_mutation_contract_respects_structured_write_denial() -> None:
    record = _build_record(
        prompt_text="Search for Qiming in the Vontology and do not create anything.",
        response_text="I searched and did not create anything.",
        required_prompt_tools=["create_concepts"],
        aux_llm_calls=[
            {
                "type": "write_tool_request_evidence",
                "request_evidence": {
                    "create_concepts": {
                        "request_state": "low_confidence",
                        "confirmation_state": "low_confidence",
                        "denial_state": "explicit_denial",
                        "rationale": "The current prompt explicitly forbids creation.",
                    }
                },
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None
    required_effects = record.get("required_effects") or []
    assert not any(
        effect.get("effect_type") == "required_mutation"
        for effect in required_effects
        if isinstance(effect, dict)
    )


def test_prompt_representation_contract_respects_structured_write_denial() -> None:
    record = _build_record(
        prompt_text="Do not download or store this arXiv paper: https://arxiv.org/abs/2510.06248",
        response_text="I did not download or store the paper.",
        aux_llm_calls=[
            {
                "type": "write_tool_request_evidence",
                "request_evidence": {
                    "download_paper": {
                        "request_state": "low_confidence",
                        "confirmation_state": "low_confidence",
                        "denial_state": "explicit_denial",
                        "rationale": "The current prompt explicitly forbids download.",
                    }
                },
            }
        ],
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    assert execution.get("required_effects_contract") is None
    required_effects = record.get("required_effects") or []
    assert not any(
        effect.get("effect_type") == "scholarly_representation"
        for effect in required_effects
        if isinstance(effect, dict)
    )


def test_required_evidence_permission_denied_is_preserved_distinctly(
    monkeypatch,
) -> None:
    _patch_workflow_required_effects_contract(monkeypatch)

    record = _build_record(
        prompt_text="Explain the failure in more detail from the telemetry.",
        tool_invocations=[
            {
                "tool": "conversation_telemetry_get_locator",
                "error": "PERMISSION_DENIED",
                "result_summary": "Error: PERMISSION_DENIED — Not authorised for conversation",
                "payload": {"success": False},
            },
            {
                "tool": "chat_history_get_segments",
                "payload": {"success": True},
            },
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    locator_effect = next(
        effect
        for effect in required_effects
        if effect.get("effect_id") == "conversation_locator"
    )
    assert locator_effect.get("status") == "not_satisfied"
    assert "required_evidence_permission_denied" in (
        locator_effect.get("failure_codes") or []
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "required_evidence_permission_denied" in (
        completion_gate.get("blocking_failure_codes") or []
    )
    assert "PERMISSION_DENIED" in (completion_gate.get("decision_reason") or "")


def test_workflow_lookup_failure_blocks_completion_claim() -> None:
    record = _build_record(
        prompt_text="What gmail profiles can you see?",
        response_text=(
            "I couldn't complete that request because the authoritative "
            "conversation-turn workflow definition was not available."
        ),
        workflow_routing={
            "workflow_id": "#V#conversation_turn_execution_workflow",
            "verdict": "custom_workflow",
            "source": "selector",
        },
        aux_llm_calls=[
            {
                "type": "workflow_instance_submission",
                "path": "orchestrator.execute_workflow",
                "status": "submitted",
                "reason_code": "durable_instance_created",
                "workflow_id": "#V#conversation_turn_execution_workflow",
                "workflow_instance_id": "348019b8-2563-4fba-b634-3adf93654235",
                "submission": {
                    "success": True,
                    "workflow_id": "#V#conversation_turn_execution_workflow",
                    "status": "submitted",
                    "instance_id": "348019b8-2563-4fba-b634-3adf93654235",
                    "verification": {
                        "runnable_verification_success": True,
                        "preflight_passed": True,
                        "postflight_passed": True,
                    },
                },
            },
            {
                "type": "workflow_use_episode",
                "workflow_id": "#V#conversation_turn_execution_workflow",
                "workflow_instance_id": "348019b8-2563-4fba-b634-3adf93654235",
                "completed": False,
                "terminal_stage": "workflow_lookup",
                "final_state": None,
                "termination_reason": {
                    "code": "workflow_not_registered",
                    "detail": "workflow_definition_not_found",
                },
            },
        ],
        turn_execution_diagnostics={
            "latest_progress": {
                "counters": {
                    "tools_started": 0,
                    "tools_completed": 0,
                },
                "diagnostic_events": [],
            }
        },
        tool_invocations=[],
    )

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert "workflow_access_failure" in (
        completion_gate.get("blocking_failure_codes") or []
    )

    execution_correctness = record.get("execution_correctness") or {}
    assert execution_correctness.get("overall_outcome") != "successful_completion"


def test_incidental_read_tool_failure_outside_required_evidence_contract_does_not_block(
    monkeypatch,
) -> None:
    _patch_workflow_required_effects_contract(monkeypatch)

    record = _build_record(
        prompt_text="Explain the failure in more detail from the telemetry.",
        response_text="Here is the telemetry explanation.",
        tool_invocations=[
            {
                "tool": "fetch_concept",
                "payload": {"success": True},
            },
            {
                "tool": "search_concepts",
                "error": "lookup_failed",
                "result_summary": "Search failed",
                "payload": {"success": False},
            },
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects == []

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True
