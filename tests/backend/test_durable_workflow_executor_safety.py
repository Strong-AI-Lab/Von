"""Regression tests for durable action-execution safety envelope (WS2).

JVNAUTOSCI-1087: Durable execution must honour explicit `on_failure`
transitions instead of always failing fast on action errors.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from src.backend.workflows.durable.durable_executor import DurableWorkflowExecutor
from src.backend.workflows.durable.models import WorkflowInstance, WorkflowInstanceStatus
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)


def _build_instance(workflow_id: str) -> WorkflowInstance:
    return WorkflowInstance(
        instance_id="instance-1",
        workflow_id=workflow_id,
        user_id="#V#test_user",
        org_id="#V#test_org",
        namespace="#V#test_user/#V#test_org",
        status=WorkflowInstanceStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        inputs={},
    )


def test_durable_executor_routes_to_on_failure_recovery() -> None:
    """Failed actions should transition via `on_failure` when defined."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_failure_recovery",
        initial_state="risky",
        states={
            "risky": WorkflowStateSpec(
                state_id="risky",
                actions=(WorkflowActionInvocation(action_id="risky.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="recover",
                        reason="on_failure",
                        condition=lambda ctx: bool(ctx.get("last_action_failed")),
                    ),
                ),
            ),
            "recover": WorkflowStateSpec(
                state_id="recover",
                actions=(WorkflowActionInvocation(action_id="recover.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def fail_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="failed", error="boom")

    def recover_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"recovered": True})

    registry.register(ActionSpec(action_id="risky.action", handler=fail_handler))
    registry.register(ActionSpec(action_id="recover.action", handler=recover_handler))

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "recover"
    assert result.data.get("recovered") is True

    checkpoint_errors = [
        call.kwargs.get("error")
        for call in manager.checkpoint.call_args_list
        if isinstance(call.kwargs, dict)
    ]
    assert "boom" not in checkpoint_errors


def test_durable_executor_still_fails_without_on_failure_route() -> None:
    """Without an explicit failure route, durable execution should fail-fast."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_fail_fast",
        initial_state="risky",
        states={
            "risky": WorkflowStateSpec(
                state_id="risky",
                actions=(WorkflowActionInvocation(action_id="risky.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def fail_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="failed", error="boom")

    registry.register(ActionSpec(action_id="risky.action", handler=fail_handler))

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.final_state == "risky"
    assert result.error == "boom"
    assert any(
        isinstance(call.kwargs, dict) and call.kwargs.get("error") == "boom"
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_blocks_unsatisfied_metadata_precondition() -> None:
    """Metadata precondition failures should block execution with a reason code."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_metadata_validation",
        initial_state="guarded",
        states={
            "guarded": WorkflowStateSpec(
                state_id="guarded",
                actions=(WorkflowActionInvocation(action_id="guarded.action"),),
                terminal=True,
                metadata={"preconditions": ["#V#user_authenticated"]},
            ),
        },
    )

    registry = ActionRegistry()
    calls = {"count": 0}

    def guarded_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"ok": True})

    registry.register(ActionSpec(action_id="guarded.action", handler=guarded_handler))

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"user_authenticated": False}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is False
    assert result.final_state == "guarded"
    assert result.error is not None
    assert result.error.startswith(
        "metadata_validation_failed:metadata_precondition_unsatisfied:guarded:"
    )
    assert calls["count"] == 0
    assert any(
        isinstance(call.kwargs, dict) and call.kwargs.get("error") == result.error
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_warn_mode_records_metadata_failure_without_blocking(
    monkeypatch,
) -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_metadata_warn",
        initial_state="guarded",
        states={
            "guarded": WorkflowStateSpec(
                state_id="guarded",
                actions=(WorkflowActionInvocation(action_id="guarded.action"),),
                terminal=True,
                metadata={"preconditions": ["#V#user_authenticated"]},
            ),
        },
    )

    registry = ActionRegistry()
    calls = {"count": 0}

    def guarded_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"ok": True})

    registry.register(ActionSpec(action_id="guarded.action", handler=guarded_handler))

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"user_authenticated": False}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    monkeypatch.setenv("VON_WORKFLOW_METADATA_VALIDATION_MODE", "warn")

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "guarded"
    assert result.error is None
    assert calls["count"] == 1
    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 1
    pre_action = events[0]
    assert pre_action.get("phase") == "pre_action"
    assert pre_action.get("ok") is False
    assert pre_action.get("mode") == "warn"
    assert pre_action.get("enforced") is False
    assert pre_action.get("reason_code") == "metadata_precondition_unsatisfied"


def test_durable_executor_off_mode_skips_metadata_checks(monkeypatch) -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_metadata_off",
        initial_state="guarded",
        states={
            "guarded": WorkflowStateSpec(
                state_id="guarded",
                actions=(WorkflowActionInvocation(action_id="guarded.action"),),
                terminal=True,
                metadata={"preconditions": ["#V#user_authenticated"]},
            ),
        },
    )

    registry = ActionRegistry()
    calls = {"count": 0}

    def guarded_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        calls["count"] += 1
        return WorkflowActionResult(outputs={"ok": True})

    registry.register(ActionSpec(action_id="guarded.action", handler=guarded_handler))

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"user_authenticated": False}

    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)
    monkeypatch.setenv("VON_WORKFLOW_METADATA_VALIDATION_MODE", "off")

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.final_state == "guarded"
    assert result.error is None
    assert calls["count"] == 1
    events = result.data.get("workflow_metadata_validation_events")
    assert isinstance(events, list)
    assert len(events) == 1
    assert all(event.get("mode") == "off" for event in events)
    assert all(event.get("enforced") is False for event in events)
    assert all(event.get("skipped") is True for event in events)
    assert all(
        event.get("skip_reason") == "disabled_by_rollout_mode" for event in events
    )

