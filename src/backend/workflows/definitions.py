"""Built-in workflow definitions for chat orchestration."""

from __future__ import annotations

from typing import Iterable

from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from .workflow_registry import WorkflowRegistration, WorkflowRegistry


MISSING_TOOL_CALL_WORKFLOW_ID = "#V#missing_tool_call_workflow"
CHAT_NARRATION_WORKFLOW_ID = "#V#chat_narration_workflow"
CHAT_ASSISTANT_WORKFLOW_ID = "#V#chat_assistant_workflow"
TODO_REFRESH_WORKFLOW_ID = "#V#todo_refresh_workflow"


def _transition_if_flag_set(flag: str, *, to_state: str, reason: str) -> WorkflowTransitionSpec:
    return WorkflowTransitionSpec(
        to_state=to_state,
        condition=lambda ctx: bool(ctx.get(flag)),
        reason=reason,
    )


def build_missing_tool_call_workflow() -> WorkflowDefinition:
    observed = WorkflowStateSpec(
        state_id="observed",
        actions=(
            WorkflowActionInvocation(
                action_id="missing_tool_call.assess",
                description="Assess whether a missing tool call retry is needed.",
            ),
        ),
        transitions=(
            _transition_if_flag_set(
                "missing_tool_call_retry_needed",
                to_state="needs_retry",
                reason="retry_needed",
            ),
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="no_retry_required",
            ),
        ),
    )

    needs_retry = WorkflowStateSpec(
        state_id="needs_retry",
        actions=(
            WorkflowActionInvocation(
                action_id="missing_tool_call.retry",
                description="Run retry prompt to elicit tool call JSON.",
            ),
        ),
        transitions=(
            _transition_if_flag_set(
                "missing_tool_call_retry_success",
                to_state="completed",
                reason="retry_succeeded",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: True,
                reason="retry_failed",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=MISSING_TOOL_CALL_WORKFLOW_ID,
        initial_state="observed",
        states={
            "observed": observed,
            "needs_retry": needs_retry,
            "completed": completed,
            "failed": failed,
        },
        termination_states=("completed", "failed"),
        purpose="Recover missing tool-call outputs from assistant responses.",
    )


def build_chat_narration_workflow() -> WorkflowDefinition:
    classify_need = WorkflowStateSpec(
        state_id="classify_need",
        actions=(WorkflowActionInvocation(action_id="narration.classify"),),
        transitions=(
            _transition_if_flag_set(
                "narration_required",
                to_state="select_prompt_fragments",
                reason="narration_required",
            ),
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="narration_not_required",
            ),
        ),
    )

    select_prompt = WorkflowStateSpec(
        state_id="select_prompt_fragments",
        actions=(WorkflowActionInvocation(action_id="narration.select_prompts"),),
        transitions=(
            _transition_if_flag_set(
                "narration_prompts_resolved",
                to_state="render_narration",
                reason="prompt_resolved",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: True,
                reason="prompt_resolution_failed",
            ),
        ),
    )

    render = WorkflowStateSpec(
        state_id="render_narration",
        actions=(WorkflowActionInvocation(action_id="narration.render"),),
        transitions=(
            _transition_if_flag_set(
                "narration_rendered",
                to_state="emit_audio",
                reason="rendered",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: True,
                reason="render_failed",
            ),
        ),
    )

    emit_audio = WorkflowStateSpec(
        state_id="emit_audio",
        actions=(WorkflowActionInvocation(action_id="narration.emit_audio"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="emitted",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=CHAT_NARRATION_WORKFLOW_ID,
        initial_state="classify_need",
        states={
            "classify_need": classify_need,
            "select_prompt_fragments": select_prompt,
            "render_narration": render,
            "emit_audio": emit_audio,
            "completed": completed,
            "failed": failed,
        },
        termination_states=("completed", "failed"),
        purpose="Generate and emit narration for chat responses.",
    )


def build_todo_refresh_workflow() -> WorkflowDefinition:
    ready_check = WorkflowStateSpec(
        state_id="check_cache_freshness",
        actions=(WorkflowActionInvocation(action_id="todo_refresh.check_cache"),),
        transitions=(
            _transition_if_flag_set(
                "todo_refresh_needed",
                to_state="maybe_fetch_gmail",
                reason="refresh_needed",
            ),
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="cache_fresh",
            ),
        ),
    )

    maybe_fetch_gmail = WorkflowStateSpec(
        state_id="maybe_fetch_gmail",
        actions=(WorkflowActionInvocation(action_id="todo_refresh.fetch_gmail"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="extract_tasks",
                condition=lambda ctx: True,
                reason="gmail_checked",
            ),
        ),
    )

    extract_tasks = WorkflowStateSpec(
        state_id="extract_tasks",
        actions=(WorkflowActionInvocation(action_id="todo_refresh.extract_tasks"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="prioritise",
                condition=lambda ctx: True,
                reason="tasks_extracted",
            ),
        ),
    )

    prioritise = WorkflowStateSpec(
        state_id="prioritise",
        actions=(WorkflowActionInvocation(action_id="todo_refresh.prioritise"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="summarise",
                condition=lambda ctx: True,
                reason="prioritised",
            ),
        ),
    )

    summarise = WorkflowStateSpec(
        state_id="summarise",
        actions=(WorkflowActionInvocation(action_id="todo_refresh.summarise"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="summarised",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=TODO_REFRESH_WORKFLOW_ID,
        initial_state="check_cache_freshness",
        states={
            "check_cache_freshness": ready_check,
            "maybe_fetch_gmail": maybe_fetch_gmail,
            "extract_tasks": extract_tasks,
            "prioritise": prioritise,
            "summarise": summarise,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose="Refresh to-do list from Gmail and cached knowledge.",
    )


def register_default_workflows(registry: WorkflowRegistry) -> None:
    for registration in (
        WorkflowRegistration(
            workflow_id=MISSING_TOOL_CALL_WORKFLOW_ID,
            definition=build_missing_tool_call_workflow(),
            purpose="Handle missing tool-call recovery and retry.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=CHAT_NARRATION_WORKFLOW_ID,
            definition=build_chat_narration_workflow(),
            purpose="Narration generation pipeline.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
            definition=WorkflowDefinition(
                workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
                initial_state="completed",
                states={"completed": WorkflowStateSpec(state_id="completed", terminal=True)},
                termination_states=("completed",),
                purpose="Default chat assistant no-op workflow placeholder.",
            ),
            purpose="Base chat assistant workflow.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=TODO_REFRESH_WORKFLOW_ID,
            definition=build_todo_refresh_workflow(),
            purpose="Refresh to-dos using Gmail and KB data.",
            source="built_in",
        ),
    ):
        registry.register(registration)
