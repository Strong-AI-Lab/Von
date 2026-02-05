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
    CHAT_NARRATION_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
    register_default_workflows,
)
from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowResult,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from .workflow_registry import WorkflowRegistration, WorkflowRegistry
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
    "WorkflowSelector",
    "WorkflowSelection",
    "register_default_workflows",
    "CHAT_ASSISTANT_WORKFLOW_ID",
    "CHAT_NARRATION_WORKFLOW_ID",
    "MISSING_TOOL_CALL_WORKFLOW_ID",
    "TODO_REFRESH_WORKFLOW_ID",
    "TOOL_CALLING_WORKFLOW_ID",
    "WRITE_TOOL_POLICY_WORKFLOW_ID",
]
