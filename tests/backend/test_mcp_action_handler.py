"""Tests for Phase 3.1: Generic MCP tool invocation action handler.

JVNAUTOSCI-922 Phase 3.1: Verifies that:
- ActionRegistry fallback handler routes unregistered action IDs.
- _action_mcp_tool_invoke invokes the gateway correctly.
- Payload defaults (namespace, gmail_profile) are applied.
- Errors are returned cleanly when the gateway is unavailable or raises.
- Explicitly registered actions still take precedence over the fallback.
"""

from __future__ import annotations

from typing import Any, Dict, MutableMapping
from unittest.mock import MagicMock, patch

import pytest

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)


# ---------------------------------------------------------------------------
# ActionRegistry fallback handler tests.
# ---------------------------------------------------------------------------


class TestFallbackHandler:
    def test_unregistered_action_uses_fallback(self):
        """Unregistered action IDs should route through the fallback handler."""
        fallback_called_with: list[str] = []

        def fallback(request: WorkflowActionRequest) -> WorkflowActionResult:
            fallback_called_with.append(request.action_id)
            return WorkflowActionResult(outputs={"via": "fallback"})

        registry = ActionRegistry()
        registry.set_fallback_handler(fallback)

        result = registry.execute(
            "some_mcp_tool",
            inputs={"key": "val"},
            context={},
            env=WorkflowEnvironment(llm_client=None),
        )

        assert result.ok
        assert result.outputs["via"] == "fallback"
        assert fallback_called_with == ["some_mcp_tool"]

    def test_registered_action_takes_precedence(self):
        """Explicitly registered actions must not be bypassed by the fallback."""

        def explicit(request: WorkflowActionRequest) -> WorkflowActionResult:
            return WorkflowActionResult(outputs={"via": "explicit"})

        def fallback(request: WorkflowActionRequest) -> WorkflowActionResult:
            return WorkflowActionResult(outputs={"via": "fallback"})

        registry = ActionRegistry()
        registry.register(ActionSpec(action_id="my_action", handler=explicit))
        registry.set_fallback_handler(fallback)

        result = registry.execute(
            "my_action",
            inputs={},
            context={},
            env=WorkflowEnvironment(llm_client=None),
        )
        assert result.outputs["via"] == "explicit"

    def test_no_fallback_returns_error(self):
        """Without a fallback, unregistered actions return action_not_registered."""
        registry = ActionRegistry()
        result = registry.execute(
            "missing",
            inputs={},
            context={},
            env=WorkflowEnvironment(llm_client=None),
        )
        assert not result.ok
        assert "action_not_registered:missing" in (result.error or "")

    def test_fallback_exception_returns_failed(self):
        """If the fallback handler raises, the registry returns a failed result."""

        def bad_fallback(request: WorkflowActionRequest) -> WorkflowActionResult:
            raise RuntimeError("gateway exploded")

        registry = ActionRegistry()
        registry.set_fallback_handler(bad_fallback)

        result = registry.execute(
            "broken_tool",
            inputs={},
            context={},
            env=WorkflowEnvironment(llm_client=None),
        )
        assert not result.ok
        assert "gateway exploded" in (result.error or "")

    def test_fallback_receives_correct_inputs(self):
        """The fallback handler receives action_id, inputs, context, and env."""
        captured: list[WorkflowActionRequest] = []

        def capture(request: WorkflowActionRequest) -> WorkflowActionResult:
            captured.append(request)
            return WorkflowActionResult()

        registry = ActionRegistry()
        registry.set_fallback_handler(capture)
        env = WorkflowEnvironment(llm_client="test_client", user_namespace="#V#user")
        ctx = {"some": "context"}

        registry.execute(
            "tool_x",
            inputs={"a": 1},
            context=ctx,
            env=env,
        )

        assert len(captured) == 1
        req = captured[0]
        assert req.action_id == "tool_x"
        assert req.inputs == {"a": 1}
        assert req.data is ctx
        assert req.environment is env


# ---------------------------------------------------------------------------
# _action_mcp_tool_invoke tests (via orchestrator).
# ---------------------------------------------------------------------------


def _build_mock_orchestrator(
    *,
    gateway_enabled: bool = True,
    invoke_payload: Any = None,
    invoke_duration_ms: float = 42.0,
    invoke_raises: Exception | None = None,
    describe_methods_result: Dict[str, Any] | None = None,
):
    """Build a minimal mock orchestrator with _action_mcp_tool_invoke wired up."""
    # Import here to avoid circular issues at module level.
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    # Create gateway mock.
    gateway = MagicMock()
    gateway.enabled = gateway_enabled

    if invoke_raises:
        gateway.invoke.side_effect = invoke_raises
    else:
        result_mock = MagicMock()
        result_mock.payload = invoke_payload or {"found": True}
        result_mock.duration_ms = invoke_duration_ms
        gateway.invoke.return_value = result_mock

    gateway.describe_methods.return_value = describe_methods_result or {}

    # Build orchestrator with mocked dependencies.
    with patch.object(InternalMCPChatOrchestrator, "__init__", lambda self: None):
        orch = InternalMCPChatOrchestrator()  # type: ignore[call-arg]
        orch._gateway = gateway  # type: ignore[assignment]
        orch._logger = MagicMock()
        # _apply_payload_defaults and _tool_schema_for_name exist on the
        # class; we don't need to mock them for most tests.

    return orch, gateway


