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
from src.backend.integrations.internal_mcp.schemas import Schema, schema_to_json_schema
from src.backend.mcp_server import mcp_stdio_server
from src.backend.mcp_server import rag_mcp_stdio_server


def _assert_array_schemas_define_items(node: Any, *, path: str) -> None:
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "array" or (
            isinstance(node_type, list) and "array" in node_type
        ):
            assert "items" in node, f"Array schema at {path} is missing items"
        for key, value in node.items():
            _assert_array_schemas_define_items(value, path=f"{path}.{key}")
        return

    if isinstance(node, list):
        for index, value in enumerate(node):
            _assert_array_schemas_define_items(value, path=f"{path}[{index}]")


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


def test_testing_and_turn_execution_tools_are_exposed_on_vontology_stdio_surface() -> None:
    payloads = get_surface_tool_payloads(SURFACE_VONTOLOGY_STDIO)
    names = {item["name"] for item in payloads}

    expected = {
        "turn_execution_list",
        "turn_execution_get",
        "turn_execution_get_diagnostics",
        "turn_execution_get_critic_bundle",
        "turn_execution_search_failures",
        "turn_execution_build_benchmark",
        "turn_execution_build_selector_benchmark",
        "turn_execution_build_dashboard",
        "turn_execution_backfill_from_chat_history",
        "turn_execution_namespace_coverage_report",
        "workflow_build_prediction_envelope",
        "workflow_validate_candidate",
        "workflow_concept_parity_audit",
        "testing_theory_create_slice",
        "testing_theory_import_canonical_context",
        "testing_theory_assert_local_claims",
        "testing_theory_compute_diff",
        "testing_theory_rollback_local_writes",
        "testing_theory_promote_validated_claims",
        "testing_theory_gc_expired",
        "experiment_create_spec",
        "experiment_start_run",
        "experiment_record_observation",
        "experiment_compute_verdict",
        "experiment_emit_learning_signal",
        "experiment_execute_target_workflow",
        "experiment_execute_regression_suite",
        "experiment_run_list",
        "experiment_run_get",
        "episode_critique_build_benchmark",
        "episode_critique_memory_list",
        "episode_critique_memory_get",
        "context_bundle_resolve_effective_context",
        "context_bundle_assemble_context_dossier",
        "context_bundle_update_report_revision",
        "context_bundle_build_reconstructed_workspace",
        "context_bundle_build_benchmark",
        "repo_dossier_file_snapshot",
        "repo_dossier_search",
        "repo_dossier_workflow_definition_get",
        "repo_dossier_prompt_definition_get",
        "repo_dossier_git_metadata",
        "testing_prepare_experiment_spec",
        "testing_prepare_meeting_invitation_spec",
        "testing_prepare_arxiv_paper_ingestion_fixture",
        "testing_verify_arxiv_paper_ingestion_result",
        "testing_cleanup_arxiv_paper_ingestion_artifacts",
    }
    assert expected.issubset(names)
    for tool_name in expected:
        assert tool_name in mcp_stdio_server._TOOL_HANDLERS


def test_schema_to_json_schema_includes_items_for_arrays() -> None:
    payload = schema_to_json_schema(
        Schema(
            required={"names": list},
            optional={"aliases": (list, type(None))},
        )
    )

    assert payload["properties"]["names"]["type"] == "array"
    assert payload["properties"]["names"]["items"] == {}
    assert payload["properties"]["aliases"]["type"] == ["array", "null"]
    assert payload["properties"]["aliases"]["items"] == {}


def test_vontology_stdio_payload_array_schemas_define_items() -> None:
    payloads = get_surface_tool_payloads(SURFACE_VONTOLOGY_STDIO)

    for payload in payloads:
        _assert_array_schemas_define_items(
            payload.get("inputSchema"), path=f"{payload['name']}.inputSchema"
        )
