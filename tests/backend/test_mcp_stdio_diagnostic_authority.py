"""Local stdio diagnostic authority reaches storage without escaping its call."""

import asyncio
import json

import pytest

from src.backend.integrations.internal_mcp import catalogue, gateway
from src.backend.mcp_server import mcp_stdio_server as stdio
from src.backend.security.access_control import override_current_actor


def decode(blocks):
    return json.loads(blocks[0].text)


@pytest.fixture
def diagnostics(monkeypatch):
    calls = []

    def read(**kwargs):
        calls.append(kwargs)
        return {"success": True, "request_id": kwargs["request_id"]}

    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        read,
    )
    return calls


def test_local_stdio_reads_exact_request_and_does_not_leak_authority(diagnostics):
    async def run():
        result = decode(
            await stdio.call_tool(
                "turn_execution_get_diagnostics", {"request_id": "incident-turn"}
            )
        )
        assert gateway.get_internal_mcp_actor_context_source() is None
        # The same handler outside the local transport still fails closed.
        denied = decode(
            await stdio._handle_turn_execution_get_diagnostics(
                {
                    "request_id": "other-turn",
                    "operator": True,
                    "user_concept_id": "#V#admin",
                }
            )
        )
        return result, denied

    result, denied = asyncio.run(run())
    assert result["request_id"] == "incident-turn"
    assert result["success"] is True
    assert denied["error_code"] == "workflow_global_admin_authority_required"
    assert [call["request_id"] for call in diagnostics] == ["incident-turn"]


def test_stdio_cannot_upgrade_existing_authenticated_actor(monkeypatch, diagnostics):
    monkeypatch.setattr(
        "src.backend.services.von_operational_administrator_service.is_live_von_operational_administrator",
        lambda _actor: False,
    )
    with override_current_actor("#V#ordinary_user", "#V#org"):
        result = decode(
            asyncio.run(
                stdio.call_tool(
                    "turn_execution_get_diagnostics", {"request_id": "foreign-turn"}
                )
            )
        )
    assert result["error_code"] == "workflow_global_admin_authority_required"
    assert diagnostics == []


def test_untrusted_gateway_cannot_request_local_operator_authority(diagnostics):
    with gateway.bind_internal_mcp_actor_context_source("tool_payload_fallback"):
        result = catalogue._turn_execution_get_diagnostics(
            request_id="foreign-turn",
            operator=True,
            user_concept_id="#V#admin",
            namespace="admin",
        )
    assert result["error_code"] == "workflow_global_admin_authority_required"
    assert diagnostics == []


@pytest.mark.parametrize(
    "name",
    [
        "workflow_execute",
        "workflow_cancel_instance",
        "workflow_resume_instance",
        "workflow_retry_instance",
        "workflow_trigger_schedule",
    ],
)
def test_diagnostic_read_does_not_authorise_next_workflow_mutation(
    monkeypatch, diagnostics, name
):
    observed = []
    monkeypatch.setattr(
        stdio, "_evaluate_stdio_write_access", lambda *_: (True, {}, {})
    )

    def mutation(**kwargs):
        observed.append(gateway.get_internal_mcp_actor_context_source())
        return {"success": False, "changed": False}

    monkeypatch.setattr(stdio, "_" + name, mutation)

    async def run():
        await stdio.call_tool(
            "turn_execution_get_diagnostics", {"request_id": "incident-turn"}
        )
        return decode(
            await stdio.call_tool(name, {"instance_id": "instance", "operator": True})
        )

    result = asyncio.run(run())
    assert observed == ["tool_payload_fallback"]
    assert result["changed"] is False


def test_large_local_diagnostic_can_be_reconstructed_from_bounded_pages(monkeypatch):
    import hashlib

    expected = {
        "success": True,
        "instance_id": "incident",
        "workflow_data": {"evidence": '\\"' * 100_000},
    }
    monkeypatch.setattr(stdio, "_workflow_get_instance", lambda **_: expected)
    monkeypatch.setenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS", "10000")

    async def run():
        chunks = []
        offset = 0
        digest = None
        while True:
            blocks = await stdio.call_tool(
                "workflow_get_instance",
                {"instance_id": "incident", "offset": offset, "limit": 20000},
            )
            assert len(blocks[0].text) <= 10000
            page = decode(blocks)["bounded_read"]
            assert digest is None or digest == page["sha256"]
            digest = page["sha256"]
            chunks.append(page["json_chunk"])
            if not page["has_more"]:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        return "".join(chunks), digest

    text, digest = asyncio.run(run())
    assert json.loads(text) == expected
    assert hashlib.sha256(text.encode()).hexdigest() == digest
