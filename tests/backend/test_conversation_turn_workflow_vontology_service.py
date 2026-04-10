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

NARRATION_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_narrate_completion_report"


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
    assert report.get("seeded_prompt_count") == 1

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
    assert "narrate the results of a workflow execution" in prompt_text
    assert "operationally truthful and concise" in prompt_text


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
    assert required_effects_contract.get("required_effects")[0]["effect_id"] == (
        "conversation_locator"
    )
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

    failed_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="failed",
    )
    completed_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="completed",
    )
    transition_targets = {transition.to_state for transition in completion_gate.transitions}
    assert failed_step_id in transition_targets
    assert completed_step_id in transition_targets

    narration_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="narration",
    )
    narration_state = turn_definition.states[narration_step_id]
    narration_action = narration_state.actions[0]
    prompt_contract = narration_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == NARRATION_PROMPT_CONCEPT_ID
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
    episode_rows = get_texts_for_concept(
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in narration_rows)
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
