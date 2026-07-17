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
) -> InternalMCPGateway:
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name=method_name,
            handler=handler,
            input_schema=Schema(required={}, optional={}, allow_unknown=False),
            output_schema=None,
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


def test_handler_pool_rejects_excess_work_instead_of_growing_unbounded() -> None:
    handler_started = Event()
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

    rejected = gateway.invoke("synthetic_bounded_pool_read", {})

    assert rejected.outcome == "saturated"
    assert rejected.payload["error_code"] == "internal_mcp_handler_pool_saturated"
    assert rejected.payload["retryable"] is True
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
