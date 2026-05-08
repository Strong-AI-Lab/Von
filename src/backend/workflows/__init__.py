"""LLM workflow execution and tracing utilities.

This package provides minimal, incremental foundations for JVNAUTOSCI-803.
The initial scope focuses on execution tracing (especially for chat/tool loops)
without forcing a full workflow engine migration.
"""

from .trace_model import WorkflowExecutionTrace, WorkflowStepTrace
from .trace_store import (
    get_workflow_execution_trace,
    insert_workflow_execution_trace,
    list_recent_workflow_execution_traces,
)
from .action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from .definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONVERSATION_TURN_WORKFLOW_IDS,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowResult,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from .workflow_registry import LazyWorkflowRegistration, WorkflowRegistration, WorkflowRegistry
from .workflow_selector import WorkflowSelection, WorkflowSelector

__all__ = [
    "WorkflowExecutionTrace",
    "WorkflowStepTrace",
    "ActionRegistry",
    "ActionSpec",
    "WorkflowActionResult",
    "WorkflowEnvironment",
    "WorkflowDefinition",
    "WorkflowActionInvocation",
    "WorkflowStateSpec",
    "WorkflowTransitionSpec",
    "WorkflowExecutor",
    "WorkflowResult",
    "get_workflow_execution_trace",
    "insert_workflow_execution_trace",
    "list_recent_workflow_execution_traces",
    "WorkflowRegistry",
    "WorkflowRegistration",
    "LazyWorkflowRegistration",
    "WorkflowSelector",
    "WorkflowSelection",
    "CHAT_ASSISTANT_WORKFLOW_ID",
    "CHAT_BUTTONIFY_WORKFLOW_ID",
    "CHAT_NARRATION_WORKFLOW_ID",
    "CONVERSATION_TURN_WORKFLOW_IDS",
    "CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID",
    "CONVERSATION_TURN_EXECUTION_WORKFLOW_ID",
    "GENERAL_MAIL_REVIEW_WORKFLOW_ID",
    "KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID",
    "MISSING_TOOL_CALL_WORKFLOW_ID",
    "TODO_REFRESH_WORKFLOW_ID",
    "TOOL_CALLING_WORKFLOW_ID",
    "TURN_COMPLETION_GATE_WORKFLOW_ID",
    "WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID",
    "WRITE_TOOL_POLICY_WORKFLOW_ID",
]
