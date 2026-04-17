"""Regression tests for durable action-execution safety envelope (WS2).

JVNAUTOSCI-1087: Durable execution must honour explicit `on_failure`
transitions instead of always failing fast on action errors.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from pymongo.errors import PyMongoError

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


def test_durable_executor_persists_trace_and_checkpoints_trace_link() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_trace_persistence",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=ActionRegistry(), instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.workflows.durable.durable_executor.insert_workflow_execution_trace",
            return_value="trace-1550",
        ) as insert_trace,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert result.execution_trace_id == "trace-1550"
    insert_trace.assert_called_once()
    stored_doc = insert_trace.call_args.args[0]
    assert stored_doc["instance_id"] == "instance-1"
    assert stored_doc["metadata"]["default_model"] == "test-model"
    assert any(
        isinstance(call.kwargs, dict)
        and call.kwargs.get("execution_trace_id") == "trace-1550"
        for call in manager.checkpoint.call_args_list
    )


def test_durable_executor_retries_transient_cancel_check() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_transient_cancel_retry",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
            ),
        },
    )

    manager = MagicMock()
    manager.get_instance.return_value = _build_instance(definition.workflow_id)
    manager.is_cancelled.side_effect = [
        PyMongoError("server selection timeout while reading workflow_instances"),
        False,
    ]
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=ActionRegistry(), instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=MagicMock(),
        ),
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
            return_value="test-model",
        ),
        patch(
            "src.backend.db.transient_errors.attempt_reconnect",
            return_value={"reconnected": True},
        ),
        patch(
            "src.backend.db.transient_errors.time.sleep",
            return_value=None,
        ),
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert manager.is_cancelled.call_count == 2


def test_durable_executor_uses_scoped_active_model_and_context_defaults() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_scoped_model",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="capture.action"),),
                terminal=True,
            ),
        },
    )

    captured: dict[str, object] = {}
    registry = ActionRegistry()

    def capture_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured["model"] = request.environment.model
        captured["user_concept_id"] = request.data.get("user_concept_id")
        captured["org_concept_id"] = request.data.get("org_concept_id")
        captured["namespace"] = request.data.get("namespace")
        captured["user_namespace"] = request.data.get("user_namespace")
        return WorkflowActionResult(outputs={"captured": True})

    registry.register(ActionSpec(action_id="capture.action", handler=capture_handler))

    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
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
            return_value="scoped-model",
        ) as get_active_model_name,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert captured == {
        "model": "scoped-model",
        "user_concept_id": instance.user_id,
        "org_concept_id": instance.org_id,
        "namespace": instance.namespace,
        "user_namespace": instance.namespace,
    }
    get_active_model_name.assert_called_once_with(
        user_concept_id=instance.user_id,
        org_concept_id=instance.org_id,
    )


def test_durable_executor_prefers_requested_model_override_from_instance_inputs() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_requested_model_override",
        initial_state="capture",
        states={
            "capture": WorkflowStateSpec(
                state_id="capture",
                actions=(WorkflowActionInvocation(action_id="capture.action"),),
                terminal=True,
            ),
        },
    )

    captured: dict[str, object] = {}
    requested_client = MagicMock(name="requested_client")
    registry = ActionRegistry()

    def capture_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        captured["model"] = request.environment.model
        captured["requested_model"] = request.data.get("requested_model")
        captured["requested_client_type"] = request.data.get("requested_client_type")
        return WorkflowActionResult(outputs={"captured": True})

    registry.register(ActionSpec(action_id="capture.action", handler=capture_handler))

    manager = MagicMock()
    instance = _build_instance(definition.workflow_id)
    instance.inputs = {
        "requested_model": "gemma4:26b",
        "requested_client_type": "ollama",
    }
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(registry=registry, instance_manager=manager)

    with (
        patch(
            "src.backend.languagemodels.llm_interface.get_llm_client",
            return_value=requested_client,
        ) as get_llm_client,
        patch(
            "src.backend.languagemodels.llm_interface.get_active_model_name",
        ) as get_active_model_name,
    ):
        result = executor.run_durable(
            "instance-1",
            definition,
            resume_from_checkpoint=False,
        )

    assert result.completed is True
    assert captured == {
        "model": "gemma4:26b",
        "requested_model": "gemma4:26b",
        "requested_client_type": "ollama",
    }
    get_llm_client.assert_called_once_with(
        client_type="ollama",
        user_concept_id=instance.user_id,
        org_concept_id=instance.org_id,
    )
    get_active_model_name.assert_not_called()


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


def test_durable_executor_preserves_failed_action_outputs_in_step_envelopes() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_failure_snapshot",
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
            "recover": WorkflowStateSpec(state_id="recover", terminal=True),
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="risky.action",
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
    envelopes = result.data.get("workflow_step_result_envelopes")
    assert isinstance(envelopes, list)
    assert len(envelopes) == 1
    assert envelopes[0]["action_outcome"] == "failure"
    assert envelopes[0]["output_payload"]["cache_state"] == "markdown_only_partial_cache"
    assert envelopes[0]["output_payload"]["partial_cache_without_pdf"] is True
    assert "cache_state" not in result.data


def test_durable_executor_routes_to_on_unknown_recovery() -> None:
    """Unknown outcomes should route via explicit `on_unknown` transitions."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_unknown_recovery",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="escalate",
                        reason="on_unknown",
                        condition=lambda ctx: bool(ctx.get("last_action_unknown")),
                    ),
                ),
            ),
            "escalate": WorkflowStateSpec(
                state_id="escalate",
                actions=(WorkflowActionInvocation(action_id="recover.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def unknown_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="unknown",
            error="insufficient_confidence",
            outputs={"probe_summary": "need escalation"},
        )

    def recover_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"escalated": True})

    registry.register(ActionSpec(action_id="probe.action", handler=unknown_handler))
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
    assert result.final_state == "escalate"
    assert result.data.get("last_action_unknown") is False
    assert result.data.get("probe_summary") == "need escalation"
    assert result.data.get("escalated") is True
    checkpoint_errors = [
        call.kwargs.get("error")
        for call in manager.checkpoint.call_args_list
        if isinstance(call.kwargs, dict)
    ]
    assert "action_unknown" not in checkpoint_errors


