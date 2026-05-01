from __future__ import annotations

from typing import Any, cast

import pytest
from flask import Flask

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.server.routes import settings_routes
from src.backend.server.routes.settings_routes import settings_bp
from src.backend.services import settings_service


class _StubGateway:
    enabled = True

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        raise RuntimeError("not used")


class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.calls: list[dict[str, int]] = []

    def configure_execution_caps(
        self, *, max_tool_invocations: int, tool_batch_cap: int
    ) -> None:
        self.calls.append(
            {
                "max_tool_invocations": max_tool_invocations,
                "tool_batch_cap": tool_batch_cap,
            }
        )


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 100),
        ("", 100),
        ("not-a-number", 100),
        (True, 100),
        (-5, 0),
        (0, 0),
        (7, 7),
        (999, 500),
        ("12", 12),
    ],
)
def test_get_internal_mcp_max_tool_invocations_clamps(
    raw: Any, expected: int, monkeypatch
):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: raw)
    assert settings_service.get_internal_mcp_max_tool_invocations() == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 10),
        ("", 10),
        ("not-a-number", 10),
        (True, 10),
        (0, 1),
        (1, 1),
        (7, 7),
        (999, 20),
        ("12", 12),
    ],
)
def test_get_internal_mcp_tool_batch_cap_clamps(raw: Any, expected: int, monkeypatch):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: raw)
    assert settings_service.get_internal_mcp_tool_batch_cap() == expected


def test_get_all_settings_batch_uses_canonical_internal_mcp_defaults(monkeypatch):
    monkeypatch.setattr(settings_service, "get_settings_batch", lambda _names: {})

    result = settings_service.get_all_settings_batch()

    assert (
        result["internal_mcp_max_tool_invocations"]
        == settings_service.INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
    )
    assert (
        result["internal_mcp_tool_batch_cap"]
        == settings_service.INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT
    )


def test_get_all_settings_batch_clamps_internal_mcp_caps(monkeypatch):
    monkeypatch.setattr(
        settings_service,
        "get_settings_batch",
        lambda _names: {
            settings_service.INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME: 999,
            settings_service.INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME: 99,
        },
    )

    result = settings_service.get_all_settings_batch()

    assert result["internal_mcp_max_tool_invocations"] == 500
    assert result["internal_mcp_tool_batch_cap"] == 20


def test_set_internal_mcp_max_tool_invocations_persists_clamped_value(monkeypatch):
    captured = {}

    def _update_setting(name, value):
        captured["name"] = name
        captured["value"] = value
        return True

    monkeypatch.setattr(settings_service, "update_setting", _update_setting)

    assert settings_service.set_internal_mcp_max_tool_invocations(999) is True
    assert (
        captured["name"]
        == settings_service.INTERNAL_MCP_MAX_TOOL_INVOCATIONS_SETTING_NAME
    )
    assert captured["value"] == 500


def test_set_internal_mcp_tool_batch_cap_persists_clamped_value(monkeypatch):
    captured = {}

    def _update_setting(name, value):
        captured["name"] = name
        captured["value"] = value
        return True

    monkeypatch.setattr(settings_service, "update_setting", _update_setting)

    assert settings_service.set_internal_mcp_tool_batch_cap(0) is True
    assert captured["name"] == settings_service.INTERNAL_MCP_TOOL_BATCH_CAP_SETTING_NAME
    assert captured["value"] == 1


@pytest.mark.parametrize(
    "raw, expected",
    [
        (True, True),
        (False, False),
        ("1", True),
        ("0", False),
        ("true", True),
        ("false", False),
        (1, True),
        (0, False),
        ("not-a-bool", True),
    ],
)
def test_get_auto_proceed_minimal_imposition_enabled_coerces(
    raw: Any, expected: bool, monkeypatch
):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: raw)
    assert settings_service.get_auto_proceed_minimal_imposition_enabled() is expected


def test_get_auto_proceed_minimal_imposition_enabled_uses_env_fallback(monkeypatch):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: None)
    monkeypatch.setenv("VON_AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLE", "0")
    assert settings_service.get_auto_proceed_minimal_imposition_enabled() is False


def test_set_auto_proceed_minimal_imposition_enabled_persists_bool(monkeypatch):
    captured = {}

    def _update_setting(name, value):
        captured["name"] = name
        captured["value"] = value
        return True

    monkeypatch.setattr(settings_service, "update_setting", _update_setting)

    assert (
        settings_service.set_auto_proceed_minimal_imposition_enabled(cast(Any, "yes"))
        is True
    )
    assert (
        captured["name"]
        == settings_service.AUTO_PROCEED_MINIMAL_IMPOSITION_ENABLED_SETTING_NAME
    )
    assert captured["value"] is True


def test_orchestrator_configure_execution_caps_clamps():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=30,
        tool_batch_cap=10,
    )

    orchestrator.configure_execution_caps(
        max_tool_invocations=999,
        tool_batch_cap=999,
        max_missing_tool_call_retries_per_turn=999,
    )

    caps = orchestrator.get_execution_caps()
    assert caps["max_tool_invocations"] == 500
    assert caps["tool_batch_cap"] == 20
    assert caps["max_missing_tool_call_retries_per_turn"] == 20


def test_orchestrator_configure_execution_caps_clamps_lower_bounds():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _StubGateway()),
        max_tool_invocations=30,
        tool_batch_cap=10,
    )

    orchestrator.configure_execution_caps(
        max_tool_invocations=-1,
        tool_batch_cap=0,
    )

    caps = orchestrator.get_execution_caps()
    assert caps["max_tool_invocations"] == 0
    assert caps["tool_batch_cap"] == 1


def test_settings_endpoint_refreshes_in_memory_internal_mcp_caps(monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.secret_key = "test-secret"
    app.register_blueprint(settings_bp, url_prefix="/api/settings")
    recorder = _RecordingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = recorder

    monkeypatch.setattr(
        settings_routes,
        "set_internal_mcp_max_tool_invocations",
        lambda _value: True,
    )
    monkeypatch.setattr(
        settings_routes,
        "set_internal_mcp_tool_batch_cap",
        lambda _value: True,
    )
    monkeypatch.setattr(
        settings_routes, "get_internal_mcp_max_tool_invocations", lambda: 100
    )
    monkeypatch.setattr(settings_routes, "get_internal_mcp_tool_batch_cap", lambda: 10)
    monkeypatch.setattr(settings_routes, "resolve_rag_embedder_setting", lambda: None)
    monkeypatch.setattr(settings_routes, "resolve_rag_llm_setting", lambda: None)
    monkeypatch.setattr(settings_routes, "get_server_default_llm_setting", lambda: None)
    monkeypatch.setattr(
        settings_routes,
        "get_workflow_capability_index_readiness_report",
        lambda: {},
    )

    with app.test_client() as client:
        resp = client.post(
            "/api/settings/",
            json={
                "internal_mcp_max_tool_invocations": 100,
                "internal_mcp_tool_batch_cap": 10,
            },
        )

    assert resp.status_code == 200
    assert recorder.calls == [
        {"max_tool_invocations": 100, "tool_batch_cap": 10}
    ]
