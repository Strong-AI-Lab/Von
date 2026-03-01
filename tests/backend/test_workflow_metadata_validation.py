"""Runtime validation tests for workflow metadata (WS7 / JVNAUTOSCI-1092)."""

from __future__ import annotations

from typing import Dict

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
from src.backend.workflows.trace_model import WorkflowExecutionTrace


def _build_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#metadata_validation_workflow",
        initial_state="plan",
        states={
            "plan": WorkflowStateSpec(
                state_id="plan",
                actions=(WorkflowActionInvocation(action_id="task.create"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
                metadata={
                    "preconditions": ["#V#user_authenticated"],
                    "effects": ["#V#task_created"],
                    "reads_variables": ["user_id"],
                    "writes_variables": ["task_id"],
                },
            ),
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )


def test_metadata_validation_positive_path_records_trace_events() -> None:
    definition = _build_definition()
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            outputs={
                "task_id": "task-1",
                "task_created": True,
            }
        )

    registry.register(ActionSpec(action_id="task.create", handler=handler))

    trace = WorkflowExecutionTrace(workflow_id=definition.workflow_id)
    executor = WorkflowExecutor(registry=registry, max_transitions=5)
    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": True,
            "user_id": "user-1",
        },
        trace=trace,
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert result.error is None
    assert result.data.get("task_id") == "task-1"
    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 2
    assert events[0]["phase"] == "pre_action"
    assert events[1]["phase"] == "post_action"
    assert events[0]["ok"] is True
    assert events[1]["ok"] is True
    trace_verdicts = [
        transition.get("verdict")
        for transition in trace.state_transitions
        if isinstance(transition.get("verdict"), dict)
        and transition["verdict"].get("status") == "metadata_validation"
    ]
    assert len(trace_verdicts) == 2


def test_metadata_validation_blocks_unsatisfied_precondition() -> None:
    definition = _build_definition()
    registry = ActionRegistry()
    calls: Dict[str, int] = {"count": 0}

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"task_id": "task-1", "task_created": True})

    registry.register(ActionSpec(action_id="task.create", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": False,
            "user_id": "user-1",
        },
    )

    assert result.completed is False
    assert result.final_state == "plan"
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_precondition_unsatisfied:plan:"
    )
    assert calls["count"] == 0


def test_metadata_validation_blocks_missing_write_variable() -> None:
    definition = _build_definition()
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        # Deliberately omit task_id to trigger writes_variables validation failure.
        return WorkflowActionResult(outputs={"task_created": True})

    registry.register(ActionSpec(action_id="task.create", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": True,
            "user_id": "user-1",
        },
    )

    assert result.completed is False
    assert result.final_state == "plan"
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_write_variable_missing:plan:"
    )


def test_metadata_validation_materialises_terminal_effect_evidence() -> None:
    workflow_id = "#V#terminal_effect_validation"
    terminal_effect_id = "#V#workflow_effect_terminal_effect_validation_done_terminal"
    definition = WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
                metadata={"effects": [terminal_effect_id]},
            )
        },
    )
    executor = WorkflowExecutor(registry=ActionRegistry(), max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.error is None
    assert result.data.get(terminal_effect_id) is True
    assert result.data.get("workflow_effect_terminal_effect_validation_done_terminal") is True

    events = result.data.get("workflow_terminal_effect_events")
    assert isinstance(events, list)
    assert events
    assert events[-1].get("status") == "terminal_effect_materialised"
    assert events[-1].get("symbol") == terminal_effect_id


def test_metadata_validation_terminal_effect_materialisation_is_not_over_broad() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#terminal_effect_validation_non_terminal_symbol",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
                metadata={"effects": ["#V#workflow_effect_missing_terminal_suffix"]},
            )
        },
    )
    executor = WorkflowExecutor(registry=ActionRegistry(), max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_effect_unsatisfied:done:"
    )


def test_metadata_validation_warn_mode_records_failure_without_blocking(
    monkeypatch,
) -> None:
    definition = _build_definition()
    registry = ActionRegistry()
    calls: Dict[str, int] = {"count": 0}

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"task_id": "task-1", "task_created": True})

    registry.register(ActionSpec(action_id="task.create", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)
    monkeypatch.setenv("VON_WORKFLOW_METADATA_VALIDATION_MODE", "warn")

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": False,
            "user_id": "user-1",
        },
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert result.error is None
    assert calls["count"] == 1

    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 2
    pre_action = events[0]
    assert pre_action["phase"] == "pre_action"
    assert pre_action["ok"] is False
    assert pre_action["mode"] == "warn"
    assert pre_action["enforced"] is False
    assert pre_action["reason_code"] == "metadata_precondition_unsatisfied"


