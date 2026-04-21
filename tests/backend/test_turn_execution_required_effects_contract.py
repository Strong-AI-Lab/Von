from __future__ import annotations

import pytest

from representation_intent_regression_helpers import (
    assert_low_risk_default_policy,
    build_turn_record as _build_record,
    patch_representation_profile_loader,
)
from src.backend.workflows.engine import WorkflowDefinition


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

    class _Registry:
        def get(self, workflow_id: str):
            if workflow_id == "#V#tool_calling_workflow":
                return definition
            return None

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_shared_workflow_registry_read_only",
        lambda defer_parity_work=True: _Registry(),
    )


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


def test_prompt_file_copy_representation_request_emits_required_effects_contract() -> None:
    record = _build_record(
        prompt_text="Fully represent the corresponding paper from #V#uploaded_file_copy_abc123."
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
    effect = required_effects[0]
    assert effect.get("effect_type") == "scholarly_representation"
    assert effect.get("targets") == ["#V#uploaded_file_copy_abc123"]
    assert effect.get("required_tools") == [
        "materialise_scholarly_representation_for_file_copy"
    ]
    assert effect.get("required_tools_match") == "any"


def test_prompt_tool_requirement_telemetry_can_supply_missing_target_ids() -> None:
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
    assert contract.get("artefact_context", {}).get("file_copy_ids") == [
        "#V#uploaded_file_copy_abc123"
    ]
    required_effects = record.get("required_effects") or []
    assert len(required_effects) == 1
    assert required_effects[0].get("targets") == ["#V#uploaded_file_copy_abc123"]


def test_prompt_targeted_representation_request_emits_required_effects_contract() -> None:
    record = _build_record(
        prompt_text="Represent this person profile from this CV file #V#uploaded_file_copy_person_1.",
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("domain_profile_id") == "person"
    assert_low_risk_default_policy(contract)
    assert contract.get("artefact_context", {}).get("file_copy_ids") == [
        "#V#uploaded_file_copy_person_1"
    ]

    required_effects = record.get("required_effects") or []
    assert len(required_effects) == 1
    effect = required_effects[0]
    assert effect.get("effect_type") == "representation_person"
    assert effect.get("targets") == ["#V#uploaded_file_copy_person_1"]
    assert effect.get("status") == "not_executed"


def test_low_risk_arxiv_prompt_list_emits_one_required_effect_per_target() -> None:
    record = _build_record(
        prompt_text=(
            "eprint version: https://arxiv.org/abs/2310.03714\n"
            "arXiv preprint version: https://arxiv.org/abs/2308.03688"
        ),
        response_text="Still working on it.",
    )

    execution = record.get("execution")
    assert isinstance(execution, dict)
    contract = execution.get("required_effects_contract")
    assert isinstance(contract, dict)
    assert contract.get("domain_profile_id") == "paper"
    assert contract.get("artefact_context", {}).get("urls") == [
        "https://arxiv.org/abs/2310.03714",
        "https://arxiv.org/abs/2308.03688",
    ]
    assert_low_risk_default_policy(contract)

    required_effects = record.get("required_effects") or []
    assert len(required_effects) == 2
    assert [effect.get("targets") for effect in required_effects] == [
        ["https://arxiv.org/abs/2310.03714"],
        ["https://arxiv.org/abs/2308.03688"],
    ]
    assert all(effect.get("required_tools_match") == "all" for effect in required_effects)
    assert all(effect.get("status") == "not_executed" for effect in required_effects)

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("requires_follow_up") is True
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

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    jira_effect = next(
        effect
        for effect in required_effects
        if effect.get("required_tools") == ["jira_search"]
    )
    assert jira_effect.get("status") == "not_executed"
    assert jira_effect.get("failure_code") == "prompt_required_evidence_jira_search_missing"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "escalation_required"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert "prompt_required_evidence_jira_search_missing" in (
        completion_gate.get("blocking_failure_codes") or []
    )


def test_prompt_required_evidence_contract_does_not_block_false_empty_jira_answer_without_authoritative_critic_verdict() -> None:
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
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_blocked"
    ) is False
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_source"
    ) is None

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


def test_prompt_required_evidence_contract_does_not_block_without_authoritative_critic_verdict() -> None:
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
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_blocked"
    ) is False
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_source"
    ) is None

    completion_gate = record.get("completion_gate") or {}
    assert (
        "prompt_required_evidence_positive_results_contradict_low_information_answer"
        not in (completion_gate.get("blocking_failure_codes") or [])
    )

    blocker = (
        (completion_gate.get("evidence_payload") or {}).get(
            "required_evidence_answer_consistency_blocker"
        )
        or {}
    )
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
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_blocked"
    ) is True
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_source"
    ) == "critic_verdict"

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "partial"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert "authoritative_required_evidence_answer_consistency_mismatch" in (
        completion_gate.get("blocking_failure_codes") or []
    )

    blocker = (
        (completion_gate.get("evidence_payload") or {}).get(
            "required_evidence_answer_consistency_blocker"
        )
        or {}
    )
    assert blocker.get("blocker_source") == "critic_verdict"
    assert blocker.get("status_reason") == (
        "Authoritative critic marked the answer as inconsistent with the retrieved evidence."
    )


def test_prompt_required_evidence_contract_does_not_block_degraded_retrieval_without_authoritative_critic_verdict() -> None:
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
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_blocked"
    ) is False
    assert execution.get("summary", {}).get(
        "required_evidence_answer_consistency_source"
    ) is None

    completion_gate = record.get("completion_gate") or {}
    assert (
        "prompt_required_evidence_degraded_retrieval_not_safe_for_low_information_answer"
        not in (completion_gate.get("blocking_failure_codes") or [])
    )

    blocker = (
        (completion_gate.get("evidence_payload") or {}).get(
            "required_evidence_answer_consistency_blocker"
        )
        or {}
    )
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


def test_prompt_required_mutation_contract_is_satisfied_by_task_create_execution() -> None:
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
            }
        ],
    )

    required_effects = record.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects == []

    completion_gate = record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True
