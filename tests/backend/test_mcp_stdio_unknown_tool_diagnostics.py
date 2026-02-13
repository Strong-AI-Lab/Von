import asyncio
import json
from typing import Any, cast

from src.backend.mcp_server import mcp_stdio_server


def _call_tool(name: str, arguments: dict) -> dict:
    async def _invoke() -> Any:
        return await mcp_stdio_server.call_tool(name, arguments)

    raw_response = asyncio.run(_invoke())
    response = cast(list[Any], raw_response)
    assert response
    text = getattr(response[0], "text", None)
    assert isinstance(text, str)
    return json.loads(text)


def test_unknown_internal_only_tool_returns_surface_guidance():
    payload = _call_tool("chat_introspect", {})
    assert payload.get("success") is False
    assert payload.get("error_code") == "unknown_tool"

    details = payload.get("error_details") or {}
    surface_diagnostic = details.get("surface_diagnostic") or {}
    assert surface_diagnostic.get("classification") == "internal_mcp_only_tool"
    assert surface_diagnostic.get("recommended_surface") == "internal_mcp_gateway"
    assert (
        surface_diagnostic.get("docs_reference")
        == "docs/engineering/workflow_mcp_capability_matrix.md"
    )

    suggestions = payload.get("suggestions") or []
    assert any("von_chat_run" in str(item) for item in suggestions)


def test_unknown_non_workflow_tool_keeps_generic_guidance():
    payload = _call_tool("totally_unknown_tool", {})
    assert payload.get("success") is False
    assert payload.get("error_code") == "unknown_tool"
    details = payload.get("error_details") or {}
    assert "surface_diagnostic" not in details
