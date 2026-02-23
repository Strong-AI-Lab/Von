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


def _transition_if_flag_set(
    flag: str, *, to_state: str, reason: str
) -> WorkflowTransitionSpec:
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
            WorkflowTransitionSpec(
                to_state="render_narration",
                condition=lambda ctx: True,
                reason="prompt_selected",
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


def build_chat_buttonify_workflow() -> WorkflowDefinition:
    """Build workflow for quick-reply option extraction from final screen text.

    This is a concrete output-transformation workflow instance and is intended
    to be reusable as a pattern for additional response transforms.
    """

    assess = WorkflowStateSpec(
        state_id="assess_input",
        actions=(WorkflowActionInvocation(action_id="buttonify.assess_input"),),
        transitions=(
            _transition_if_flag_set(
                "buttonify_should_run",
                to_state="select_prompt",
                reason="eligible",
            ),
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="skipped",
            ),
        ),
    )

    select_prompt = WorkflowStateSpec(
        state_id="select_prompt",
        actions=(WorkflowActionInvocation(action_id="buttonify.select_prompt"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="extract_options",
                condition=lambda ctx: True,
                reason="prompt_selected",
            ),
        ),
    )

    extract = WorkflowStateSpec(
        state_id="extract_options",
        actions=(WorkflowActionInvocation(action_id="buttonify.extract_options"),),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="transformation_completed",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
        initial_state="assess_input",
        states={
            "assess_input": assess,
            "select_prompt": select_prompt,
            "extract_options": extract,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose="Produce quick-reply action options as output transformation.",
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


def build_write_tool_policy_workflow() -> WorkflowDefinition:
    decide = WorkflowStateSpec(
        state_id="decide",
        actions=(
            WorkflowActionInvocation(
                action_id="write_policy.decide",
                description="Decide which write-category tools are allowed for this user prompt.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="decided",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=WRITE_TOOL_POLICY_WORKFLOW_ID,
        initial_state="decide",
        states={
            "decide": decide,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose="Determine which write tools are allowed for a chat prompt.",
    )


def build_concept_suggestion_preflight_workflow() -> WorkflowDefinition:
    """Workflow for Stage-5 specialised concept suggestion fallback.

    This stage is intentionally lightweight and deterministic. The action
    applies guardrailed fallback concept suggestion only when earlier
    preflight discovery stages are sparse.
    """

    suggest = WorkflowStateSpec(
        state_id="suggest",
        actions=(
            WorkflowActionInvocation(
                action_id="preflight.specialised_suggest",
                description=(
                    "Generate specialised fallback type/predicate suggestions "
                    "when baseline ontology preflight is sparse."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="evaluated",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
        initial_state="suggest",
        states={
            "suggest": suggest,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose=(
            "Evaluate specialised fallback concept suggestions for ontology "
            "preflight when baseline discovery is sparse."
        ),
    )


def build_tool_calling_workflow() -> WorkflowDefinition:
    """Workflow wrapping the standard tool-calling pipeline.

    Flow::

        plan  ──[tool_calls_present]──▸  validate
          │                                  │
          └──[direct_response]──▸ completed   ├──[tool_calls_present]──▸ execute ──▸ backfill
                                              │                                       │
                                              └──[no tools]──▸ completed               ├──[more_tool_calls]──▸ validate  (loop)
                                                                                       └──[done]──▸ completed

    This expresses the same logic currently inline in ``orchestrator.run()``
    (LLM call → missing-tool-call recovery → preflight → tool execution →
    screen backfill) as a declarative workflow so that it can participate in
    workflow selection alongside narration, todo refresh, etc.

    The execute handler processes **one batch** of tool calls (respecting
    ``tool_batch_cap``).  Overflow batches and chained tool calls from the
    backfill LLM response re-enter via ``validate``.

    See JVNAUTOSCI-922 Phase 2.
    """
    plan = WorkflowStateSpec(
        state_id="plan",
        actions=(
            WorkflowActionInvocation(
                action_id="tool_calling.plan",
                description="Generate tool calls via LLM (structured or legacy).",
            ),
        ),
        transitions=(
            _transition_if_flag_set(
                "tool_calls_present",
                to_state="validate",
                reason="tool_calls_found",
            ),
            # No tool calls found (or error with pre-built result) still flows
            # through turn-execution critic + completion gate.
            WorkflowTransitionSpec(
                to_state="postcondition_critic",
                condition=lambda ctx: True,
                reason="direct_response",
            ),
        ),
    )

    validate = WorkflowStateSpec(
        state_id="validate",
        actions=(
            WorkflowActionInvocation(
                action_id="tool_calling.validate",
                description="Preflight validation, coercion, and repair of tool calls.",
            ),
        ),
        transitions=(
            _transition_if_flag_set(
                "tool_calls_validated",
                to_state="execute",
                reason="validation_passed",
            ),
            # Validation errors must still pass through critic + completion gate
            # so unresolved required effects cannot be marked as safely complete.
            WorkflowTransitionSpec(
                to_state="postcondition_critic",
                condition=lambda ctx: True,
                reason="validation_error",
            ),
        ),
    )

    execute = WorkflowStateSpec(
        state_id="execute",
        actions=(
            WorkflowActionInvocation(
                action_id="tool_calling.execute",
                description="Execute tool-call batch against MCP gateway.",
            ),
        ),
        transitions=(
            # Execute always transitions to backfill (even on errors, the
            # backfill summariser can explain what went wrong).
            WorkflowTransitionSpec(
                to_state="backfill",
                condition=lambda ctx: True,
                reason="batch_executed",
            ),
        ),
    )

    backfill = WorkflowStateSpec(
        state_id="backfill",
        actions=(
            WorkflowActionInvocation(
                action_id="tool_calling.backfill",
                description="Summariser LLM call; detect chained tool calls.",
            ),
        ),
        transitions=(
            # Chained tool calls from the summariser response
            _transition_if_flag_set(
                "more_tool_calls",
                to_state="validate",
                reason="chained_tool_calls",
            ),
            WorkflowTransitionSpec(
                to_state="postcondition_critic",
                condition=lambda ctx: True,
                reason="backfill_done",
            ),
        ),
    )

    postcondition_critic = WorkflowStateSpec(
        state_id="postcondition_critic",
        actions=(
            WorkflowActionInvocation(
                action_id="turn_execution.critic",
                description="Evaluate required effects and postcondition checks.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completion_gate",
                condition=lambda ctx: True,
                reason="critic_completed",
            ),
        ),
    )

    completion_gate = WorkflowStateSpec(
        state_id="completion_gate",
        actions=(
            WorkflowActionInvocation(
                action_id="turn_execution.completion_gate",
                description="Decide whether completion can be claimed safely.",
            ),
        ),
        transitions=(
            _transition_if_flag_set(
                "completion_gate_requires_follow_up",
                to_state="completed",
                reason="follow_up_required",
            ),
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="completion_gate_passed",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        initial_state="plan",
        states={
            "plan": plan,
            "validate": validate,
            "execute": execute,
            "backfill": backfill,
            "postcondition_critic": postcondition_critic,
            "completion_gate": completion_gate,
            "completed": completed,
            "failed": failed,
        },
        termination_states=("completed", "failed"),
        purpose=(
            "Standard tool-calling pipeline: plan → validate → execute → backfill "
            "→ postcondition critic → completion gate."
        ),
    )


def build_kb_mutation_postcondition_critic_workflow() -> WorkflowDefinition:
    evaluate = WorkflowStateSpec(
        state_id="evaluate",
        actions=(
            WorkflowActionInvocation(
                action_id="turn_execution.critic",
                description="Evaluate KB mutation postconditions.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="evaluated",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
        initial_state="evaluate",
        states={
            "evaluate": evaluate,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose=(
            "Evaluate whether implicit KB mutation effects were executed and verified."
        ),
    )


def build_turn_completion_gate_workflow() -> WorkflowDefinition:
    decide = WorkflowStateSpec(
        state_id="decide",
        actions=(
            WorkflowActionInvocation(
                action_id="turn_execution.completion_gate",
                description="Apply completion gate policy for turn execution.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="decided",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=TURN_COMPLETION_GATE_WORKFLOW_ID,
        initial_state="decide",
        states={
            "decide": decide,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose="Determine if it is safe to claim turn completion.",
    )


def build_conversation_turn_execution_workflow() -> WorkflowDefinition:
    critic = WorkflowStateSpec(
        state_id="critic",
        actions=(
            WorkflowActionInvocation(
                action_id="turn_execution.critic",
                description="Build execution evidence and postcondition checks.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completion_gate",
                condition=lambda ctx: True,
                reason="critic_completed",
            ),
        ),
    )

    completion_gate = WorkflowStateSpec(
        state_id="completion_gate",
        actions=(
            WorkflowActionInvocation(
                action_id="turn_execution.completion_gate",
                description="Apply completion gate to execution evidence.",
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="completed",
                condition=lambda ctx: True,
                reason="completion_gate_decided",
            ),
        ),
    )

    completed = WorkflowStateSpec(state_id="completed", terminal=True)

    return WorkflowDefinition(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        initial_state="critic",
        states={
            "critic": critic,
            "completion_gate": completion_gate,
            "completed": completed,
        },
        termination_states=("completed",),
        purpose="Canonical turn execution workflow with critic and completion gate.",
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
            workflow_id=CHAT_BUTTONIFY_WORKFLOW_ID,
            definition=build_chat_buttonify_workflow(),
            purpose="Buttonify output-transformation pipeline.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
            definition=WorkflowDefinition(
                workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
                initial_state="completed",
                states={
                    "completed": WorkflowStateSpec(state_id="completed", terminal=True)
                },
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
        WorkflowRegistration(
            workflow_id=WRITE_TOOL_POLICY_WORKFLOW_ID,
            definition=build_write_tool_policy_workflow(),
            purpose="Write-tool policy decision pipeline.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
            definition=build_concept_suggestion_preflight_workflow(),
            purpose="Specialised concept-suggestion preflight fallback pipeline.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
            definition=build_kb_mutation_postcondition_critic_workflow(),
            purpose="KB mutation postcondition critic.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=TURN_COMPLETION_GATE_WORKFLOW_ID,
            definition=build_turn_completion_gate_workflow(),
            purpose="Turn completion gate policy workflow.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            definition=build_conversation_turn_execution_workflow(),
            purpose="Conversation turn execution workflow.",
            source="built_in",
        ),
        WorkflowRegistration(
            workflow_id=TOOL_CALLING_WORKFLOW_ID,
            definition=build_tool_calling_workflow(),
            purpose="Standard tool-calling pipeline.",
            source="built_in",
        ),
    ):
        registry.register(registration)
