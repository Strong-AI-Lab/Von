"""Canonical workflow identity constants for conversation-turn orchestration.

The executable definitions for these workflows are Vontology-authored and are
loaded through the workflow registry's authoritative loader. Python no longer
defines or registers the workflow graphs for this family.
"""

from __future__ import annotations

MISSING_TOOL_CALL_WORKFLOW_ID = "#V#missing_tool_call_workflow"
CHAT_NARRATION_WORKFLOW_ID = "#V#chat_narration_workflow"
CHAT_BUTTONIFY_WORKFLOW_ID = "#V#chat_buttonify_workflow"
CHAT_ASSISTANT_WORKFLOW_ID = "#V#chat_assistant_workflow"
TODO_REFRESH_WORKFLOW_ID = "#V#todo_refresh_workflow"
WRITE_TOOL_POLICY_WORKFLOW_ID = "#V#write_tool_policy_workflow"
CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID = (
    "#V#concept_suggestion_preflight_workflow"
)
TOOL_CALLING_WORKFLOW_ID = "#V#tool_calling_workflow"
KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID = (
    "#V#kb_mutation_postcondition_critic_workflow"
)
TURN_COMPLETION_GATE_WORKFLOW_ID = "#V#turn_completion_gate_workflow"
CONVERSATION_TURN_EXECUTION_WORKFLOW_ID = "#V#conversation_turn_execution_workflow"
WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID = (
    "#V#workflow_experience_context_prelude"
)

ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"

CONVERSATION_TURN_WORKFLOW_IDS: tuple[str, ...] = (
    MISSING_TOOL_CALL_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_ASSISTANT_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
)
