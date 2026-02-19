import asyncio
from typing import Any, cast

from src.backend.integrations.internal_mcp.tool_contract_registry import (
    JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES,
    SURFACE_JIRA_FAMILY_SERVER,
    SURFACE_VONTOLOGY_STDIO,
    SURFACE_VONRAG_STDIO,
    get_canonical_tool_registry,
    get_surface_tool_payloads,
)
from src.backend.mcp_server import mcp_stdio_server
from src.backend.mcp_server import rag_mcp_stdio_server


def test_all_vontology_stdio_handlers_are_canonically_registered() -> None:
    registry_names = set(get_canonical_tool_registry().keys())
    handler_names = set(mcp_stdio_server._TOOL_HANDLERS.keys())
    missing = sorted(handler_names - registry_names)
    assert (
        not missing
    ), f"Vontology stdio handlers missing canonical registry definitions: {missing}"


def test_vonrag_stdio_surface_matches_canonical_registry() -> None:
    async def _invoke_list_tools() -> list[Any]:
        list_tools_fn = cast(Any, rag_mcp_stdio_server.list_tools)
        result = await list_tools_fn()
        return cast(list[Any], result)

    runtime_tools = asyncio.run(_invoke_list_tools())
    runtime_payload = {
        tool.name: {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }
        for tool in runtime_tools
    }
    canonical_payload = {
        item["name"]: item for item in get_surface_tool_payloads(SURFACE_VONRAG_STDIO)
    }

    assert set(runtime_payload.keys()) == set(canonical_payload.keys())
    for name in canonical_payload:
        assert runtime_payload[name] == canonical_payload[name]


def test_surface_only_exceptions_are_explicit() -> None:
    """Tools exposed outside internal catalogue must be explicitly declared."""
    contracts = get_canonical_tool_registry()
    exposed_outside_internal = {
        name
        for name, contract in contracts.items()
        if not contract.exposure.expose_in_internal_catalogue
        and (
            contract.exposure.expose_in_vontology_stdio
            or contract.exposure.expose_in_vonrag_stdio
            or contract.exposure.expose_in_manifest
            or contract.exposure.expose_in_jira_family_server
        )
    }
    assert exposed_outside_internal == {
        "assign_task",
        "audit_concept_text_relations",
        "create_task",
        "find_concepts_by_name",
        "find_subconcepts",
        "get_concept_index_status",
        "get_task",
        "list_my_tasks",
        "update_task_status",
        "von_chat_run",
    }


def test_jira_family_surface_is_sourced_from_canonical_registry() -> None:
    payloads = get_surface_tool_payloads(SURFACE_JIRA_FAMILY_SERVER)
    names = {item["name"] for item in payloads}
    assert names == set(JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES)

    registry = get_canonical_tool_registry()
    for name in names:
        assert name in registry
        assert registry[name].internal_method_name == name


def test_turn_execution_tools_are_exposed_on_vontology_stdio_surface() -> None:
    payloads = get_surface_tool_payloads(SURFACE_VONTOLOGY_STDIO)
    names = {item["name"] for item in payloads}

    expected = {
        "turn_execution_list",
        "turn_execution_get",
        "turn_execution_search_failures",
        "turn_execution_build_benchmark",
        "turn_execution_backfill_from_chat_history",
    }
    assert expected.issubset(names)
    for tool_name in expected:
        assert tool_name in mcp_stdio_server._TOOL_HANDLERS
