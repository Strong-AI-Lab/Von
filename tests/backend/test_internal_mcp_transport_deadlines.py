from __future__ import annotations

import time
from threading import Event, Thread

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
    get_internal_mcp_actor_context_source,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import (
    _BoundedHandlerExecutor,
    InternalMCPTransport,
    get_internal_mcp_execution_scope,
    internal_mcp_cancellation_requested,
    raise_if_internal_mcp_cancelled,
)


def _gateway_for(
    *,
    method_name: str,
    handler,
    transport: InternalMCPTransport,
    category: str = "read",
    output_schema: Schema | None = None,
) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name=method_name,
            handler=handler,
            input_schema=Schema(required={}, optional={}, allow_unknown=False),
            output_schema=output_schema,
            category=category,
        )
    )
    return InternalMCPGateway(
        catalogue=catalogue,
        transport=transport,
        enabled=True,
    )


def test_handler_before_hard_deadline_remains_success_after_advisory_budget() -> None:
    transport = InternalMCPTransport(
        read_timeout_sec=0.25,
        read_advisory_timeout_sec=0.01,
    )

    def _handler():
        time.sleep(0.03)
        scope = get_internal_mcp_execution_scope()
        return {
            "success": True,
            "execution_scope_present": scope is not None,
            "actor_context_source": get_internal_mcp_actor_context_source(),
        }

    gateway = _gateway_for(
        method_name="synthetic_fast_read",
        handler=_handler,
        transport=transport,
    )

    result = gateway.invoke("synthetic_fast_read", {})

    assert result.payload == {
        "success": True,
        "execution_scope_present": True,
        "actor_context_source": "tool_payload_fallback",
    }
    assert result.outcome == "completed"
    assert result.timed_out is False
    assert result.advisory_budget_exceeded is True
    assert result.handler_duration_ms is not None
    assert result.handler_duration_ms >= 20.0
    assert result.queue_duration_ms is not None
    assert result.transport_overhead_ms is not None

    diagnostics = gateway.get_diagnostics()
    method_metrics = diagnostics["methods"]["synthetic_fast_read"]
    assert method_metrics["last_outcome"] == "completed"
    assert method_metrics["timeouts"] == 0


