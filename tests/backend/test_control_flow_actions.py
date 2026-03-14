from __future__ import annotations

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import register_control_flow_actions
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
    WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)


def _child_definition(workflow_id: str, action_id: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=action_id),),
                terminal=True,
            )
        },
        termination_states=("start",),
    )


def test_break_action_emits_control_signal() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    context: dict[str, object] = {}
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_BREAK_ID,
        inputs={"loop_scope_id": "main"},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("control_signal") == "break"
    assert result.outputs.get("control_scope") == "main"


def test_fork_join_actions_execute_and_merge_branch_results() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit_one",
            handler=lambda _request: WorkflowActionResult(outputs={"value_one": 1}),
        )
    )
    registry.register(
        ActionSpec(
            action_id="child.emit_two",
            handler=lambda _request: WorkflowActionResult(outputs={"value_two": 2}),
        )
    )
    definitions = {
        "#V#child_one": _child_definition("#V#child_one", "child.emit_one"),
        "#V#child_two": _child_definition("#V#child_two", "child.emit_two"),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    context: dict[str, object] = {}
    fork_result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FORK_ID,
        inputs={
            "fork_id": "main_fork",
            "failure_policy": "collect_errors",
            "branches": [
                {"branch_id": "a", "workflow_id": "#V#child_one"},
                {"branch_id": "b", "workflow_id": "#V#child_two"},
            ],
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert fork_result.status == "success"
    assert fork_result.outputs.get("fork_success_count") == 2
    join_result = registry.execute(
        WORKFLOW_CONTROL_ACTION_JOIN_ID,
        inputs={"fork_id": "main_fork"},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert join_result.status == "success"
    assert join_result.outputs.get("fork_joined") is True
    merged = join_result.outputs.get("result")
    assert merged == {"value_one": 1, "value_two": 2}


def test_join_action_fails_without_matching_fork() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_JOIN_ID,
        inputs={"fork_id": "missing"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "join_without_matching_fork" in str(result.error or "")


def test_for_each_action_executes_child_workflow_per_item() -> None:
    registry = ActionRegistry()

    def _emit_item(request):
        return WorkflowActionResult(
            outputs={
                "item_value": request.data.get("current_item"),
                "item_index": request.data.get("index"),
            }
        )

    registry.register(ActionSpec(action_id="child.emit_item", handler=_emit_item))
    definitions = {
        "#V#child_each": _child_definition("#V#child_each", "child.emit_item"),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    context: dict[str, object] = {"candidate_items": ["A", "B", "C"]}
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={
            "workflow_id": "#V#child_each",
            "items_context_key": "candidate_items",
            "max_items": 2,
            "success_policy": "all_must_succeed",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("for_each_item_count") == 2
    assert result.outputs.get("for_each_success_count") == 2
    assert result.outputs.get("for_each_error_count") == 0
    results = result.outputs.get("iteration_results")
    assert isinstance(results, list)
    assert results[0]["result"] == {"item_value": "A", "item_index": 0}
    assert results[1]["result"] == {"item_value": "B", "item_index": 1}


def test_for_each_action_respects_partial_success_policy() -> None:
    registry = ActionRegistry()

    def _conditionally_fail(request):
        if request.data.get("current_item") == "bad":
            return WorkflowActionResult(status="failed", error="child_failed")
        return WorkflowActionResult(outputs={"item_value": request.data.get("current_item")})

    registry.register(
        ActionSpec(action_id="child.maybe_fail", handler=_conditionally_fail)
    )
    definitions = {
        "#V#child_each_partial": _child_definition(
            "#V#child_each_partial",
            "child.maybe_fail",
        ),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={
            "workflow_id": "#V#child_each_partial",
            "items": ["good", "bad"],
            "success_policy": "allow_partial",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("for_each_success_count") == 1
    assert result.outputs.get("for_each_error_count") == 1
    assert result.outputs.get("for_each_partial_success") is True


# ---------------------------------------------------------------------------
# context.set action tests (JVNAUTOSCI-1440)
# ---------------------------------------------------------------------------


def _build_context_set_registry() -> ActionRegistry:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _wid: None)
    return registry


def test_context_set_literal_values() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {"key": "flag_a", "value": True},
                {"key": "counter", "value": 42},
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["flag_a"] is True
    assert result.outputs["counter"] == 42
    assert result.outputs["_context_set_applied_keys"] == ["flag_a", "counter"]


def test_context_set_value_from_context() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {"key": "copied_val", "value_from_context": "source_key"},
            ]
        },
        context={"source_key": "hello"},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["copied_val"] == "hello"


def test_context_set_value_from_context_missing_source() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {"key": "dest", "value_from_context": "nonexistent"},
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["dest"] is None


def test_context_set_fails_when_assignments_missing() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "assignments_missing" in (result.error or "")


def test_context_set_fails_on_missing_key() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={"assignments": [{"value": "no_key"}]},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "missing_key" in (result.error or "")


def test_context_set_fails_on_non_mapping_assignment() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={"assignments": ["not_a_dict"]},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "not_mapping" in (result.error or "")
