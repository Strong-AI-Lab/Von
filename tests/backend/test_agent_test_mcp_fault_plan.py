from __future__ import annotations

import math
from typing import Any

import pytest

from src.backend.integrations.internal_mcp.agent_test_fault_plan import (
    AGENT_TEST_MCP_FAULT_EVENT_SCHEMA_VERSION,
    AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION,
    AgentTestMCPFaultPlanError,
    bind_agent_test_mcp_fault_plan,
)
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.schemas import Schema, SchemaValidationError
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> tuple[InternalMCPGateway, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def _handler(**payload: Any) -> dict[str, Any]:
        calls.append(dict(payload))
        return {"success": True, "value": payload["value"]}

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="fault_probe",
            handler=_handler,
            input_schema=Schema(
                required={"value": str},
                optional={},
                allow_unknown=False,
            ),
            output_schema=Schema(
                required={"success": bool, "value": str},
                optional={},
                allow_unknown=False,
            ),
            category="read",
        )
    )
    catalogue.register(
        MethodDefinition(
            name="unaffected_probe",
            handler=_handler,
            input_schema=Schema(
                required={"value": str},
                optional={},
                allow_unknown=False,
            ),
            output_schema=Schema(
                required={"success": bool, "value": str},
                optional={},
                allow_unknown=False,
            ),
            category="read",
        )
    )
    return (
        InternalMCPGateway(
            catalogue=catalogue,
            transport=InternalMCPTransport(),
            enabled=True,
        ),
        calls,
    )


def _plan(
    fault_class: str,
    *,
    max_activations: int = 1,
    retry_after_seconds: float | None = None,
) -> dict[str, Any]:
    fault: dict[str, Any] = {
        "fault_id": f"probe_{fault_class}",
        "tool_name": "fault_probe",
        "fault_class": fault_class,
        "max_activations": max_activations,
    }
    if retry_after_seconds is not None:
        fault["retry_after_seconds"] = retry_after_seconds
    return {
        "schema_version": AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION,
        "plan_id": "focused_gateway_fault_test",
        "faults": [fault],
    }


def test_fault_plan_fails_closed_outside_agent_test(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)

    with pytest.raises(
        AgentTestMCPFaultPlanError,
        match="requires_agent_test_instance",
    ):
        with bind_agent_test_mcp_fault_plan(_plan("timeout")):
            raise AssertionError("fault scope must not open")


@pytest.mark.parametrize(
    ("fault_class", "expected_error_code", "retry_after_seconds"),
    [
        ("timeout", "tool_timeout", None),
        ("rate_limit", "rate_limit", 2.5),
        (
            "temporary_unavailability",
            "tool_temporarily_unavailable",
            1.0,
        ),
    ],
)
def test_gateway_injects_typed_agent_test_fault_with_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    fault_class: str,
    expected_error_code: str,
    retry_after_seconds: float | None,
):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    gateway, calls = _build_gateway()

    with bind_agent_test_mcp_fault_plan(
        _plan(
            fault_class,
            retry_after_seconds=retry_after_seconds,
        )
    ) as scope:
        payload = gateway.invoke("fault_probe", {"value": "safe"}).payload

        assert payload["success"] is False
        assert payload["error_code"] == expected_error_code
        assert payload["retryable"] is True
        if retry_after_seconds is None:
            assert "retry_after_seconds" not in payload
        else:
            assert payload["retry_after_seconds"] == retry_after_seconds
        event = payload["agent_test_fault_event"]
        assert event["schema_version"] == AGENT_TEST_MCP_FAULT_EVENT_SCHEMA_VERSION
        assert event["source"] == "agent_test_mcp_fault_plan"
        assert event["agent_test_instance"] is True
        assert event["plan_id"] == "focused_gateway_fault_test"
        assert len(event["plan_sha256"]) == 64
        assert event["fault_class"] == fault_class
        assert event["tool_name"] == "fault_probe"
        assert event["activation_index"] == 1
        assert event["remaining_activations"] == 0
        assert scope.event_snapshot() == (event,)

    assert calls == []
    diagnostics = gateway.get_diagnostics()
    assert diagnostics["total_calls"] == 1
    assert diagnostics["total_failures"] == 1
    assert diagnostics["methods"]["fault_probe"]["failures"] == 1
    assert diagnostics["methods"]["fault_probe"]["last_error"] == (expected_error_code)


