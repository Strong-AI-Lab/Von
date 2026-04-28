from __future__ import annotations

from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services import settings_service


class _StubGateway:
    enabled = True

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        raise RuntimeError("not used")


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 30),
        ("", 30),
        ("not-a-number", 30),
        (True, 30),
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
        tool_batch_cap=0,
        max_missing_tool_call_retries_per_turn=999,
    )

    caps = orchestrator.get_execution_caps()
    assert caps["max_tool_invocations"] == 50
    assert caps["tool_batch_cap"] == 1
    assert caps["max_missing_tool_call_retries_per_turn"] == 20
