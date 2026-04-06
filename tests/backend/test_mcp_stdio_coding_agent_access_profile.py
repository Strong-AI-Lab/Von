from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.mcp_server import mcp_stdio_server


def _patch_access_profile(
    monkeypatch,
    *,
    authority_state: str,
    authority_kind: str,
    write_allowed: bool,
    write_mode: str,
    db_name: str,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.coding_agent_mcp_access_profile_service.build_coding_agent_mcp_access_profile",
        lambda: {
            "success": True,
            "profile_id": "coding_agent_vontology_mcp_access",
            "environment": {
                "authority_state": authority_state,
                "authority_kind": authority_kind,
                "configured_database_name": db_name,
            },
            "shared_authority_write_policy": {
                "mode": write_mode,
                "write_category_tools_allowed": write_allowed,
                "reason_codes": [write_mode],
            },
            "von_chat_run_policy": {
                "default_allow_writes": write_allowed,
            },
        },
    )


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _decode_text_payload(result: object) -> dict[str, Any]:
    items = cast(list[Any], result)
    first = items[0]
    return cast(dict[str, Any], json.loads(cast(str, getattr(first, "text"))))


def test_coding_agent_access_profile_gateway_invoke_success_path(
    monkeypatch,
) -> None:
    _patch_access_profile(
        monkeypatch,
        authority_state="test_isolated",
        authority_kind="test_like",
        write_allowed=True,
        write_mode="enabled_test_isolated",
        db_name="test_von_db",
    )
    gateway = _build_gateway()

    result = gateway.invoke("coding_agent_mcp_access_profile", {})
    payload = result.payload

    assert payload["success"] is True
    assert payload["profile_id"] == "coding_agent_vontology_mcp_access"
    assert payload["environment"]["authority_state"] == "test_isolated"


def test_stdio_read_diagnostic_tool_reports_canonical_primary_blocked_write_mode(
    monkeypatch,
) -> None:
    _patch_access_profile(
        monkeypatch,
        authority_state="canonical_primary",
        authority_kind="prod_like",
        write_allowed=False,
        write_mode="blocked_canonical_primary_requires_explicit_approval",
        db_name="von_db",
    )

    async def _runner():
        result = await mcp_stdio_server.call_tool("coding_agent_mcp_access_profile", {})
        return _decode_text_payload(result)

    payload = asyncio.run(_runner())

    assert payload["success"] is True
    assert payload["environment"]["authority_state"] == "canonical_primary"
    assert (
        payload["shared_authority_write_policy"]["write_category_tools_allowed"]
        is False
    )


def test_stdio_write_tool_is_blocked_on_canonical_primary_without_approval(
    monkeypatch,
) -> None:
    _patch_access_profile(
        monkeypatch,
        authority_state="canonical_primary",
        authority_kind="prod_like",
        write_allowed=False,
        write_mode="blocked_canonical_primary_requires_explicit_approval",
        db_name="von_db",
    )

    async def _stub_handler(arguments):
        raise AssertionError("blocked write tool should not reach the handler")

    monkeypatch.setitem(
        mcp_stdio_server._TOOL_HANDLERS,
        "upsert_text_relation",
        _stub_handler,
    )

    async def _runner():
        result = await mcp_stdio_server.call_tool(
            "upsert_text_relation",
            {"concept_id": "#V#x", "predicate": "#V#hasNote", "text": "hello"},
        )
        return _decode_text_payload(result)

    payload = asyncio.run(_runner())

    assert payload["error_code"] == "coding_agent_write_blocked"
    assert (
        payload["error_details"]["access_profile"]["authority_state"]
        == "canonical_primary"
    )


def test_stdio_write_tool_is_allowed_on_noncanonical_local_db(monkeypatch) -> None:
    _patch_access_profile(
        monkeypatch,
        authority_state="local_noncanonical",
        authority_kind="dev_like",
        write_allowed=True,
        write_mode="enabled_noncanonical_local",
        db_name="dev_von_db",
    )

    async def _stub_handler(arguments):
        return [
            mcp_stdio_server._json_text(
                {
                    "success": True,
                    "tool": "upsert_text_relation",
                    "arguments": arguments,
                }
            )
        ]

    monkeypatch.setitem(
        mcp_stdio_server._TOOL_HANDLERS,
        "upsert_text_relation",
        _stub_handler,
    )

    async def _runner():
        result = await mcp_stdio_server.call_tool(
            "upsert_text_relation",
            {"concept_id": "#V#x", "predicate": "#V#hasNote", "text": "hello"},
        )
        return _decode_text_payload(result)

    payload = asyncio.run(_runner())

    assert payload["success"] is True
    assert payload["tool"] == "upsert_text_relation"
    assert payload["arguments"]["text"] == "hello"


def test_stdio_write_tool_is_allowed_in_test_isolated_mode(monkeypatch) -> None:
    _patch_access_profile(
        monkeypatch,
        authority_state="test_isolated",
        authority_kind="test_like",
        write_allowed=True,
        write_mode="enabled_test_isolated",
        db_name="test_von_db",
    )

    async def _stub_handler(arguments):
        return [
            mcp_stdio_server._json_text(
                {
                    "success": True,
                    "tool": "upsert_text_relation",
                    "arguments": arguments,
                }
            )
        ]

    monkeypatch.setitem(
        mcp_stdio_server._TOOL_HANDLERS,
        "upsert_text_relation",
        _stub_handler,
    )

    async def _runner():
        result = await mcp_stdio_server.call_tool(
            "upsert_text_relation",
            {"concept_id": "#V#x", "predicate": "#V#hasNote", "text": "hello"},
        )
        return _decode_text_payload(result)

    payload = asyncio.run(_runner())

    assert payload["success"] is True
    assert payload["tool"] == "upsert_text_relation"
