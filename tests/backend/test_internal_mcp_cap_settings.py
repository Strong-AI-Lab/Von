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
        (999, 50),
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
        (999, 50),
        ("12", 12),
    ],
)
def test_get_internal_mcp_tool_batch_cap_clamps(raw: Any, expected: int, monkeypatch):
    monkeypatch.setattr(settings_service, "get_setting", lambda _name: raw)
    assert settings_service.get_internal_mcp_tool_batch_cap() == expected


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
    assert captured["value"] == 50


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
