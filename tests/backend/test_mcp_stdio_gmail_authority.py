from __future__ import annotations

import asyncio
import json
from typing import cast

from mcp.types import TextContent


def test_stdio_binds_operator_provenance_only_for_direct_gmail_tools(monkeypatch):
    from src.backend.integrations.internal_mcp.gateway import (
        get_internal_mcp_actor_context_source,
    )
    from src.backend.mcp_server import mcp_stdio_server

    observed: list[tuple[str, str | None]] = []

    async def _probe(arguments):
        observed.append(
            (str(arguments.get("probe")), get_internal_mcp_actor_context_source())
        )
        return [mcp_stdio_server._json_text({"success": True})]

    monkeypatch.setitem(
        mcp_stdio_server._TOOL_HANDLERS,
        "gmail_get_message",
        _probe,
    )
    monkeypatch.setitem(
        mcp_stdio_server._TOOL_HANDLERS,
        "search_concepts",
        _probe,
    )

    async def _invoke_probes():
        await mcp_stdio_server.call_tool(
            "gmail_get_message",
            {"probe": "gmail"},
        )
        await mcp_stdio_server.call_tool(
            "search_concepts",
            {"probe": "other"},
        )

    asyncio.run(_invoke_probes())

    assert observed == [
        ("gmail", "trusted_operator_payload_fallback"),
        ("other", None),
    ]


def test_stdio_gmail_get_message_returns_compact_catalogue_projection(monkeypatch):
    from src.backend.integrations.google import gmail_service
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.mcp_server import mcp_stdio_server

    secret_body_data = "raw-body-marker-must-not-survive"
    secret_raw_mime = "raw-mime-marker-must-not-survive"
    monkeypatch.setattr(
        gmail_service,
        "load_profiles_from_env",
        lambda: {
            "operator-profile": gmail_service.GmailProfile(
                profile_id="operator-profile",
                token_path="/nonexistent/operator-profile.json",
            )
        },
    )
    monkeypatch.setattr(
        gmail_service,
        "get_message",
        lambda **_kwargs: {
            "id": "message-1",
            "threadId": "thread-1",
            "snippet": "bounded summary",
            "raw": secret_raw_mime,
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "sender@example.test"},
                    {"name": "Subject", "value": "Projection check"},
                ],
                "body": {"data": secret_body_data, "size": 17},
            },
        },
    )
    monkeypatch.setattr(catalogue, "_gmail_authorised_email", lambda _profile: None)

    async def _invoke_get_message() -> list[TextContent]:
        return cast(
            list[TextContent],
            await mcp_stdio_server.call_tool(
                "gmail_get_message",
                {
                    "profile": "operator-profile",
                    "message_id": "message-1",
                    "format": "full",
                },
            ),
        )

    blocks = asyncio.run(_invoke_get_message())
    payload = json.loads(blocks[0].text)

    assert payload["message_id"] == "message-1"
    assert payload["subject"] == "Projection check"
    assert "raw" not in payload
    assert "payload" not in payload
    assert secret_raw_mime not in blocks[0].text
    assert secret_body_data not in blocks[0].text