def test_durable_executor_fails_unknown_without_on_unknown_route() -> None:
    """Without on_unknown route, unknown outcomes should fail closed."""
    definition = WorkflowDefinition(
        workflow_id="#V#durable_unknown_fail_closed",
        initial_state="probe",
        states={
            "probe": WorkflowStateSpec(
                state_id="probe",
                actions=(WorkflowActionInvocation(action_id="probe.action"),),
                terminal=True,
            ),
        },
    )

    registry = ActionRegistry()

    def unknown_handler(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(status="unknown")

    registry.register(ActionSpec(action_id="probe.action", handler=unknown_handler))

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
    assert result.final_state == "probe"
    assert result.error == "action_unknown"
    assert any(
        isinstance(call.kwargs, dict) and call.kwargs.get("error") == "action_unknown"
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


def test_durable_executor_materialises_terminal_effect_evidence() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_terminal_effect_validation",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                terminal=True,
                metadata={
                    "effects": [
                        (
                            "#V#workflow_effect_"
                            "durable_terminal_effect_validation_done_terminal"
                        )
                    ]
                },
            ),
        },
    )

    instance = _build_instance(definition.workflow_id)
    manager = MagicMock()
    manager.get_instance.return_value = instance
    manager.is_cancelled.return_value = False
    manager.extend_lock.return_value = True
    manager.checkpoint.return_value = True

    executor = DurableWorkflowExecutor(
        registry=ActionRegistry(),
        instance_manager=manager,
    )

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

    terminal_effect_id = (
        "#V#workflow_effect_durable_terminal_effect_validation_done_terminal"
    )
    assert result.completed is True
    assert result.error is None
    assert result.data.get(terminal_effect_id) is True
    assert (
        result.data.get(
            "workflow_effect_durable_terminal_effect_validation_done_terminal"
        )
        is True
    )
    events = result.data.get("workflow_terminal_effect_events")
    assert isinstance(events, list)
    assert events
    assert events[-1].get("status") == "terminal_effect_materialised"
    assert events[-1].get("symbol") == terminal_effect_id


