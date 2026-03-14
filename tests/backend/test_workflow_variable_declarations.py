"""Tests for workflow variable declaration initialisation in the engine.

JVNAUTOSCI-1440: Verifies that ``WorkflowExecutor.run()`` initialises
declared variables from ``definition.metadata["variable_declarations"]``
and that caller-supplied data takes precedence over defaults.
"""

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
)


def _capture_context_definition(
    *,
    variable_declarations: list[dict] | None = None,
) -> tuple[WorkflowDefinition, list[dict]]:
    """Build a single-step workflow that captures the runtime context."""
    captured: list[dict] = []

    def _capture(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured.append(dict(request.data))
        return WorkflowActionResult(outputs={})

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="capture", handler=_capture))

    definition = WorkflowDefinition(
        workflow_id="#V#var_test_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="capture"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
        metadata={"variable_declarations": variable_declarations}
        if variable_declarations
        else {},
    )
    return definition, captured


def test_variable_defaults_are_set_in_context() -> None:
    definition, captured = _capture_context_definition(
        variable_declarations=[
            {"name": "counter", "default_value": 0},
            {"name": "flag", "default_value": False},
        ],
    )
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="capture",
            handler=lambda req: (
                captured.append(dict(req.data)),
                WorkflowActionResult(outputs={}),
            )[1],
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert captured
    ctx = captured[0]
    assert ctx["counter"] == 0
    assert ctx["flag"] is False


def test_caller_data_takes_precedence_over_defaults() -> None:
    definition, captured = _capture_context_definition(
        variable_declarations=[
            {"name": "counter", "default_value": 0},
        ],
    )
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="capture",
            handler=lambda req: (
                captured.append(dict(req.data)),
                WorkflowActionResult(outputs={}),
            )[1],
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={"counter": 99},
    )

    assert result.completed is True
    assert captured
    assert captured[0]["counter"] == 99


def test_variable_declarations_none_metadata_is_safe() -> None:
    """No metadata at all should not crash the engine."""
    definition = WorkflowDefinition(
        workflow_id="#V#no_metadata_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                terminal=True,
            ),
        },
        termination_states=("start",),
    )

    registry = ActionRegistry()
    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True


def test_malformed_variable_declarations_are_skipped() -> None:
    """Non-mapping entries and entries without a name should be silently skipped."""
    captured: list[dict] = []

    def _capture(req: WorkflowActionRequest) -> WorkflowActionResult:
        captured.append(dict(req.data))
        return WorkflowActionResult(outputs={})

    definition = WorkflowDefinition(
        workflow_id="#V#malformed_decl_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="capture"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
        metadata={
            "variable_declarations": [
                "not_a_dict",
                {"name": "", "default_value": "should_skip"},
                {"name": "  ", "default_value": "should_skip"},
                {"name": "valid_var", "default_value": "ok"},
            ]
        },
    )

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="capture", handler=_capture))

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert captured
    ctx = captured[0]
    assert ctx.get("valid_var") == "ok"
    assert "" not in ctx
    assert "  " not in ctx


def test_variable_default_none_is_set_explicitly() -> None:
    """A declaration with default_value=None should still set the key."""
    captured: list[dict] = []

    def _capture(req: WorkflowActionRequest) -> WorkflowActionResult:
        captured.append(dict(req.data))
        return WorkflowActionResult(outputs={})

    definition = WorkflowDefinition(
        workflow_id="#V#none_default_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="capture"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
        metadata={
            "variable_declarations": [
                {"name": "nullable_var", "default_value": None},
            ]
        },
    )

    registry = ActionRegistry()
    registry.register(ActionSpec(action_id="capture", handler=_capture))

    result = WorkflowExecutor(registry=registry, max_transitions=5).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert captured
    assert "nullable_var" in captured[0]
    assert captured[0]["nullable_var"] is None
