from __future__ import annotations

from typing import Any

from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from src.backend.workflows.workflow_registry import (
    LazyWorkflowRegistration,
    WorkflowRegistration,
    WorkflowRegistry,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service


TEST_WORKFLOW_PURPOSES: dict[str, str] = {
    MISSING_TOOL_CALL_WORKFLOW_ID: (
        "Recovery workflow for conversation turns where expected tool-call JSON "
        "was missing and a targeted retry may repair the response."
    ),
    CHAT_NARRATION_WORKFLOW_ID: (
        "Narrative generation workflow for conversation turns that need "
        "storytelling, framing, or persona-consistent narration."
    ),
    CHAT_BUTTONIFY_WORKFLOW_ID: (
        "Output-transformation workflow that converts an assistant response into "
        "safe quick-reply button options."
    ),
    CHAT_ASSISTANT_WORKFLOW_ID: (
        "Direct conversational response workflow for greetings, hello messages, "
        "simple questions, clarifications, thank-you replies, and general chat "
        "turns that do not require tools."
    ),
    TODO_REFRESH_WORKFLOW_ID: (
        "Refresh the user's to-do list from Gmail and knowledge-base task data."
    ),
    WRITE_TOOL_POLICY_WORKFLOW_ID: (
        "Policy decision workflow that allows, denies, or escalates proposed "
        "write-category tool invocations."
    ),
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID: (
        "Specialised ontology preflight workflow that emits guarded fallback "
        "type and predicate suggestions when baseline discovery is sparse."
    ),
    TOOL_CALLING_WORKFLOW_ID: (
        "General-purpose tool-calling pipeline for turns that require MCP tools, "
        "data retrieval, file operations, external APIs, web search, arXiv paper "
        "search, transformer-paper lookup, or knowledge-base writes."
    ),
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID: (
        "Critic workflow that evaluates whether required knowledge-base mutation "
        "effects were executed and verified."
    ),
    TURN_COMPLETION_GATE_WORKFLOW_ID: (
        "Completion-gate workflow that prevents false completion claims when "
        "required effects remain unresolved or unverified."
    ),
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID: (
        "Canonical conversation-turn execution workflow that derives required "
        "effects, runs postcondition checks, and gates completion claims."
    ),
}


def build_test_conversation_turn_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    register_test_conversation_turn_workflows(registry)
    return registry


def register_test_conversation_turn_workflows(
    registry: WorkflowRegistry,
) -> WorkflowRegistry:
    for workflow_id in authority_service.CANONICAL_CHAT_WORKFLOW_IDS:
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id=workflow_id,
                purpose=TEST_WORKFLOW_PURPOSES.get(workflow_id, workflow_id),
                source="vontology",
            )
        )
    return registry


def build_authoritative_test_workflow_definition(workflow_id: str) -> Any:
    spec = authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS[workflow_id]
    return authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=spec,
    )


def register_authoritative_test_workflows(
    registry: WorkflowRegistry,
    workflow_ids: tuple[str, ...] | None = None,
) -> WorkflowRegistry:
    target_ids = workflow_ids or authority_service.CANONICAL_CHAT_WORKFLOW_IDS
    for workflow_id in target_ids:
        registry.register(
            WorkflowRegistration(
                workflow_id=workflow_id,
                definition=build_authoritative_test_workflow_definition(workflow_id),
                purpose=TEST_WORKFLOW_PURPOSES.get(workflow_id, workflow_id),
                source="vontology",
            )
        )
    return registry


def authoritative_step_id(*, workflow_id: str, state_id: str) -> str:
    return authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id=state_id,
    )
