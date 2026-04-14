from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.conversation_turn_workflow_vontology_service import (
    _ensure_conversation_turn_prompt_support,
    bootstrap_canonical_conversation_turn_workflows,
)
from src.backend.services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_WORKFLOW_ID,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.vontology_loader import load_workflow_definition_from_vontology

SELECTOR_PROMPT_CONCEPT_ID = "#V#chat_turn_classifier_prompt"
NARRATION_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_narrate_completion_report"
RECOVERY_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_recovery_decision"


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_conversation_turn_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_conversation_turn_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 3

    selector_rows = get_texts_for_concept(
        SELECTOR_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    selector_text = next(
        ((row or {}).get("text") for row in selector_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(selector_text, str)
    assert "full turn context as LLM context messages" in selector_text
    assert "Do not assume the current request is standalone" in selector_text

    prompt_rows = get_texts_for_concept(
        NARRATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    assert "Selected Workflow User Response" in prompt_text
    assert "Completion Report` is supporting evidence only" in prompt_text
    assert "Do not narrate workflow bookkeeping as the response" in prompt_text

    recovery_rows = get_texts_for_concept(
        RECOVERY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    recovery_text = next(
        ((row or {}).get("text") for row in recovery_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(recovery_text, str)
    assert "recovery-decision policy" in recovery_text
    assert "next best bounded automated step" in recovery_text
    assert "`decision`" in recovery_text
    assert "`\"retry_execution\"` or `\"respond_with_follow_up\"`" in recovery_text


def test_bootstrap_materialises_conversation_turn_workflow_family_and_prompt_links(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_conversation_turn_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert report.get("success") is True
    assert counts.get("errors") == 0
    assert counts.get("workflows_published") == 4

    chat_definition = load_workflow_definition_from_vontology(CHAT_ASSISTANT_WORKFLOW_ID)
    assert chat_definition is not None
    chat_respond_step_id = authority_service._step_concept_id(
        workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
        state_id="respond",
    )
    chat_respond_action = chat_definition.states[chat_respond_step_id].actions[0]
    assert chat_respond_action.action_id == "tool_calling.respond"

    tool_calling_definition = load_workflow_definition_from_vontology(
        TOOL_CALLING_WORKFLOW_ID
    )
    assert tool_calling_definition is not None
    tool_calling_respond_step_id = authority_service._step_concept_id(
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        state_id="respond",
    )
    tool_calling_respond_action = tool_calling_definition.states[
        tool_calling_respond_step_id
    ].actions[0]
    assert tool_calling_respond_action.action_id == "tool_calling.respond"
    assert tool_calling_respond_action.execution_mode == "deterministic"
    assert tool_calling_respond_action.llm_policy is None
    required_effects_contract = tool_calling_definition.metadata.get(
        "required_effects_contract"
    )
    assert isinstance(required_effects_contract, dict)
    assert required_effects_contract.get("contract_id") == (
        "conversation_diagnostics_required_evidence"
    )
    required_effects = required_effects_contract.get("required_effects")
    assert isinstance(required_effects, list)
    assert required_effects[0]["effect_id"] == "conversation_locator"
    contract_rows = get_texts_for_concept(
        TOOL_CALLING_WORKFLOW_ID,
        predicate="#V#hasWorkflowRequiredEffectsContractJson",
        limit=5,
    )
    assert any((row or {}).get("text") for row in contract_rows)

    turn_definition = load_workflow_definition_from_vontology(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert turn_definition is not None

    critic_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="critic",
    )
    critic_action = turn_definition.states[critic_step_id].actions[0]
    assert critic_action.action_id == "turn_execution.critic"

    completion_gate_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="completion_gate",
    )
    completion_gate = turn_definition.states[completion_gate_step_id]
    completion_gate_action = completion_gate.actions[0]
    assert completion_gate_action.action_id == "turn_execution.completion_gate"

    recovery_decision_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="recovery_decision",
    )
    recovery_decision = turn_definition.states[recovery_decision_step_id]
    recovery_action = recovery_decision.actions[0]
    assert recovery_action.action_id == "llm.action"
    assert recovery_action.validation_policy == {"output_format": "json_value"}
    recovery_prompt_contract = recovery_action.prompt_contract
    assert isinstance(recovery_prompt_contract, dict)
    assert recovery_prompt_contract.get("resolved_prompt_concept_id") == (
        RECOVERY_PROMPT_CONCEPT_ID
    )
    recovery_context_fields = (recovery_action.llm_policy or {}).get("context_fields")
    assert isinstance(recovery_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "workflow_discovery_result"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "completion_gate_repeat_eligible"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_recovery_last_reasoning"
        for field in recovery_context_fields
    )

    completed_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="completed",
    )
    execution_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="execution",
    )
    recovery_follow_up_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="apply_recovery_follow_up",
    )
    transition_targets = {transition.to_state for transition in completion_gate.transitions}
    assert execution_step_id not in transition_targets
    assert recovery_decision_step_id in transition_targets
    assert completed_step_id in transition_targets
    recovery_transition_targets = {
        transition.to_state for transition in recovery_decision.transitions
    }
    assert recovery_follow_up_step_id in recovery_transition_targets

    narration_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="narration",
    )
    narration_state = turn_definition.states[narration_step_id]
    narration_action = narration_state.actions[0]
    prompt_contract = narration_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == NARRATION_PROMPT_CONCEPT_ID
    narration_context_fields = (narration_action.llm_policy or {}).get("context_fields")
    assert isinstance(narration_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "selected_workflow_user_response"
        for field in narration_context_fields
    )
    narration_mappings = narration_state.metadata.get("tool_output_context_mappings") or []
    assert any(
        mapping.get("tool_output_field") == "final_response"
        and mapping.get("context_key") == "response_text"
        for mapping in narration_mappings
        if isinstance(mapping, dict)
    )


def test_turn_and_episode_prompt_authority_resolve_on_live_surface(
    _reset_mock_db: Any,
) -> None:
    turn_report = bootstrap_canonical_conversation_turn_workflows()
    episode_report = bootstrap_canonical_episode_evaluation_workflow()

    assert turn_report.get("success") is True
    assert episode_report.get("success") is True

    narration_rows = get_texts_for_concept(
        NARRATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    recovery_rows = get_texts_for_concept(
        RECOVERY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    episode_rows = get_texts_for_concept(
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    selector_rows = get_texts_for_concept(
        SELECTOR_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in selector_rows)
    assert any((row or {}).get("text") for row in narration_rows)
    assert any((row or {}).get("text") for row in recovery_rows)
    assert any((row or {}).get("text") for row in episode_rows)

    episode_definition = load_workflow_definition_from_vontology(
        EPISODE_EVALUATION_WORKFLOW_ID
    )
    assert episode_definition is not None
    evaluate_step_id = authority_service._step_concept_id(
        workflow_id=EPISODE_EVALUATION_WORKFLOW_ID,
        state_id="evaluate_episode",
    )
    evaluate_action = episode_definition.states[evaluate_step_id].actions[0]
    prompt_contract = evaluate_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == (
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID
    )