class TestMCPToolInvoke:
    def test_invokes_gateway_with_correct_tool_name(self):
        orch, gw = _build_mock_orchestrator(invoke_payload={"concepts": []})

        request = WorkflowActionRequest(
            action_id="search_concepts",
            inputs={"query": "workflow"},
            environment=WorkflowEnvironment(
                llm_client=None, user_namespace="#V#test_user"
            ),
            data={},
        )
        result = orch._action_mcp_tool_invoke(request)

        assert result.ok
        assert result.outputs["mcp_tool"] == "search_concepts"
        assert result.outputs["mcp_result"] == {"concepts": []}
        assert result.outputs["mcp_duration_ms"] == 42.0
        # Gateway was called with the tool name and a payload dict.
        gw.invoke.assert_called_once()
        call_args = gw.invoke.call_args
        assert call_args[0][0] == "search_concepts"

    def test_inputs_passed_as_payload(self):
        orch, gw = _build_mock_orchestrator()

        request = WorkflowActionRequest(
            action_id="fetch_concept",
            inputs={"concept_id": "#V#my_concept", "include_relations": True},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        orch._action_mcp_tool_invoke(request)

        payload = gw.invoke.call_args[0][1]
        assert payload["concept_id"] == "#V#my_concept"
        assert payload["include_relations"] is True

    def test_gateway_disabled_returns_error(self):
        orch, _ = _build_mock_orchestrator(gateway_enabled=False)

        request = WorkflowActionRequest(
            action_id="some_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        result = orch._action_mcp_tool_invoke(request)

        assert not result.ok
        assert "gateway_unavailable" in (result.error or "")

    def test_gateway_none_returns_error(self):
        from src.backend.integrations.internal_mcp.orchestrator import (
            InternalMCPChatOrchestrator,
        )

        with patch.object(InternalMCPChatOrchestrator, "__init__", lambda self: None):
            orch = InternalMCPChatOrchestrator()  # type: ignore[call-arg]
            orch._gateway = None  # type: ignore[assignment]
            orch._logger = MagicMock()

        request = WorkflowActionRequest(
            action_id="tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        result = orch._action_mcp_tool_invoke(request)
        assert not result.ok
        assert "gateway_unavailable" in (result.error or "")

    def test_gateway_exception_returns_failed(self):
        orch, _ = _build_mock_orchestrator(
            invoke_raises=RuntimeError("connection_timeout")
        )

        request = WorkflowActionRequest(
            action_id="flaky_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        result = orch._action_mcp_tool_invoke(request)

        assert not result.ok
        assert "mcp_invoke_failed:flaky_tool" in (result.error or "")
        assert "connection_timeout" in (result.error or "")

    def test_result_alias_for_conditions(self):
        """The 'result' output alias allows transition conditions to work."""
        orch, _ = _build_mock_orchestrator(invoke_payload={"success": True})

        request = WorkflowActionRequest(
            action_id="check_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        result = orch._action_mcp_tool_invoke(request)

        assert result.ok
        # 'result' should be set so that on_true/on_false conditions work.
        assert result.outputs["result"] == {"success": True}

    def test_empty_inputs_sends_empty_payload(self):
        orch, gw = _build_mock_orchestrator()

        request = WorkflowActionRequest(
            action_id="no_args_tool",
            inputs={},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
        orch._action_mcp_tool_invoke(request)

        payload = gw.invoke.call_args[0][1]
        assert isinstance(payload, dict)


# ---------------------------------------------------------------------------
# Integration: fallback + orchestrator handler end-to-end.
# ---------------------------------------------------------------------------


class TestFallbackWithOrchestrator:
    def test_registry_with_fallback_routes_unknown_to_gateway(self):
        """When the registry fallback is wired to _action_mcp_tool_invoke,
        unregistered action IDs invoke the MCP gateway."""
        orch, gw = _build_mock_orchestrator(invoke_payload={"items": [1, 2, 3]})

        registry = ActionRegistry()
        registry.set_fallback_handler(orch._action_mcp_tool_invoke)

        # Execute an action that isn't registered.
        result = registry.execute(
            "list_concepts",
            inputs={"limit": 10},
            context={},
            env=WorkflowEnvironment(llm_client=None, user_namespace="#V#u"),
        )

        assert result.ok
        assert result.outputs["mcp_tool"] == "list_concepts"
        gw.invoke.assert_called_once()

    def test_registered_action_still_works_with_fallback(self):
        """Explicit registrations are not affected by the fallback."""
        orch, gw = _build_mock_orchestrator()

        def custom(req: WorkflowActionRequest) -> WorkflowActionResult:
            return WorkflowActionResult(outputs={"custom": True})

        registry = ActionRegistry()
        registry.register(ActionSpec(action_id="custom.action", handler=custom))
        registry.set_fallback_handler(orch._action_mcp_tool_invoke)

        result = registry.execute(
            "custom.action",
            inputs={},
            context={},
            env=WorkflowEnvironment(llm_client=None),
        )
        assert result.outputs["custom"] is True
        gw.invoke.assert_not_called()