def test_hard_deadline_returns_typed_timeout_and_requests_cooperative_cancel() -> None:
    cancellation_seen = Event()
    handler_released = Event()
    transport = InternalMCPTransport(
        read_timeout_sec=0.04,
        read_advisory_timeout_sec=0.01,
    )

    def _handler():
        try:
            while True:
                if internal_mcp_cancellation_requested():
                    cancellation_seen.set()
                    raise_if_internal_mcp_cancelled()
                time.sleep(0.002)
        finally:
            handler_released.set()

    gateway = _gateway_for(
        method_name="synthetic_slow_read",
        handler=_handler,
        transport=transport,
    )

    started_at = time.perf_counter()
    result = gateway.invoke("synthetic_slow_read", {})
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.2
    assert result.outcome == "timed_out"
    assert result.payload["success"] is False
    assert result.payload["status"] == "timed_out"
    assert result.payload["error_code"] == "tool_timeout"
    assert result.payload["retryable"] is True
    assert result.payload["outcome_finality"] == "terminal_for_turn"
    assert result.payload["late_result_policy"] == "discard_from_turn"
    assert result.payload["handler_isolation"] == "bounded_worker_pool"
    assert result.payload["timeout_phase"] == "handler"
    assert result.handler_duration_ms is None
    assert result.handler_elapsed_ms is not None
    assert cancellation_seen.wait(timeout=1.0)
    assert handler_released.wait(timeout=1.0)

    deadline = time.monotonic() + 1.0
    diagnostics = transport.get_diagnostics()
    while diagnostics["late_completion_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
        diagnostics = transport.get_diagnostics()
    assert diagnostics["late_completion_count"] == 1
    assert diagnostics["late_completions"][0]["payload_discarded"] is True

    method_metrics = gateway.get_diagnostics()["methods"]["synthetic_slow_read"]
    assert method_metrics["timeouts"] == 1
    assert method_metrics["last_outcome"] == "timed_out"
    assert method_metrics["last_error"] == "tool_timeout"


def test_caller_deadline_shortens_the_registered_method_timeout() -> None:
    cancellation_seen = Event()
    handler_deadlines: list[float] = []
    transport = InternalMCPTransport(
        read_timeout_sec=0.5,
        read_advisory_timeout_sec=0.1,
    )

    def _handler():
        scope = get_internal_mcp_execution_scope()
        assert scope is not None
        handler_deadlines.append(scope.deadline_monotonic)
        while not internal_mcp_cancellation_requested():
            time.sleep(0.002)
        cancellation_seen.set()
        return {"success": True, "late_payload": "must_be_discarded"}

    gateway = _gateway_for(
        method_name="synthetic_caller_bounded_read",
        handler=_handler,
        transport=transport,
    )
    caller_deadline = time.monotonic() + 0.04

    started_at = time.perf_counter()
    result = gateway.invoke(
        "synthetic_caller_bounded_read",
        {},
        deadline_monotonic=caller_deadline,
    )
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.2
    assert result.outcome == "timed_out"
    assert result.timeout_sec is not None
    assert 0.0 < result.timeout_sec < 0.1
    assert result.payload["timeout_phase"] == "handler"
    assert result.payload["late_result_policy"] == "discard_from_turn"
    assert "late_payload" not in result.payload
    assert handler_deadlines
    assert handler_deadlines[0] <= caller_deadline + 0.005
    assert cancellation_seen.wait(timeout=1.0)


def test_result_completed_after_absolute_deadline_is_discarded() -> None:
    class _AfterDeadlineExecutor:
        @staticmethod
        def submit(task) -> bool:
            with task.lock:
                task.started_at = task.submitted_at
                task.result = {"success": True, "late_payload": "discard me"}
                task.completed_at = time.perf_counter()
                task.completed_monotonic = task.deadline_monotonic + 0.001
                task.done_event.set()
            return True

        @staticmethod
        def diagnostics() -> dict:
            return {"test_executor": True}

    transport = InternalMCPTransport(
        read_timeout_sec=0.05,
        read_advisory_timeout_sec=0.01,
        handler_executor=_AfterDeadlineExecutor(),  # type: ignore[arg-type]
    )
    gateway = _gateway_for(
        method_name="synthetic_deadline_race",
        handler=lambda: {"success": True},
        transport=transport,
    )

    result = gateway.invoke("synthetic_deadline_race", {})

    assert result.outcome == "timed_out"
    assert result.payload["error_code"] == "tool_timeout"
    assert result.payload["outcome_finality"] == "terminal_for_turn"
    assert "late_payload" not in result.payload
    diagnostics = transport.get_diagnostics()
    assert diagnostics["late_completion_count"] == 1
    assert diagnostics["late_completions"][0]["payload_discarded"] is True


def test_expired_caller_deadline_returns_timeout_without_dispatch() -> None:
    handler_called = Event()
    executor = _BoundedHandlerExecutor(worker_count=1, queue_capacity=1)
    transport = InternalMCPTransport(
        read_timeout_sec=0.5,
        read_advisory_timeout_sec=0.1,
        handler_executor=executor,
    )

    def _handler():
        handler_called.set()
        return {"success": True}

    gateway = _gateway_for(
        method_name="synthetic_expired_read",
        handler=_handler,
        transport=transport,
    )

    result = gateway.invoke(
        "synthetic_expired_read",
        {},
        deadline_monotonic=time.monotonic() - 1.0,
    )

    assert result.outcome == "timed_out"
    assert result.timeout_sec == 0.0
    assert result.timeout_phase == "pre_dispatch"
    assert result.payload["timeout_phase"] == "pre_dispatch"
    assert result.payload["cancellation_requested"] is True
    assert result.payload["outcome_finality"] == "terminal_for_turn"
    assert handler_called.is_set() is False
    assert executor.diagnostics()["submitted_count"] == 0


def test_expired_write_deadline_reports_that_no_mutation_started() -> None:
    handler_called = Event()
    executor = _BoundedHandlerExecutor(worker_count=1, queue_capacity=1)
    transport = InternalMCPTransport(
        write_timeout_sec=0.5,
        write_advisory_timeout_sec=0.1,
        handler_executor=executor,
    )
    gateway = _gateway_for(
        method_name="synthetic_expired_write",
        handler=lambda: handler_called.set(),
        transport=transport,
        category="write",
    )

    result = gateway.invoke(
        "synthetic_expired_write",
        {},
        deadline_monotonic=time.monotonic() - 1.0,
    )

    assert result.outcome == "timed_out"
    assert result.timeout_phase == "pre_dispatch"
    assert result.payload["error_code"] == "tool_timeout"
    assert result.payload["retryable"] is True
    assert result.payload["mutation_outcome"] == "not_started"
    assert handler_called.is_set() is False
    assert executor.diagnostics()["submitted_count"] == 0


def test_non_cooperative_late_handler_does_not_hold_the_calling_worker() -> None:
    late_handler_released = Event()
    transport = InternalMCPTransport(
        read_timeout_sec=0.03,
        read_advisory_timeout_sec=0.01,
    )

    def _handler():
        try:
            time.sleep(0.2)
            return {"success": True, "late_payload": "must_not_replace_timeout"}
        finally:
            late_handler_released.set()

    gateway = _gateway_for(
        method_name="synthetic_non_cooperative_read",
        handler=_handler,
        transport=transport,
    )

    started_at = time.perf_counter()
    result = gateway.invoke("synthetic_non_cooperative_read", {})
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.15
    assert result.payload["error_code"] == "tool_timeout"
    assert "late_payload" not in result.payload
    assert late_handler_released.wait(timeout=1.0)

    deadline = time.monotonic() + 1.0
    diagnostics = transport.get_diagnostics()
    while diagnostics["late_completion_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
        diagnostics = transport.get_diagnostics()
    assert diagnostics["late_completions"][0]["outcome"] == "late_success"
    assert diagnostics["late_completions"][0]["payload_discarded"] is True


def test_write_timeout_is_explicitly_indeterminate_and_not_retryable() -> None:
    transport = InternalMCPTransport(
        write_timeout_sec=0.03,
        write_advisory_timeout_sec=0.01,
    )
    gateway = _gateway_for(
        method_name="synthetic_slow_write",
        handler=lambda: time.sleep(0.1),
        transport=transport,
        category="write",
    )

    result = gateway.invoke("synthetic_slow_write", {})

    assert result.payload["success"] is False
    assert result.payload["error_code"] == "tool_timeout_outcome_unknown"
    assert result.payload["retryable"] is False
    assert result.payload["mutation_outcome"] == "unknown"
    assert result.payload["recovery_affordances"] == [
        {"action_type": "inspect_operation_state_before_retry"}
    ]


def test_dispatched_write_reports_one_bounded_late_completion_observation() -> None:
    observations = []
    observation_seen = Event()
    transport = InternalMCPTransport(
        write_timeout_sec=0.03,
        write_advisory_timeout_sec=0.01,
    )

    def _handler():
        while not internal_mcp_cancellation_requested():
            time.sleep(0.002)
        return {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "created_ids": ["#V#late_created"],
            "api_key": "must-not-leave-the-transport-worker",
            "large_detail": "x" * 100_000,
        }

    def _observe(observation) -> None:
        observations.append(observation)
        observation_seen.set()

    gateway = _gateway_for(
        method_name="synthetic_observed_slow_write",
        handler=_handler,
        transport=transport,
        category="write",
    )

    result = gateway.invoke(
        "synthetic_observed_slow_write",
        {},
        late_completion_observer=_observe,
    )

    assert result.outcome == "timed_out"
    assert result.payload["mutation_outcome"] == "unknown"
    assert result.payload["late_result_policy"] == "observe_out_of_band"
    assert result.telemetry_metadata()["late_result_policy"] == "observe_out_of_band"
    assert observation_seen.wait(timeout=1.0)
    assert len(observations) == 1
    observation = observations[0]
    assert observation["execution_id"] == result.execution_id
    assert observation["method_name"] == "synthetic_observed_slow_write"
    assert observation["category"] == "write"
    assert observation["outcome"] == "late_success"
    assert observation["payload"]["effect_status"] == "succeeded"
    assert observation["payload"]["changed"] is True
    assert observation["payload"]["_truncated"] is True
    assert observation["payload_truncated"] is True
    assert observation["output_schema_validation"] == "indeterminate_truncated"
    assert observation["output_schema_valid"] is None

    time.sleep(0.02)
    assert len(observations) == 1
    diagnostics = transport.get_diagnostics()
    assert diagnostics["late_completion_count"] == 1
    assert diagnostics["late_completions"][0]["observer_notified"] is True
    assert diagnostics["late_completions"][0]["payload_discarded"] is False


def test_queued_write_timeout_reports_not_started_and_never_observes() -> None:
    blocker_started = Event()
    release_blocker = Event()
    write_handler_called = Event()
    observations = []
    executor = _BoundedHandlerExecutor(worker_count=1, queue_capacity=1)
    transport = InternalMCPTransport(
        read_timeout_sec=0.5,
        write_timeout_sec=0.03,
        read_advisory_timeout_sec=0.1,
        write_advisory_timeout_sec=0.01,
        handler_executor=executor,
    )

    def _blocker():
        blocker_started.set()
        assert release_blocker.wait(timeout=1.0)
        return {"success": True}

    blocker_gateway = _gateway_for(
        method_name="synthetic_queue_blocker",
        handler=_blocker,
        transport=transport,
    )
    blocker_results = []
    blocker_thread = Thread(
        target=lambda: blocker_results.append(
            blocker_gateway.invoke("synthetic_queue_blocker", {})
        )
    )
    blocker_thread.start()
    assert blocker_started.wait(timeout=1.0)

    def _queued_write():
        write_handler_called.set()
        return {"success": True, "changed": True}

    write_gateway = _gateway_for(
        method_name="synthetic_queued_observed_write",
        handler=_queued_write,
        transport=transport,
        category="write",
    )
    result = write_gateway.invoke(
        "synthetic_queued_observed_write",
        {},
        late_completion_observer=observations.append,
    )

    assert result.outcome == "timed_out"
    assert result.timeout_phase == "queue"
    assert result.payload["timeout_phase"] == "queue"
    assert result.payload["error_code"] == "tool_timeout"
    assert result.payload["retryable"] is True
    assert result.payload["mutation_outcome"] == "not_started"
    assert result.payload["late_result_policy"] == "discard_from_turn"
    assert result.telemetry_metadata()["late_result_policy"] == "discard_from_turn"
    assert write_handler_called.is_set() is False
    assert observations == []

    release_blocker.set()
    blocker_thread.join(timeout=1.0)
    assert blocker_thread.is_alive() is False
    assert blocker_results and blocker_results[0].outcome == "completed"

    completion_deadline = time.monotonic() + 1.0
    while (
        executor.diagnostics()["completed_count"] < 2
        and time.monotonic() < completion_deadline
    ):
        time.sleep(0.002)

    assert executor.diagnostics()["completed_count"] == 2
    assert write_handler_called.is_set() is False
    assert observations == []
    assert transport.get_diagnostics()["late_completion_count"] == 0


def test_gateway_marks_invalid_late_write_output_as_indeterminate_evidence() -> None:
    observations = []
    observation_seen = Event()
    transport = InternalMCPTransport(
        write_timeout_sec=0.03,
        write_advisory_timeout_sec=0.01,
    )

    def _handler():
        while not internal_mcp_cancellation_requested():
            time.sleep(0.002)
        return {"success": True, "effect_status": 7}

    def _observe(observation) -> None:
        observations.append(observation)
        observation_seen.set()

    gateway = _gateway_for(
        method_name="synthetic_invalid_late_write",
        handler=_handler,
        transport=transport,
        category="write",
        output_schema=Schema(
            required={"success": bool, "effect_status": str},
            optional={},
            allow_unknown=False,
        ),
    )
    result = gateway.invoke(
        "synthetic_invalid_late_write",
        {},
        late_completion_observer=_observe,
    )

    assert result.outcome == "timed_out"
    assert result.payload["mutation_outcome"] == "unknown"
    assert observation_seen.wait(timeout=1.0)
    assert len(observations) == 1
    assert observations[0]["output_schema_validation"] == "invalid"
    assert observations[0]["output_schema_valid"] is False
    assert "effect_status" in observations[0]["output_schema_error"]


def test_read_late_completion_is_discarded_even_when_observer_is_supplied() -> None:
    observations = []
    transport = InternalMCPTransport(
        read_timeout_sec=0.03,
        read_advisory_timeout_sec=0.01,
    )

    def _handler():
        while not internal_mcp_cancellation_requested():
            time.sleep(0.002)
        return {"success": True, "late_payload": "discard me"}

    gateway = _gateway_for(
        method_name="synthetic_observer_ignored_for_read",
        handler=_handler,
        transport=transport,
    )
    result = gateway.invoke(
        "synthetic_observer_ignored_for_read",
        {},
        late_completion_observer=observations.append,
    )

    assert result.outcome == "timed_out"
    assert result.payload["late_result_policy"] == "discard_from_turn"
    deadline = time.monotonic() + 1.0
    diagnostics = transport.get_diagnostics()
    while diagnostics["late_completion_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
        diagnostics = transport.get_diagnostics()
    assert observations == []
    assert diagnostics["late_completions"][0]["payload_discarded"] is True
    assert diagnostics["late_completions"][0]["observer_notified"] is False


def test_late_completion_observer_failure_does_not_kill_bounded_worker() -> None:
    executor = _BoundedHandlerExecutor(worker_count=1, queue_capacity=2)
    transport = InternalMCPTransport(
        write_timeout_sec=0.03,
        write_advisory_timeout_sec=0.01,
        handler_executor=executor,
    )

    def _late_handler():
        while not internal_mcp_cancellation_requested():
            time.sleep(0.002)
        return {"success": True, "changed": True}

    gateway = _gateway_for(
        method_name="synthetic_observer_failure_write",
        handler=_late_handler,
        transport=transport,
        category="write",
    )

    result = gateway.invoke(
        "synthetic_observer_failure_write",
        {},
        late_completion_observer=lambda _observation: (_ for _ in ()).throw(
            RuntimeError("observer failed")
        ),
    )
    assert result.outcome == "timed_out"

    deadline = time.monotonic() + 1.0
    diagnostics = transport.get_diagnostics()
    while diagnostics["late_completion_count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
        diagnostics = transport.get_diagnostics()
    assert diagnostics["late_completions"][0]["observer_error_type"] == "RuntimeError"
    assert diagnostics["late_completions"][0]["payload_discarded"] is True

    follow_up_gateway = _gateway_for(
        method_name="synthetic_write_after_observer_failure",
        handler=lambda: {"success": True, "changed": True},
        transport=transport,
        category="write",
    )
    follow_up = follow_up_gateway.invoke(
        "synthetic_write_after_observer_failure",
        {},
    )
    assert follow_up.outcome == "completed"
    assert follow_up.payload == {"success": True, "changed": True}
    assert executor.diagnostics()["completed_count"] >= 2


def test_handler_pool_rejects_excess_work_instead_of_growing_unbounded() -> None:
    handler_started = Event()
    rejected_write_started = Event()
    release_handler = Event()
    executor = _BoundedHandlerExecutor(worker_count=1, queue_capacity=1)
    transport = InternalMCPTransport(
        read_timeout_sec=0.5,
        read_advisory_timeout_sec=0.1,
        handler_executor=executor,
    )

    def _handler():
        handler_started.set()
        assert release_handler.wait(timeout=1.0)
        return {"success": True}

    gateway = _gateway_for(
        method_name="synthetic_bounded_pool_read",
        handler=_handler,
        transport=transport,
    )
    completed_results = []
    first = Thread(
        target=lambda: completed_results.append(
            gateway.invoke("synthetic_bounded_pool_read", {})
        )
    )
    second = Thread(
        target=lambda: completed_results.append(
            gateway.invoke("synthetic_bounded_pool_read", {})
        )
    )
    first.start()
    assert handler_started.wait(timeout=1.0)
    second.start()

    queue_deadline = time.monotonic() + 1.0
    while (
        transport.get_diagnostics()["handler_pool"]["queue_depth"] < 1
        and time.monotonic() < queue_deadline
    ):
        time.sleep(0.002)

    rejected_write_gateway = _gateway_for(
        method_name="synthetic_rejected_pool_write",
        handler=lambda: rejected_write_started.set(),
        transport=transport,
        category="write",
    )
    rejected = rejected_write_gateway.invoke("synthetic_rejected_pool_write", {})

    assert rejected.outcome == "saturated"
    assert rejected.payload["error_code"] == "internal_mcp_handler_pool_saturated"
    assert rejected.payload["retryable"] is True
    assert rejected.payload["mutation_outcome"] == "not_started"
    assert rejected.payload["recovery_affordances"][0]["action_type"] == "bounded_retry"
    assert rejected_write_started.is_set() is False
    assert executor.diagnostics()["worker_count"] == 1
    assert executor.diagnostics()["queue_capacity"] == 1
    assert executor.diagnostics()["rejected_count"] == 1

    release_handler.set()
    first.join(timeout=1.0)
    second.join(timeout=1.0)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert len(completed_results) == 2
    assert all(result.outcome == "completed" for result in completed_results)
