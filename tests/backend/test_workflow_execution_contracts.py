from __future__ import annotations

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
)


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
