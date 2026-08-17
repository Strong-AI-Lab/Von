from __future__ import annotations

import pytest

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
    resolve_action_inputs_from_context,
)
from src.backend.workflows.execution_contracts import (
    workflow_final_state_is_failure_like,
)


@pytest.mark.parametrize(
    ("final_state", "expected"),
    [
        ("failed_unmarked", True),
        ("failed_gmail_state", True),
        ("#V#workflow_step_email_ingestion_failed_unmarked", True),
        ("done_with_retryable_failures", False),
        ("completed_with_errors", False),
    ],
)
def test_workflow_failure_like_terminal_states_are_token_bounded(
    final_state: str, expected: bool
) -> None:
    assert workflow_final_state_is_failure_like(final_state) is expected


def test_static_context_bindings_resolve_shared_dotted_paths() -> None:
    resolved = resolve_action_inputs_from_context(
        action_inputs={
            "scenario_id": {"$context_key": "scenario_contract.scenario_id"},
            "verdict": {
                "$context_key": "represented_operational_evaluator_draft.verdict"
            },
            "first_evidence": {
                "$context_key": (
                    "represented_operational_evaluator_draft.evidence.0.kind"
                )
            },
        },
        context={
            "scenario_contract": {"scenario_id": "scenario-a"},
            "represented_operational_evaluator_draft": {
                "verdict": "pass",
                "evidence": [{"kind": "trace_readback"}],
            },
        },
    )

    assert resolved == {
        "scenario_id": "scenario-a",
        "verdict": "pass",
        "first_evidence": "trace_readback",
    }


def test_static_context_bindings_preserve_literal_dotted_key_precedence() -> None:
    resolved = resolve_action_inputs_from_context(
        action_inputs={"value": {"$context_key": "receipt.status"}},
        context={
            "facts": {
                "receipt.status": "legacy-literal",
                "receipt": {"status": "nested-path"},
            }
        },
    )

    assert resolved == {"value": "legacy-literal"}


def test_workflow_executor_emits_step_and_workflow_result_envelopes() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#return_signal_workflow",
        initial_state="compute",
        states={
            "compute": WorkflowStateSpec(
                state_id="compute",
                actions=(WorkflowActionInvocation(action_id="emit.return"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="next",
                        reason="next_step",
                        condition=lambda _ctx: True,
                    ),
                ),
            ),
            "next": WorkflowStateSpec(state_id="next", terminal=True),
        },
    )

    registry = ActionRegistry()

    def _emit_return(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={
                "control_signal": "return",
                "return_payload": {"answer": 42},
                "intermediate": "ok",
            },
        )

    registry.register(ActionSpec(action_id="emit.return", handler=_emit_return))

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.final_state == "compute"
    assert result.result_envelope is not None
    assert result.result_envelope.get("control_signal") == "return"
    assert result.result_envelope.get("terminal_status") == "completed"
    assert result.result_envelope.get("declared_output_payload") == {"answer": 42}
    envelopes = result.data.get("workflow_step_result_envelopes")
    assert isinstance(envelopes, list)
    assert len(envelopes) == 1
    assert envelopes[0].get("control_signal") == "return"
    assert envelopes[0].get("state_id") == "compute"
    assert isinstance(envelopes[0].get("mutation_summary"), dict)


