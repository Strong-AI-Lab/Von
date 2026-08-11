import asyncio
import json
from typing import Any, cast

from src.backend.mcp_server import mcp_stdio_server


def test_stdio_gmail_list_messages_uses_canonical_projection_handler(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_canonical_handler(**kwargs):
        captured.update(kwargs)
        return {
            "messages": [
                {
                    "id": "message-1",
                    "message_id": "message-1",
                    "threadId": "thread-1",
                    "thread_id": "thread-1",
                    "sender": "Airline <travel@example.test>",
                    "subject": "Booking confirmation",
                }
            ],
            "effective_query": {
                "effective_query_string": 'newer_than:2d subject:"booking confirmation"'
            },
        }

    monkeypatch.setattr(
        mcp_stdio_server.internal_mcp_catalogue_module,
        "_gmail_list_messages",
        fake_canonical_handler,
    )

    async def invoke() -> Any:
        return await mcp_stdio_server.call_tool(
            "gmail_list_messages",
            {
                "profile": "zhan-gmail",
                "query": 'newer_than:2d subject:"booking confirmation"',
                "max_results": 3,
                "include_metadata": ["sender", "subject"],
            },
        )

    raw_response = cast(list[Any], asyncio.run(invoke()))
    payload = json.loads(raw_response[0].text)

    assert captured == {
        "profile": "zhan-gmail",
        "query": 'newer_than:2d subject:"booking confirmation"',
        "max_results": 3,
        "include_metadata": ["sender", "subject"],
        "_audit_source": "mcp_stdio",
    }
    assert payload["messages"][0]["sender"] == "Airline <travel@example.test>"
    assert payload["effective_query"]["effective_query_string"].startswith(
        "newer_than:2d"
    )