def test_durable_executor_applies_output_mapping_before_metadata_validation() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_output_mapping_validation",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="tool.write"),),
                terminal=True,
                metadata={
                    "writes_context_keys": [
                        "#V#workflow_context_key_validated_type_id",
                    ],
                    "tool_output_context_mappings": [
                        {
                            "mapping_concept_id": "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id",
                            "tool_output_field": "concept_id",
                            "context_key": "validated_type_id",
                        }
                    ],
                },
            ),
        },
    )

    registry = ActionRegistry()

    def write_handler(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(outputs={"result": {"concept_id": "#V#person"}})

    registry.register(ActionSpec(action_id="tool.write", handler=write_handler))

    instance = _build_instance(definition.workflow_id)
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

    assert result.completed is True
    assert result.error is None
    assert result.data.get("validated_type_id") == "#V#person"
    events = result.data.get("workflow_tool_output_mapping_events")
    assert isinstance(events, list)
    assert len(events) == 1
    assert events[0].get("tool_output_field") == "concept_id"
    assert events[0].get("context_key") == "validated_type_id"
    assert events[0].get("value_present") is True


def test_durable_executor_populates_result_envelope_for_return_signal() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_return_signal",
        initial_state="compute",
        states={
            "compute": WorkflowStateSpec(
                state_id="compute",
                actions=(WorkflowActionInvocation(action_id="emit.return"),),
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
            action_id="emit.return",
            handler=lambda _request: WorkflowActionResult(
                outputs={
                    "control_signal": "return",
                    "return_payload": {"answer": "done"},
                }
            ),
        )
    )

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
    assert result.final_state == "compute"
    assert isinstance(result.result_envelope, dict)
    assert result.result_envelope.get("control_signal") == "return"
    assert result.result_envelope.get("declared_output_payload") == {"answer": "done"}


def test_durable_executor_records_plan_state_checkpoint_progress() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_plan_state_progress",
        initial_state="dispatch",
        states={
            "dispatch": WorkflowStateSpec(
                state_id="dispatch",
                actions=(WorkflowActionInvocation(action_id="dispatch.action"),),
                terminal=True,
                metadata={
                    "checkpoint_policy": {
                        "schema_version": "workflow_step_checkpoint_policy.v1",
                        "plan_item_updates": [
                            {"item_id": "dispatch", "status": "done"}
                        ],
                        "summary_context_keys": ["dispatch.completed"],
                        "cursor_context_keys": ["page_cursor"],
                        "progress_message": "Dispatching",
                        "force_summary": True,
                    }
                },
            ),
        },
        termination_states=("dispatch",),
        metadata={
            "plan_state_policy": {
                "schema_version": "workflow_plan_state_policy.v1",
                "plan_items": ["dispatch"],
                "summary_interval_steps": 1,
            },
            "completion_gate": {
                "schema_version": "workflow_completion_gate.v1",
                "required_done_plan_items": ["dispatch"],
                "required_context_keys": ["dispatch.completed"],
            },
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="dispatch.action",
            handler=lambda _request: WorkflowActionResult(
                outputs={
                    "dispatch": {"completed": True},
                    "page_cursor": "cursor-2",
                }
            ),
        )
    )

    instance = _build_instance(definition.workflow_id)
    instance.inputs = {"page_cursor": "cursor-1"}

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

    assert result.completed is True
    plan_state = result.data.get("workflow_plan_state")
    assert isinstance(plan_state, dict)
    assert plan_state["items"]["dispatch"]["status"] == "done"
    gate = result.data.get("workflow_completion_gate")
    assert isinstance(gate, dict)
    assert gate["safe_to_claim_completion"] is True
    assert manager.checkpoint.call_args_list
    assert any(
        call.kwargs.get("progress_current") == 1
        and call.kwargs.get("progress_total") == 1
        and call.kwargs.get("progress_message") == "Dispatching"
        for call in manager.checkpoint.call_args_list
        if isinstance(call.kwargs, dict)
    )


def test_durable_executor_blocks_false_terminal_success_when_completion_gate_unmet() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#durable_completion_gate_unmet",
        initial_state="dispatch",
        states={
            "dispatch": WorkflowStateSpec(
                state_id="dispatch",
                actions=(WorkflowActionInvocation(action_id="dispatch.action"),),
                terminal=True,
            ),
        },
        termination_states=("dispatch",),
        metadata={
            "plan_state_policy": {
                "schema_version": "workflow_plan_state_policy.v1",
                "plan_items": ["dispatch", "finalise"],
            },
            "completion_gate": {
                "schema_version": "workflow_completion_gate.v1",
                "required_done_plan_items": ["dispatch", "finalise"],
                "required_context_keys": ["dispatch.completed"],
            },
        },
    )

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="dispatch.action",
            handler=lambda _request: WorkflowActionResult(outputs={}),
        )
    )

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
    assert result.error is not None
    assert result.error.startswith("workflow_completion_gate_unmet")
    gate = result.data.get("workflow_completion_gate")
    assert isinstance(gate, dict)
    assert gate["safe_to_claim_completion"] is False
    assert "required_plan_items_unmet" in gate["blocking_reason_codes"]
    assert "missing_required_context_keys" in gate["blocking_reason_codes"]