def test_gateway_applies_three_same_tool_faults_in_represented_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    gateway, calls = _build_gateway()
    plan = {
        "schema_version": AGENT_TEST_MCP_FAULT_PLAN_SCHEMA_VERSION,
        "plan_id": "ordered_transient_chain",
        "faults": [
            {
                "fault_id": "timeout_first",
                "tool_name": "fault_probe",
                "fault_class": "timeout",
                "max_activations": 1,
            },
            {
                "fault_id": "rate_limit_second",
                "tool_name": "fault_probe",
                "fault_class": "rate_limit",
                "max_activations": 1,
                "retry_after_seconds": 0.0,
            },
            {
                "fault_id": "unavailable_third",
                "tool_name": "fault_probe",
                "fault_class": "temporary_unavailability",
                "max_activations": 1,
                "retry_after_seconds": 0.0,
            },
        ],
    }

    with bind_agent_test_mcp_fault_plan(plan) as scope:
        results = [
            gateway.invoke("fault_probe", {"value": f"attempt-{index}"}).payload
            for index in range(1, 5)
        ]

    assert [result.get("error_code") for result in results[:3]] == [
        "tool_timeout",
        "rate_limit",
        "tool_temporarily_unavailable",
    ]
    assert results[3] == {"success": True, "value": "attempt-4"}
    assert [event["fault_id"] for event in scope.event_snapshot()] == [
        "timeout_first",
        "rate_limit_second",
        "unavailable_third",
    ]
    assert calls == [{"value": "attempt-4"}]


def test_gateway_establishes_authenticated_actor_before_injecting_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.integrations.internal_mcp import agent_test_fault_plan
    from src.backend.security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
        override_current_actor,
    )

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    gateway, calls = _build_gateway()
    observed_actor: dict[str, str | None] = {}
    original_inject = agent_test_fault_plan.maybe_inject_agent_test_mcp_fault

    def _inspect_actor(tool_name: str):
        observed_actor["user_id"] = get_effective_user_concept_id()
        observed_actor["org_id"] = get_effective_organisation_concept_id()
        return original_inject(tool_name)

    monkeypatch.setattr(
        agent_test_fault_plan,
        "maybe_inject_agent_test_mcp_fault",
        _inspect_actor,
    )
    with override_current_actor("#V#unit_user", "#V#unit_org"):
        with bind_agent_test_mcp_fault_plan(_plan("timeout")):
            result = gateway.invoke("fault_probe", {"value": "safe"}).payload

    assert result["error_code"] == "tool_timeout"
    assert observed_actor == {
        "user_id": "#V#unit_user",
        "org_id": "#V#unit_org",
    }
    assert calls == []


def test_fault_scope_is_tool_specific_bounded_and_removed_on_exit(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "true")
    gateway, calls = _build_gateway()

    with bind_agent_test_mcp_fault_plan(
        _plan("temporary_unavailability", max_activations=1)
    ) as scope:
        unaffected = gateway.invoke("unaffected_probe", {"value": "other"}).payload
        first = gateway.invoke("fault_probe", {"value": "first"}).payload
        second = gateway.invoke("fault_probe", {"value": "second"}).payload

        assert unaffected == {"success": True, "value": "other"}
        assert first["error_code"] == "tool_temporarily_unavailable"
        assert second == {"success": True, "value": "second"}
        assert len(scope.event_snapshot()) == 1

    after_scope = gateway.invoke("fault_probe", {"value": "after"}).payload
    assert after_scope == {"success": True, "value": "after"}
    assert calls == [
        {"value": "other"},
        {"value": "second"},
        {"value": "after"},
    ]


def test_fault_plan_does_not_bypass_gateway_input_validation(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    gateway, calls = _build_gateway()

    with bind_agent_test_mcp_fault_plan(_plan("timeout")) as scope:
        with pytest.raises(SchemaValidationError):
            gateway.invoke("fault_probe", {"value": 7})

        assert scope.event_snapshot() == ()

    assert calls == []


def test_fault_plan_rejects_wildcards_and_unknown_policy_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    wildcard_plan = _plan("timeout")
    wildcard_plan["faults"][0]["tool_name"] = "*"

    with pytest.raises(AgentTestMCPFaultPlanError, match="tool_name_invalid"):
        with bind_agent_test_mcp_fault_plan(wildcard_plan):
            raise AssertionError("invalid fault scope must not open")

    unknown_field_plan = _plan("rate_limit")
    unknown_field_plan["faults"][0]["response_body"] = {"policy": "hidden"}
    with pytest.raises(AgentTestMCPFaultPlanError, match="unknown_fields"):
        with bind_agent_test_mcp_fault_plan(unknown_field_plan):
            raise AssertionError("invalid fault scope must not open")


@pytest.mark.parametrize(
    "unsafe_value",
    ["line\nbreak", "x" * 129],
)
def test_fault_plan_rejects_unbounded_or_log_unsafe_references(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_value: str,
):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    plan = _plan("rate_limit")
    plan["faults"][0]["fault_id"] = unsafe_value

    with pytest.raises(AgentTestMCPFaultPlanError, match="fault_id_invalid"):
        with bind_agent_test_mcp_fault_plan(plan):
            raise AssertionError("invalid fault scope must not open")


def test_fault_plan_rejects_non_finite_retry_hint(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")

    with pytest.raises(AgentTestMCPFaultPlanError, match="out_of_range"):
        with bind_agent_test_mcp_fault_plan(
            _plan("rate_limit", retry_after_seconds=math.nan)
        ):
            raise AssertionError("invalid fault scope must not open")
