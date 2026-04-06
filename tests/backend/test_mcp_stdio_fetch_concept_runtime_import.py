"""Regression coverage for MCP stdio fetch_concept import/runtime behaviour."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

from src.backend.mcp_server import mcp_stdio_server


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    async def _invoke() -> Any:
        return await mcp_stdio_server.call_tool(name, arguments)

    raw_response = asyncio.run(_invoke())
    response = cast(list[Any], raw_response)
    assert response
    text = getattr(response[0], "text", None)
    assert isinstance(text, str)
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def test_fetch_concept_stdio_returns_payload_without_import_error(monkeypatch):
    """fetch_concept should succeed for valid concepts in stdio script runtime."""

    monkeypatch.setattr(
        mcp_stdio_server,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id, "relationships": {}},
    )
    monkeypatch.setattr(
        mcp_stdio_server,
        "enrich_concept_with_text_relations",
        lambda concept: concept,
    )
    monkeypatch.setattr(
        "src.backend.services.relationship_write_service.detect_vacuous_typing",
        lambda concept: None,
    )

    payload = _call_tool("fetch_concept", {"concept_id": "#V#project"})
    assert payload.get("concept_id") == "#V#project"
    assert payload.get("success") is not False
    assert "error" not in payload


def test_fetch_concept_stdio_not_found_shape_stable(monkeypatch):
    """Not-found responses remain structured and do not leak runtime import errors."""

    monkeypatch.setattr(
        mcp_stdio_server,
        "get_concept_by_concept_id",
        lambda concept_id: None,
    )

    payload = _call_tool("fetch_concept", {"concept_id": "#V#missing_concept"})
    assert payload.get("success") is False
    assert payload.get("error_code") == "concept_not_found"
    assert "relative import" not in str(payload.get("error", "")).lower()


def test_concept_exists_stdio_returns_db_unavailable_when_collection_missing(
    monkeypatch,
):
    monkeypatch.setattr(
        mcp_stdio_server.ConceptsRepository,
        "collection",
        staticmethod(lambda: None),
    )

    payload = _call_tool("concept_exists", {"concept_id": "#V#thing"})

    assert payload.get("success") is False
    assert payload.get("error_code") == "db_unavailable"
    assert (
        payload.get("error_details", {}).get("reason")
        == "concepts_collection_unavailable"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/backend/mcp_server/mcp_stdio_server.py",
        "src/backend/mcp_server/rag_mcp_stdio_server.py",
    ],
)
def test_stdio_server_modules_do_not_use_relative_imports(relative_path: str):
    """Script-run MCP servers must use absolute imports to avoid runtime failures."""

    source = Path(relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)
    relative_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]

    assert not relative_imports, (
        f"{relative_path} contains relative imports that can fail in script runtime: "
        f"{[(node.module, node.lineno) for node in relative_imports]}"
    )