def test_metadata_validation_off_mode_skips_checks(monkeypatch) -> None:
    definition = _build_definition()
    registry = ActionRegistry()
    calls: Dict[str, int] = {"count": 0}

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"task_id": "task-1", "task_created": True})

    registry.register(ActionSpec(action_id="task.create", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)
    monkeypatch.setenv("VON_WORKFLOW_METADATA_VALIDATION_MODE", "off")

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "user_authenticated": False,
        },
    )

    assert result.completed is True
    assert result.final_state == "done"
    assert result.error is None
    assert calls["count"] == 1

    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 2
    assert all(event.get("mode") == "off" for event in events)
    assert all(event.get("enforced") is False for event in events)
    assert all(event.get("skipped") is True for event in events)
    assert all(
        event.get("skip_reason") == "disabled_by_rollout_mode" for event in events
    )


def test_engine_resolves_dynamic_action_inputs_from_context() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#dynamic_input_resolution_workflow",
        initial_state="resolve",
        states={
            "resolve": WorkflowStateSpec(
                state_id="resolve",
                actions=(
                    WorkflowActionInvocation(
                        action_id="tool.fetch",
                        inputs={
                            "concept_id": {"$context_key": "target_type_id"},
                            "fixed": "literal",
                        },
                    ),
                ),
                terminal=True,
                metadata={"reads_context_keys": ["target_type_id"]},
            )
        },
    )
    registry = ActionRegistry()

    def handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            outputs={
                "resolved_concept_id": request.inputs.get("concept_id"),
                "fixed": request.inputs.get("fixed"),
            }
        )

    registry.register(ActionSpec(action_id="tool.fetch", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"target_type_id": "#V#person"},
    )

    assert result.completed is True
    assert result.error is None
    assert result.data["resolved_concept_id"] == "#V#person"
    assert result.data["fixed"] == "literal"


def test_metadata_validation_writes_context_keys_accepts_symbol_aliases() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#writes_context_key_validation_workflow",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="tool.write"),),
                terminal=True,
                metadata={
                    "writes_context_keys": ["#V#workflow_context_key_validated_type_id"]
                },
            )
        },
    )
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"validated_type_id": "#V#person"})

    registry.register(ActionSpec(action_id="tool.write", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.error is None


def test_output_context_mapping_applies_before_post_action_validation() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#writes_context_key_mapping_positive",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="tool.write"),),
                terminal=True,
                metadata={
                    "writes_context_keys": ["#V#workflow_context_key_validated_type_id"],
                    "tool_output_context_mappings": [
                        {
                            "mapping_concept_id": "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id",
                            "tool_output_field": "concept_id",
                            "context_key": "validated_type_id",
                        }
                    ],
                },
            )
        },
    )
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"result": {"concept_id": "#V#person"}})

    registry.register(ActionSpec(action_id="tool.write", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.error is None
    assert result.data.get("validated_type_id") == "#V#person"
    events = result.data.get("workflow_tool_output_mapping_events")
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0].get("mapping_concept_id") == (
        "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id"
    )
    assert events[0].get("tool_output_field") == "concept_id"
    assert events[0].get("context_key") == "validated_type_id"
    assert events[0].get("value_present") is True


def test_metadata_validation_blocks_missing_writes_context_key() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#writes_context_key_validation_negative",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="tool.write"),),
                terminal=True,
                metadata={
                    "writes_context_keys": [
                        "#V#workflow_context_key_validated_type_name"
                    ]
                },
            )
        },
    )
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"validated_type_id": "#V#person"})

    registry.register(ActionSpec(action_id="tool.write", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_write_context_key_missing:write:"
    )


def test_workflow_executor_routes_on_unknown_transition() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#unknown_outcome_routing_workflow",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="escalate",
                        condition=lambda ctx: bool(ctx.get("last_action_unknown")),
                        reason="on_unknown",
                    ),
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
            ),
            "escalate": WorkflowStateSpec(state_id="escalate", terminal=True),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
    )
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="unknown",
            error="classification_inconclusive",
            outputs={"probe_summary": "insufficient evidence"},
        )

    registry.register(ActionSpec(action_id="probe.action", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.error is None
    assert result.final_state == "escalate"
    assert result.data.get("last_action_unknown") is True
    assert result.data.get("probe_summary") == "insufficient evidence"


def test_workflow_executor_rejects_unknown_without_route() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#unknown_outcome_without_route",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
    )
    registry = ActionRegistry()

    def handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="unknown")

    registry.register(ActionSpec(action_id="probe.action", handler=handler))
    executor = WorkflowExecutor(registry=registry, max_transitions=5)

    result = executor.run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.final_state == "probe"
    assert result.error == "action_unknown"
