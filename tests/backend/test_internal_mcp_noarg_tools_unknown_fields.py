"""Gateway-level regression tests for no-arg tools with orchestrator context fields.

These tools do not require caller arguments, but orchestrator payloads often include
context fields such as `namespace`. The tools must tolerate those fields rather than
failing input validation.
"""

from __future__ import annotations

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.schemas import validate_payload
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _invoke_and_assert_schema(
    gateway: InternalMCPGateway, method_name: str, payload: dict
) -> dict:
    result_payload = gateway.invoke(method_name, payload).payload
    definition = gateway.get_method_definition(method_name)
    assert definition is not None
    assert definition.output_schema is not None
    ok, errors = validate_payload(definition.output_schema, result_payload)
    assert ok, f"{method_name} output schema mismatch: {errors}"
    return result_payload


def test_get_context_accepts_orchestrator_namespace_field():
    gateway = _build_gateway()

    payload = {
        "namespace": "#V#test_user",
        "request_id": "req-123",
    }
    result = _invoke_and_assert_schema(gateway, "get_context", payload)

    assert isinstance(result.get("language"), str)
    assert isinstance(result.get("timestamp"), str)


def test_get_client_capabilities_accepts_orchestrator_namespace_field():
    gateway = _build_gateway()

    payload = {
        "namespace": "#V#test_user",
        "request_id": "req-456",
    }
    result = _invoke_and_assert_schema(gateway, "get_client_capabilities", payload)

    assert result.get("success") is True
    assert "capabilities" in result


def test_jira_get_auth_config_accepts_orchestrator_namespace_field():
    gateway = _build_gateway()

    payload = {
        "namespace": "#V#test_user",
        "request_id": "req-789",
    }
    result = _invoke_and_assert_schema(gateway, "jira_get_auth_config", payload)

    assert result.get("success") is True
    assert "token_present" in result