def test_workflow_executor_fails_break_without_explicit_on_break_route() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#break_without_route",
        initial_state="loop_step",
        states={
            "loop_step": WorkflowStateSpec(
                state_id="loop_step",
                actions=(WorkflowActionInvocation(action_id="emit.break"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="fallback",
                        reason="next_step",
                        condition=lambda _ctx: True,
                    ),
                ),
            ),
            "fallback": WorkflowStateSpec(state_id="fallback", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="emit.break",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={"control_signal": "break", "control_scope": "main_loop"},
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error == "workflow_break_outside_loop_scope"
    assert result.result_envelope is not None
    assert result.result_envelope.get("terminal_status") == "failed"


def test_workflow_executor_does_not_complete_explicit_failed_concept_state() -> None:
    failed_state = "#V#workflow_step_example_workflow_failed"
    definition = WorkflowDefinition(
        workflow_id="#V#example_workflow",
        initial_state=failed_state,
        states={
            failed_state: WorkflowStateSpec(state_id=failed_state, terminal=True),
        },
        termination_states=(failed_state,),
    )

    result = WorkflowExecutor(registry=ActionRegistry(), max_transitions=3).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.final_state == failed_state
    assert result.error == "workflow_failed_terminal_state"
    assert result.result_envelope is not None
    assert result.result_envelope["terminal_status"] == "failed"


def test_workflow_executor_uses_authored_failed_terminal_error_code() -> None:
    failed_state = "#V#workflow_step_example_workflow_failed"
    definition = WorkflowDefinition(
        workflow_id="#V#example_workflow",
        initial_state=failed_state,
        states={
            failed_state: WorkflowStateSpec(state_id=failed_state, terminal=True),
        },
        termination_states=(failed_state,),
        metadata={
            "terminal_success_contract": {
                "failed_terminal_error_code": "example_workflow_no_items_succeeded"
            }
        },
    )

    result = WorkflowExecutor(registry=ActionRegistry(), max_transitions=3).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error == "example_workflow_no_items_succeeded"
    assert result.result_envelope is not None
    assert result.result_envelope["diagnostics"]["error"] == (
        "example_workflow_no_items_succeeded"
    )


def test_workflow_executor_ignores_malformed_failed_terminal_error_code() -> None:
    failed_state = "#V#workflow_step_example_workflow_failed"
    definition = WorkflowDefinition(
        workflow_id="#V#example_workflow",
        initial_state=failed_state,
        states={
            failed_state: WorkflowStateSpec(state_id=failed_state, terminal=True),
        },
        termination_states=(failed_state,),
        metadata={
            "terminal_success_contract": {
                "failed_terminal_error_code": {"not": "a string"}
            }
        },
    )

    result = WorkflowExecutor(registry=ActionRegistry(), max_transitions=3).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error == "workflow_failed_terminal_state"


def test_failed_terminal_state_takes_precedence_over_return_signal() -> None:
    failed_state = "#V#workflow_step_returning_workflow_failed"
    definition = WorkflowDefinition(
        workflow_id="#V#returning_workflow",
        initial_state=failed_state,
        states={
            failed_state: WorkflowStateSpec(
                state_id=failed_state,
                terminal=True,
                actions=(WorkflowActionInvocation(action_id="emit.return"),),
            )
        },
        termination_states=(failed_state,),
    )
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="emit.return",
            handler=lambda _request: WorkflowActionResult(
                outputs={"control_signal": "return", "return_payload": {"ok": True}}
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=3).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.final_state == failed_state
    assert result.error == "workflow_failed_terminal_state"


def test_step_result_envelope_snapshots_are_detached_from_live_context() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#snapshot_detach_workflow",
        initial_state="compute",
        states={
            "compute": WorkflowStateSpec(
                state_id="compute",
                actions=(WorkflowActionInvocation(action_id="emit.payload"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="next_step",
                        condition=lambda _ctx: True,
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="emit.payload",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={"result": {"answer": 42}},
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    envelopes = result.data.get("workflow_step_result_envelopes")
    assert isinstance(envelopes, list)
    assert len(envelopes) == 1
    last_envelope = result.data.get("last_workflow_step_result_envelope")
    assert isinstance(last_envelope, dict)

    result.data["result"]["answer"] = 99
    assert envelopes[0]["output_payload"]["result"]["answer"] == 42
    assert last_envelope["output_payload"]["result"]["answer"] == 42

    envelopes[0]["output_payload"]["result"]["answer"] = 7
    assert last_envelope["output_payload"]["result"]["answer"] == 42


def test_step_result_envelope_preserves_failed_action_outputs() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#failure_snapshot_workflow",
        initial_state="compute",
        states={
            "compute": WorkflowStateSpec(
                state_id="compute",
                actions=(WorkflowActionInvocation(action_id="emit.failure"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        reason="on_failure",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="emit.failure",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="boom",
                outputs={
                    "cache_state": "markdown_only_partial_cache",
                    "partial_cache_without_pdf": True,
                },
            ),
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    envelopes = result.data.get("workflow_step_result_envelopes")
    assert isinstance(envelopes, list)
    assert len(envelopes) == 1
    assert envelopes[0]["action_outcome"] == "failure"
    assert (
        envelopes[0]["output_payload"]["cache_state"] == "markdown_only_partial_cache"
    )
    assert envelopes[0]["output_payload"]["partial_cache_without_pdf"] is True
    assert "cache_state" not in result.data
    assert (
        result.data["last_action_outputs"]["cache_state"]
        == "markdown_only_partial_cache"
    )
    assert (
        result.data["last_failed_action_outputs"]["partial_cache_without_pdf"] is True
    )
