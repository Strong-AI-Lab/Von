"""Workflow identity constants for explicit chat and durable capabilities.

The executable definitions for these workflows are Vontology-authored and are
loaded through the workflow registry's authoritative loader. Retired universal
turn-controller identities remain as historical compatibility constants, but
are not members of the active chat workflow family.
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
GENERAL_MAIL_REVIEW_WORKFLOW_ID = "#V#general_mail_review_workflow"
GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID = "#V#gmail_message_detail_fetch_workflow"
KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID = (
    "#V#kb_mutation_postcondition_critic_workflow"
)
TURN_COMPLETION_GATE_WORKFLOW_ID = "#V#turn_completion_gate_workflow"
TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID = (
    "#V#turn_prompt_context_adjudication_workflow"
)
CONVERSATION_TURN_EXECUTION_WORKFLOW_ID = "#V#conversation_turn_execution_workflow"
WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID = (
    "#V#workflow_experience_context_prelude"
)
WORKFLOW_MODEL_SELECTION_WORKFLOW_ID = "#V#workflow_model_selection_workflow"

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
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
    GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    WORKFLOW_MODEL_SELECTION_WORKFLOW_ID,
)
