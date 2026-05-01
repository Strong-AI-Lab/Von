#!/usr/bin/env python3
"""
MCP stdio server wrapper for Vontology operations.
This provides a Model Context Protocol interface over stdin/stdout
while reusing the existing Flask endpoint logic.
"""

import sys
import os
import asyncio
import importlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

if TYPE_CHECKING:
    from datetime import datetime, timezone
    from src.backend.vontology.utils_vontology import (
        get_vontology_node_content,
        get_vontology_tree,
        simulate_or_delete_concept,
    )
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_service import (
        enrich_concept_with_text_relations,
        get_concept_by_concept_id,
        get_concept_display_name_with_names_fallback,
        update_concept,
    )
    from src.backend.services.concept_relation_service import (
        build_concept_relations_payload,
        find_relations_with_argument,
        get_predicate_incidence,
    )
    from src.backend.services.concept_usage_profile_service import (
        build_concept_usage_profile,
    )
    from src.backend.services.concept_search_service import search_concepts
    from src.backend.services.concept_embedding_service import (
        get_concept_embedding_stats,
    )
    from src.backend.services.namespace_service import (
        derive_actor_context_from_namespace,
    )
    from src.backend.services.text_value_service import (
        delete_text_relation,
        delete_text_relation_by_predicate_and_text,
        get_text_relations_summary,
        get_texts_for_concept,
        update_text_relation_text,
        upsert_singleton_text_relation,
        upsert_text_for_concept,
    )
    from src.backend.services.text_relation_predicate_validation_service import (
        TextRelationPredicateResolutionError,
        resolve_text_relation_predicate_for_write,
    )
    from src.backend.services.rag_text_relation_change_hook_service import (
        maybe_delete_text_relation_doc_from_rag,
        maybe_sync_concept_text_relations_to_rag,
    )
    from src.backend.services.annotation_extraction_service import extract_annotations
    from src.backend.services.concept_merge_service import merge_concepts
    from src.backend.services.settings_service import (
        INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT,
        INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX,
        INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN,
        INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT,
        get_preferred_language,
        get_setting,
        resolve_llm_setting,
    )
    from src.backend.integrations.internal_mcp.catalogue import (
        _add_relationship,
        _build_paper_recommendations,
        _context_bundle_assemble_context_dossier,
        _context_bundle_build_benchmark,
        _context_bundle_build_reconstructed_workspace,
        _context_bundle_resolve_effective_context,
        _context_bundle_update_report_revision,
        _episode_critique_build_benchmark,
        _episode_critique_memory_get,
        _episode_critique_memory_list,
        _experiment_compute_verdict,
        _experiment_create_spec,
        _experiment_emit_learning_signal,
        _experiment_execute_regression_suite,
        _experiment_execute_target_workflow,
        _experiment_record_observation,
        _experiment_run_get,
        _experiment_run_list,
        _experiment_start_run,
        _jira_add_attachment,
        _jira_add_comment,
        _jira_create_issue,
        _jira_delete_issue_link,
        _jira_get_auth_config,
        _jira_get_bulk_operation_progress,
        _jira_get_issue,
        _jira_get_project_issue_types,
        _jira_get_myself,
        _jira_get_transitions,
        _jira_link_issue,
        _jira_move_issue,
        _jira_search,
        _jira_transition_issue,
        _jira_update_issue,
        _list_recent_screenshots,
        _preview_remove_relationship,
        _remove_relationship,
        _remove_relationships_bulk,
        _renderer_resolve_applicability,
        _repo_dossier_file_snapshot,
        _repo_dossier_git_metadata,
        _repo_dossier_prompt_definition_get,
        _repo_dossier_search,
        _repo_dossier_workflow_definition_get,
        _skill_catalogue_list,
        _skill_catalogue_sync,
        _testing_cleanup_arxiv_paper_ingestion_artifacts,
        _testing_prepare_arxiv_paper_ingestion_fixture,
        _testing_prepare_experiment_spec,
        _testing_prepare_meeting_invitation_spec,
        _testing_theory_assert_local_claims,
        _testing_theory_compute_diff,
        _testing_theory_create_slice,
        _testing_theory_gc_expired,
        _testing_theory_import_canonical_context,
        _testing_theory_promote_validated_claims,
        _testing_theory_rollback_local_writes,
        _chat_history_get_debug_entry,
        _chat_history_get_segments,
        _conversation_telemetry_get_locator,
        _testing_verify_arxiv_paper_ingestion_result,
        _turn_execution_backfill_from_chat_history,
        _turn_execution_build_benchmark,
        _turn_execution_build_context_answering_benchmark,
        _turn_execution_build_dashboard,
        _turn_execution_build_selector_benchmark,
        _turn_execution_get,
        _turn_execution_get_critic_bundle,
        _turn_execution_get_diagnostics,
        _turn_execution_get_live_progress,
        _turn_execution_list,
        _turn_execution_namespace_coverage_report,
        _turn_execution_search_failures,
        _undo_relationship_removal,
        _coding_agent_mcp_access_profile,
        _upsert_renderer_profile,
        _workflow_bind_event,
        _workflow_build_prediction_envelope,
        _workflow_cancel_instance,
        _workflow_create_instance,
        _workflow_create_schedule,
        _workflow_delete_event_binding,
        _workflow_delete_schedule,
        _workflow_execute,
        _workflow_get_execution_trace,
        _workflow_get_instance,
        _workflow_get_schedule,
        _workflow_list_definitions,
        _workflow_validate_candidate,
        _workflow_list_event_bindings,
        _workflow_list_execution_traces,
        _workflow_list_instances,
        _workflow_list_use_episodes,
        _workflow_list_schedules,
        _workflow_concept_parity_audit,
        _workflow_materialisation_diagnostics,
        _workflow_mcp_health_check,
        _workflow_retry_instance,
        _workflow_set_event_binding_enabled,
        _workflow_set_schedule_enabled,
        _workflow_trigger_schedule,
    )
    from src.backend.integrations.internal_mcp.schemas import make_error_response
    from src.backend.integrations.internal_mcp.workflow_surface_capabilities import (
        classify_stdio_missing_tool,
    )
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
        catalogue as internal_mcp_catalogue_module,
    )
    from src.backend.integrations.internal_mcp.tool_contract_registry import (
        SURFACE_VONTOLOGY_STDIO,
        get_surface_tool_payloads,
    )
    from src.backend.integrations.internal_mcp import (
        tool_contract_registry as tool_contract_registry_module,
    )
    from src.backend.integrations.internal_mcp.arxiv_proxy import (
        ArxivProxyError,
        get_arxiv_proxy,
    )
    from src.backend.integrations.internal_mcp.search_proxy_mcp import (
        SearchProxyError,
        get_search_proxy,
    )
    from src.backend.integrations.google import gmail_service
    from src.backend.services.rag_service import RAGBackendUnavailable, get_rag_service

# Avoid UnicodeEncodeError on Windows consoles (default cp1252) when any
# dependency logs Unicode (e.g. checkmarks). MCP runs over stdio; we must not
# crash on encode.
try:
    stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
    stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(stdout_reconfigure):
        stdout_reconfigure(encoding="utf-8", errors="backslashreplace")
    if callable(stderr_reconfigure):
        stderr_reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # pragma: no cover
    pass

# Adjust path to import from the project root
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

activate_mcp_helper_lifecycle = importlib.import_module(
    "src.backend.mcp_server.process_guard"
).activate_mcp_helper_lifecycle

# Register a helper lease and reclaim only safe same-owner stale helpers.
_MCP_HELPER_LIFECYCLE = activate_mcp_helper_lifecycle(
    __file__, log_fn=lambda message: print(message, file=sys.stderr)
)

# Load repo-root .env for MCP stdio runs (VS Code MCP launches may not source .env).
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(dotenv_path=os.path.join(project_root, ".env"), override=False)
except Exception:
    # Safe no-op if python-dotenv isn't installed or .env isn't present.
    pass

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print("Error: MCP package not installed. Run: pdm add mcp", file=sys.stderr)
    sys.exit(1)


def _bind_imports(module_name: str, names: list[str]) -> None:
    module = importlib.import_module(module_name)
    globals().update({name: getattr(module, name) for name in names})


_bind_imports("datetime", ["datetime", "timezone"])
_bind_imports(
    "src.backend.vontology.utils_vontology",
    [
        "get_vontology_node_content",
        "get_vontology_tree",
        "simulate_or_delete_concept",
    ],
)
_bind_imports(
    "src.backend.db.repositories.concepts_repository",
    ["ConceptsRepository"],
)
_bind_imports(
    "src.backend.services.concept_service",
    [
        "get_concept_display_name_with_names_fallback",
        "get_concept_by_concept_id",
        "enrich_concept_with_text_relations",
        "update_concept",
    ],
)
_bind_imports(
    "src.backend.services.concept_relation_service",
    [
        "build_concept_relations_payload",
        "find_relations_with_argument",
        "get_predicate_incidence",
    ],
)
_bind_imports(
    "src.backend.services.concept_usage_profile_service",
    ["build_concept_usage_profile"],
)
_bind_imports("src.backend.services.concept_search_service", ["search_concepts"])
_bind_imports(
    "src.backend.services.concept_embedding_service",
    ["get_concept_embedding_stats"],
)
_bind_imports(
    "src.backend.services.namespace_service",
    ["derive_actor_context_from_namespace"],
)
_bind_imports(
    "src.backend.services.text_value_service",
    [
        "upsert_text_for_concept",
        "get_texts_for_concept",
        "get_text_relations_summary",
        "upsert_singleton_text_relation",
        "update_text_relation_text",
        "delete_text_relation",
        "delete_text_relation_by_predicate_and_text",
    ],
)
_bind_imports(
    "src.backend.services.text_relation_predicate_validation_service",
    [
        "TextRelationPredicateResolutionError",
        "resolve_text_relation_predicate_for_write",
    ],
)
_bind_imports(
    "src.backend.services.rag_text_relation_change_hook_service",
    [
        "maybe_delete_text_relation_doc_from_rag",
        "maybe_sync_concept_text_relations_to_rag",
    ],
)
_bind_imports(
    "src.backend.services.annotation_extraction_service",
    ["extract_annotations"],
)
_bind_imports("src.backend.services.concept_merge_service", ["merge_concepts"])
_bind_imports(
    "src.backend.services.settings_service",
    [
        "resolve_llm_setting",
        "get_preferred_language",
        "get_setting",
        "INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT",
        "INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX",
        "INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN",
        "INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT",
    ],
)
_bind_imports(
    "src.backend.integrations.internal_mcp.catalogue",
    [
        "_add_relationship",
        "_build_paper_recommendations",
        "_context_bundle_assemble_context_dossier",
        "_context_bundle_build_benchmark",
        "_context_bundle_build_reconstructed_workspace",
        "_context_bundle_resolve_effective_context",
        "_context_bundle_update_report_revision",
        "_jira_add_comment",
        "_jira_add_attachment",
        "_jira_create_issue",
        "_jira_delete_issue_link",
        "_jira_get_auth_config",
        "_jira_get_bulk_operation_progress",
        "_jira_get_issue",
        "_jira_get_project_issue_types",
        "_jira_get_myself",
        "_jira_get_transitions",
        "_jira_link_issue",
        "_jira_move_issue",
        "_jira_search",
        "_jira_transition_issue",
        "_jira_update_issue",
        "_list_recent_screenshots",
        "_remove_relationship",
        "_preview_remove_relationship",
        "_remove_relationships_bulk",
        "_skill_catalogue_list",
        "_skill_catalogue_sync",
        "_undo_relationship_removal",
        "_turn_execution_build_benchmark",
        "_turn_execution_build_context_answering_benchmark",
        "_turn_execution_build_selector_benchmark",
        "_turn_execution_build_dashboard",
        "_turn_execution_backfill_from_chat_history",
        "_experiment_compute_verdict",
        "_experiment_create_spec",
        "_experiment_emit_learning_signal",
        "_experiment_execute_regression_suite",
        "_experiment_execute_target_workflow",
        "_episode_critique_build_benchmark",
        "_episode_critique_memory_get",
        "_episode_critique_memory_list",
        "_repo_dossier_file_snapshot",
        "_repo_dossier_git_metadata",
        "_repo_dossier_prompt_definition_get",
        "_repo_dossier_search",
        "_repo_dossier_workflow_definition_get",
        "_experiment_record_observation",
        "_experiment_run_get",
        "_experiment_run_list",
        "_experiment_start_run",
        "_testing_cleanup_arxiv_paper_ingestion_artifacts",
        "_testing_prepare_arxiv_paper_ingestion_fixture",
        "_testing_prepare_experiment_spec",
        "_testing_prepare_meeting_invitation_spec",
        "_testing_verify_arxiv_paper_ingestion_result",
        "_testing_theory_assert_local_claims",
        "_testing_theory_compute_diff",
        "_testing_theory_create_slice",
        "_testing_theory_gc_expired",
        "_testing_theory_import_canonical_context",
        "_testing_theory_promote_validated_claims",
        "_testing_theory_rollback_local_writes",
        "_chat_history_get_debug_entry",
        "_chat_history_get_segments",
        "_conversation_telemetry_get_locator",
        "_turn_execution_get",
        "_turn_execution_get_diagnostics",
        "_turn_execution_get_live_progress",
        "_turn_execution_get_critic_bundle",
        "_turn_execution_list",
        "_turn_execution_search_failures",
        "_turn_execution_namespace_coverage_report",
        "_coding_agent_mcp_access_profile",
        "_renderer_resolve_applicability",
        "_upsert_renderer_profile",
        "_workflow_bind_event",
        "_workflow_cancel_instance",
        "_workflow_build_prediction_envelope",
        "_workflow_create_instance",
        "_workflow_execute",
        "_workflow_create_schedule",
        "_workflow_delete_event_binding",
        "_workflow_delete_schedule",
        "_workflow_get_execution_trace",
        "_workflow_get_instance",
        "_workflow_get_schedule",
        "_workflow_list_definitions",
        "_workflow_validate_candidate",
        "_workflow_list_event_bindings",
        "_workflow_list_execution_traces",
        "_workflow_list_instances",
        "_workflow_list_use_episodes",
        "_workflow_list_schedules",
        "_workflow_concept_parity_audit",
        "_workflow_materialisation_diagnostics",
        "_workflow_mcp_health_check",
        "_workflow_retry_instance",
        "_workflow_set_event_binding_enabled",
        "_workflow_set_schedule_enabled",
        "_workflow_trigger_schedule",
    ],
)
_bind_imports(
    "src.backend.integrations.internal_mcp.schemas",
    ["make_error_response"],
)
_bind_imports(
    "src.backend.integrations.internal_mcp.workflow_surface_capabilities",
    ["classify_stdio_missing_tool"],
)
tool_contract_registry_module = importlib.import_module(
    "src.backend.integrations.internal_mcp.tool_contract_registry"
)
SURFACE_VONTOLOGY_STDIO = tool_contract_registry_module.SURFACE_VONTOLOGY_STDIO
get_surface_tool_payloads = tool_contract_registry_module.get_surface_tool_payloads
_bind_imports(
    "src.backend.integrations.internal_mcp.arxiv_proxy",
    ["get_arxiv_proxy", "ArxivProxyError"],
)
_bind_imports(
    "src.backend.integrations.internal_mcp.search_proxy_mcp",
    ["get_search_proxy", "SearchProxyError"],
)
internal_mcp_catalogue_module = importlib.import_module(
    "src.backend.integrations.internal_mcp.catalogue"
)
internal_mcp_module = importlib.import_module("src.backend.integrations.internal_mcp")
InternalMCPGateway = internal_mcp_module.InternalMCPGateway
InternalMCPTransport = internal_mcp_module.InternalMCPTransport
build_default_catalogue = internal_mcp_module.build_default_catalogue
gmail_service = importlib.import_module("src.backend.integrations.google.gmail_service")
_bind_imports(
    "src.backend.services.rag_service",
    ["get_rag_service", "RAGBackendUnavailable"],
)

# Create MCP server instance
app = Server("vontology-mcp")


_LOG = logging.getLogger(__name__)

_TOOL_LIST_CACHE: list[Tool] | None = None
_TOOL_LIST_CACHE_PATH = (
    Path(project_root) / "data" / "mcp_tool_cache" / "vontology_tools_runtime.json"
)
_TOOL_MANIFEST_PATH = (
    Path(project_root) / "src" / "backend" / "mcp_server" / "vontology_mcp.json"
)
_tool_cache_dependency_paths: list[Path] = [Path(__file__).resolve()]
_INTERNAL_METHOD_DEFINITION_CACHE: dict[str, Any] | None = None
for _dependency_module in (
    tool_contract_registry_module,
    internal_mcp_catalogue_module,
):
    _dependency_file = getattr(_dependency_module, "__file__", None)
    if isinstance(_dependency_file, str) and _dependency_file:
        _tool_cache_dependency_paths.append(Path(_dependency_file).resolve())
_tool_cache_dependency_paths.append(_TOOL_MANIFEST_PATH.resolve())
_TOOL_CACHE_DEPENDENCY_PATHS: tuple[Path, ...] = tuple(_tool_cache_dependency_paths)


def _tool_cache_dependency_signature() -> dict[str, int]:
    signature: dict[str, int] = {}
    for dependency_path in _TOOL_CACHE_DEPENDENCY_PATHS:
        try:
            signature[str(dependency_path)] = dependency_path.stat().st_mtime_ns
        except Exception:
            continue
    return signature


def _tool_cache_enabled() -> bool:
    return os.getenv("VON_MCP_TOOL_LIST_CACHE_ENABLED", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _tool_cache_path() -> Path:
    override = os.getenv("VON_MCP_TOOL_LIST_CACHE_PATH")
    if isinstance(override, str) and override.strip():
        return Path(override.strip())
    return _TOOL_LIST_CACHE_PATH


def _tool_to_cache_payload(tool: Tool) -> dict[str, Any]:
    return {
        "name": str(getattr(tool, "name", "")),
        "description": str(getattr(tool, "description", "")),
        "inputSchema": getattr(tool, "inputSchema", {}) or {},
    }


def _tool_from_surface_payload(tool_payload: dict[str, Any]) -> Tool:
    input_schema = tool_payload.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {}
    return Tool(
        name=str(tool_payload["name"]),
        description=str(tool_payload.get("description") or ""),
        inputSchema=input_schema,
    )


def _parse_tool_payload_list(raw_tools: Any) -> list[Tool]:
    if not isinstance(raw_tools, list):
        return []
    tools: list[Tool] = []
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        description = raw.get("description")
        input_schema = raw.get("inputSchema")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(input_schema, dict):
            input_schema = {}
        tools.append(
            Tool(
                name=name.strip(),
                description=str(description or ""),
                inputSchema=input_schema,
            )
        )
    return tools


def _load_tool_list_cache() -> list[Tool] | None:
    if not _tool_cache_enabled():
        return None
    try:
        cache_path = _tool_cache_path()
        if not cache_path.exists():
            return None

        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None

        dependency_signature = payload.get("dependency_signature")
        expected_signature = _tool_cache_dependency_signature()
        if isinstance(dependency_signature, dict):
            normalised_signature: dict[str, int] = {}
            for key, value in dependency_signature.items():
                if isinstance(key, str) and isinstance(value, int):
                    normalised_signature[key] = value
            if normalised_signature != expected_signature:
                return None
        else:
            # Backward compatibility with older cache payloads that only tracked
            # this file. New payloads include dependency_signature and should
            # invalidate when registry/catalogue modules change.
            source_mtime_ns = payload.get("source_file_mtime_ns")
            current_mtime_ns = Path(__file__).resolve().stat().st_mtime_ns
            if (
                not isinstance(source_mtime_ns, int)
                or source_mtime_ns != current_mtime_ns
            ):
                return None

        tools = _parse_tool_payload_list(payload.get("tools"))
        if tools:
            return tools
    except Exception as exc:  # pragma: no cover - best-effort diagnostics cache
        _LOG.debug("Could not load MCP tool cache: %s", exc)
    return None


def _load_tool_list_manifest() -> list[Tool] | None:
    """Load tool contracts from the static manifest as a fast startup fallback.

    This avoids heavy canonical-registry construction on cold starts, which can
    otherwise cause MCP clients to time out and report transport drops before
    list_tools returns.
    """
    try:
        if not _TOOL_MANIFEST_PATH.exists():
            return None
        payload = json.loads(_TOOL_MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        tools = _parse_tool_payload_list(payload.get("tools"))
        if tools:
            return tools
    except Exception as exc:  # pragma: no cover - startup reliability fallback
        _LOG.debug("Could not load MCP tool manifest fallback: %s", exc)
    return None


def _persist_tool_list_cache(tools: list[Tool]) -> None:
    if not _tool_cache_enabled():
        return
    try:
        source_path = Path(__file__).resolve()
        payload = {
            "cached_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "src/backend/mcp_server/mcp_stdio_server.py:list_tools",
            "source_file": str(source_path),
            "source_file_mtime_ns": source_path.stat().st_mtime_ns,
            "dependency_signature": _tool_cache_dependency_signature(),
            "tool_count": len(tools),
            "tools": [_tool_to_cache_payload(tool) for tool in tools],
        }
        cache_path = _tool_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - best-effort diagnostics cache
        _LOG.debug("Could not persist MCP tool cache: %s", exc)


def _truthy_env(var_name: str) -> bool:
    return os.getenv(var_name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _coerce_bool_argument(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _get_internal_method_definition(tool_name: str) -> Any | None:
    global _INTERNAL_METHOD_DEFINITION_CACHE

    if _INTERNAL_METHOD_DEFINITION_CACHE is None:
        try:
            catalogue = build_default_catalogue()
            _INTERNAL_METHOD_DEFINITION_CACHE = {
                name: catalogue.get(name) for name in catalogue.list_methods()
            }
        except Exception:
            _INTERNAL_METHOD_DEFINITION_CACHE = {}

    return (_INTERNAL_METHOD_DEFINITION_CACHE or {}).get(tool_name)


def _get_stdio_tool_category(tool_name: str) -> str | None:
    try:
        registry = tool_contract_registry_module.get_canonical_tool_registry()
        contract = registry.get(tool_name)
    except Exception:
        contract = None
    if contract is None:
        return None
    category = getattr(contract, "category", None)
    if not isinstance(category, str):
        return None
    category = category.strip()
    return category or None


def _preview_safe_write_call_requested(
    tool_name: str,
    arguments: dict[str, Any],
) -> bool:
    if tool_name == "von_chat_run":
        return _coerce_bool_argument(arguments.get("dry_run"), default=True)

    definition = _get_internal_method_definition(tool_name)
    write_guardrail = getattr(definition, "write_guardrail", None)
    if not isinstance(write_guardrail, dict):
        return False

    preview_param = str(write_guardrail.get("preview_safe_dry_run_param") or "").strip()
    if not preview_param:
        return False

    preview_default = _coerce_bool_argument(
        write_guardrail.get("preview_safe_dry_run_default"),
        default=False,
    )
    return _coerce_bool_argument(arguments.get(preview_param), default=preview_default)


def _build_access_profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    environment = profile.get("environment")
    write_policy = profile.get("shared_authority_write_policy")
    von_chat_run_policy = profile.get("von_chat_run_policy")
    environment = environment if isinstance(environment, dict) else {}
    write_policy = write_policy if isinstance(write_policy, dict) else {}
    von_chat_run_policy = (
        von_chat_run_policy if isinstance(von_chat_run_policy, dict) else {}
    )
    return {
        "profile_id": profile.get("profile_id"),
        "authority_state": environment.get("authority_state"),
        "authority_kind": environment.get("authority_kind"),
        "configured_database_name": environment.get("configured_database_name"),
        "write_mode": write_policy.get("mode"),
        "write_category_tools_allowed": write_policy.get(
            "write_category_tools_allowed"
        ),
        "von_chat_run_default_allow_writes": von_chat_run_policy.get(
            "default_allow_writes"
        ),
    }


def _evaluate_stdio_write_access(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    from src.backend.services.coding_agent_mcp_access_profile_service import (
        build_coding_agent_mcp_access_profile,
    )

    profile = build_coding_agent_mcp_access_profile()
    write_policy = profile.get("shared_authority_write_policy")
    write_policy = write_policy if isinstance(write_policy, dict) else {}
    allowed = bool(write_policy.get("write_category_tools_allowed"))
    if allowed or _preview_safe_write_call_requested(tool_name, arguments):
        return True, profile, write_policy
    return False, profile, write_policy


_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "token",
)


def _truncate_string(value: str, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + f"\n... [truncated {len(value) - max_chars} chars]"


def _redact_debug_value(value: Any, *, max_string_chars: int) -> Any:
    if isinstance(value, str):
        return _truncate_string(value, max_chars=max_string_chars)
    if isinstance(value, list):
        return [
            _redact_debug_value(item, max_string_chars=max_string_chars)
            for item in value
        ]
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            key_str = str(key)
            key_lower = key_str.lower()
            if any(fragment in key_lower for fragment in _SENSITIVE_KEY_FRAGMENTS):
                redacted[key_str] = "[redacted]"
            else:
                redacted[key_str] = _redact_debug_value(
                    inner, max_string_chars=max_string_chars
                )
        return redacted
    return value


class VonChatRunTimeout(TimeoutError):
    def __init__(
        self, *, timeout_seconds: float, pid: int, thread_id: int | None
    ) -> None:
        super().__init__(f"Timed out after {timeout_seconds:.0f}s.")
        self.timeout_seconds = timeout_seconds
        self.pid = pid
        self.thread_id = thread_id


async def _run_blocking_with_timeout(func, *, timeout_seconds: float):
    """Run a blocking callable in a worker thread with an overall timeout.

    Returns the callable's result. On timeout, raises VonChatRunTimeout carrying
    the current process PID and the worker thread ID (if captured).

    Export for testing.
    """

    import asyncio
    import concurrent.futures
    import os
    import threading

    pid = os.getpid()
    thread_id_holder: dict[str, int | None] = {"thread_id": None}

    def _wrapped():
        thread_id_holder["thread_id"] = threading.get_ident()
        return func()

    # Use a dedicated executor so we can reliably capture the worker thread ID.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="von_chat_run"
    ) as executor:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(executor, _wrapped)
        try:
            if timeout_seconds <= 0:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise VonChatRunTimeout(
                timeout_seconds=timeout_seconds,
                pid=pid,
                thread_id=thread_id_holder["thread_id"],
            ) from exc


class _RestrictedGateway:
    """Gateway wrapper used by `von_chat_run`.

    When writes are not allowed, blocks non-read-category tools.
    """

    def __init__(self, *, gateway: Any, allow_writes: bool) -> None:
        self._gateway: Any = gateway
        self._allow_writes = bool(allow_writes)

    @property
    def enabled(self) -> bool:
        return self._gateway.enabled

    def describe_methods(self) -> dict[str, dict[str, Any]]:
        return self._gateway.describe_methods()

    def invoke(self, method_name: str, payload: dict[str, Any] | None = None):
        if not self._allow_writes:
            meta = self._gateway.describe_methods().get(method_name) or {}
            category = meta.get("category")
            if category != "read":
                raise PermissionError(
                    "Write tools are disabled for von_chat_run. "
                    "Re-run with allow_writes=true and set VON_MCP_ALLOW_WRITES=1."
                )
        return self._gateway.invoke(method_name, payload)


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    global _TOOL_LIST_CACHE
    if _TOOL_LIST_CACHE is not None:
        return list(_TOOL_LIST_CACHE)

    disk_cached_tools = _load_tool_list_cache()
    if disk_cached_tools is not None:
        _TOOL_LIST_CACHE = list(disk_cached_tools)
        return list(_TOOL_LIST_CACHE)

    manifest_tools = _load_tool_list_manifest()
    if manifest_tools is not None:
        _TOOL_LIST_CACHE = list(manifest_tools)
        _persist_tool_list_cache(_TOOL_LIST_CACHE)
        return list(_TOOL_LIST_CACHE)

    tools = [
        _tool_from_surface_payload(tool_payload)
        for tool_payload in get_surface_tool_payloads(SURFACE_VONTOLOGY_STDIO)
    ]

    _TOOL_LIST_CACHE = list(tools)
    _persist_tool_list_cache(_TOOL_LIST_CACHE)
    return list(_TOOL_LIST_CACHE)


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:  # type: ignore[misc]
    """Handle tool calls by delegating to specific handlers."""

    parsed_arguments = arguments if isinstance(arguments, dict) else {}
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        surface_diagnostic = classify_stdio_missing_tool(name)
        details: dict[str, Any] = {"requested_tool": name}
        suggestions = [
            "Use list_tools to see available MCP tools",
            "Check tool name spelling (common: search_concepts, create_concepts, fetch_concept)",
        ]
        if isinstance(surface_diagnostic, dict):
            details["surface_diagnostic"] = surface_diagnostic
            diagnostic_suggestions = surface_diagnostic.get("suggestions")
            if isinstance(diagnostic_suggestions, list) and diagnostic_suggestions:
                suggestions = [str(item) for item in diagnostic_suggestions]

        return [
            _json_error(
                f"Unknown tool: {name}",
                error_code="unknown_tool",
                details=details,
                suggestions=suggestions,
            )
        ]

    if _get_stdio_tool_category(name) == "write":
        write_allowed, access_profile, write_policy = _evaluate_stdio_write_access(
            name,
            parsed_arguments,
        )
        if not write_allowed:
            raw_reason_codes = write_policy.get("reason_codes")
            reason_codes = (
                raw_reason_codes if isinstance(raw_reason_codes, list) else []
            )
            details = {
                "tool": name,
                "access_profile": _build_access_profile_summary(access_profile),
                "write_reason_codes": list(reason_codes),
            }
            suggestions = [
                "Use coding_agent_mcp_access_profile to inspect the current authority state and write mode.",
                "If this targets the canonical primary DB, set VON_MCP_ALLOW_WRITES=1 only when that write path is explicitly approved.",
            ]
            if _preview_safe_write_call_requested(name, {}):
                suggestions.insert(
                    0,
                    "Use the preview-safe dry-run mode for this tool if you only need inspection or planning output.",
                )
            return [
                _json_error(
                    (
                        f"Write-category tool '{name}' is blocked by the current "
                        "coding-agent MCP access profile."
                    ),
                    error_code="coding_agent_write_blocked",
                    details=details,
                    suggestions=suggestions,
                )
            ]

    try:
        return await handler(parsed_arguments)
    except Exception as exc:  # Defensive: avoid crashing the stdio server
        return [
            _json_error(
                str(exc),
                error_code="tool_execution_error",
                details={"tool": name, "exception_type": type(exc).__name__},
                suggestions=[
                    "Check parameter values and types",
                    "Verify concept IDs exist",
                ],
            )
        ]


def _json_text(payload: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, indent=2, default=str))


def _json_error(
    message: str,
    *,
    error_code: str = "error",
    details: dict[str, Any] | None = None,
    suggestions: list[str] | None = None,
    related_concept_ids: list[str] | None = None,
) -> TextContent:
    """Create a standardised MCP error response as TextContent.

    Uses the shared make_error_response for consistent error structure
    across both stdio and internal MCP gateways (JVNAUTOSCI-692).
    """
    return _json_text(
        make_error_response(
            error_code=error_code,
            message=message,
            details=details,
            suggestions=suggestions,
            related_concept_ids=related_concept_ids,
        )
    )


async def _handle_get_context(arguments: dict[str, Any]) -> list[TextContent]:
    # Get user/org context from arguments (stdio server has no session)
    user_concept_id = arguments.get("user_concept_id")
    org_concept_id = arguments.get("organisation_concept_id") or arguments.get(
        "organization_concept_id"
    )

    model_setting = resolve_llm_setting(
        user_concept_id=user_concept_id, org_concept_id=org_concept_id
    )
    context = {
        "llm_model": (
            model_setting.get("model") if isinstance(model_setting, dict) else None
        ),
        "llm_provider": (
            model_setting.get("provider") if isinstance(model_setting, dict) else None
        ),
        "language_preference": get_preferred_language(),
        "fetch_counts_on_load": get_setting("fetch_counts_on_load"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "User and organisation context managed client-side (localStorage) per JVNAUTOSCI-628",
    }
    return [_json_text(context)]


async def _handle_create_concepts(arguments: dict[str, Any]) -> list[TextContent]:
    # Route through the internal catalogue handler to keep stdio and gateway
    # semantics identical for parent resolution, duplicate policy, and scoping.
    return [_json_text(internal_mcp_catalogue_module._create_concepts(**arguments))]


async def _handle_find_subconcepts(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id of the type whose subtypes you want to find",
                    "Use search_concepts to find the parent type concept_id first",
                ],
            )
        ]

    cursor = ConceptsRepository.find(
        {"relationships.is_a_type_of": concept_id},
        {"concept_id": 1, "name": 1, "names": 1, "computed_kind": 1},
    )

    subconcepts = []
    for doc in cursor:
        cid = doc.get("concept_id")
        if not cid:
            continue
        try:
            name = get_concept_display_name_with_names_fallback(doc)
        except Exception:
            name = doc.get("name") or cid
        kind = doc.get("computed_kind") or "individual"
        subconcepts.append({"id": cid, "name": name, "kind": kind})

    return [_json_text(subconcepts)]


async def _handle_find_concepts_by_name(arguments: dict[str, Any]) -> list[TextContent]:
    name_substring = arguments.get("name")
    if not name_substring:
        return [
            _json_error(
                "Missing name parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide a name or substring to search for",
                    "Use search_concepts for more flexible semantic search",
                ],
            )
        ]

    search_result = search_concepts(
        query=name_substring,
        match_type="substring",
        include_description=False,
        limit=100,
    )

    matching_concepts = [
        {
            "id": result.get("concept_id"),
            "name": result.get("name"),
            "kind": result.get("kind", "individual"),
        }
        for result in search_result.get("results", [])
    ]
    return [_json_text(matching_concepts)]


async def _handle_von_chat_run(arguments: dict[str, Any]) -> list[TextContent]:
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
        ToolCallParsingError,
    )
    from src.backend.languagemodels.llm_interface import (
        get_active_model_name,
        get_llm_client,
    )

    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return [
            _json_text(
                {"success": False, "error": "Missing required parameter: prompt"}
            )
        ]

    if not _truthy_env("VON_INTERNAL_MCP_ENABLE"):
        return [
            _json_text(
                {
                    "success": False,
                    "error": "Internal MCP is disabled. Set VON_INTERNAL_MCP_ENABLE=1 to use von_chat_run.",
                }
            )
        ]

    model_override = arguments.get("model")
    model_name = (
        model_override
        if isinstance(model_override, str) and model_override.strip()
        else get_active_model_name()
    )

    user_namespace = (
        arguments.get("user_namespace")
        if isinstance(arguments.get("user_namespace"), str)
        else None
    )
    user_concept_id = (
        arguments.get("user_concept_id")
        if isinstance(arguments.get("user_concept_id"), str)
        else None
    )
    org_concept_id = (
        arguments.get("org_concept_id")
        if isinstance(arguments.get("org_concept_id"), str)
        else None
    )
    if org_concept_id is None and isinstance(
        arguments.get("organisation_concept_id"), str
    ):
        org_concept_id = arguments.get("organisation_concept_id")
    if user_concept_id is None or org_concept_id is None:
        namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(
            user_namespace
        )
        if user_concept_id is None:
            user_concept_id = namespace_user_id
        if org_concept_id is None:
            org_concept_id = namespace_org_id
    gmail_profile = (
        arguments.get("gmail_profile")
        if isinstance(arguments.get("gmail_profile"), str)
        else None
    )
    auxiliary_system_prompt = (
        arguments.get("auxiliary_system_prompt")
        if isinstance(arguments.get("auxiliary_system_prompt"), str)
        else None
    )

    try:
        max_tool_invocations = int(
            arguments.get(
                "max_tool_invocations", INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
            )
        )
    except Exception:
        max_tool_invocations = INTERNAL_MCP_MAX_TOOL_INVOCATIONS_DEFAULT
    max_tool_invocations = max(
        INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MIN,
        min(INTERNAL_MCP_MAX_TOOL_INVOCATIONS_MAX, max_tool_invocations),
    )

    write_access_allowed, access_profile, _write_policy = _evaluate_stdio_write_access(
        "von_chat_run",
        arguments,
    )
    access_profile_summary = _build_access_profile_summary(access_profile)

    raw_allow_writes = arguments.get("allow_writes")
    allow_writes_requested = (
        _coerce_bool_argument(raw_allow_writes, default=write_access_allowed)
        if raw_allow_writes is not None
        else write_access_allowed
    )
    raw_dry_run = arguments.get("dry_run")
    dry_run = (
        _coerce_bool_argument(raw_dry_run, default=not allow_writes_requested)
        if raw_dry_run is not None
        else not allow_writes_requested
    )

    if allow_writes_requested and not write_access_allowed and not dry_run:
        return [
            _json_text(
                {
                    "success": False,
                    "error": (
                        "Write-category tools are blocked by the current coding-agent "
                        "MCP access profile."
                    ),
                    "access_profile": access_profile_summary,
                }
            )
        ]
    effective_allow_writes = allow_writes_requested and not dry_run

    try:
        max_string_chars = int(arguments.get("max_string_chars", 8000))
    except Exception:
        max_string_chars = 8000
    max_string_chars = max(256, min(20000, max_string_chars))

    try:
        max_context_chars = int(arguments.get("max_context_chars", 120000))
    except Exception:
        max_context_chars = 120000
    max_context_chars = max(4000, min(2_000_000, max_context_chars))

    try:
        max_tool_result_chars = int(arguments.get("max_tool_result_chars", 20000))
    except Exception:
        max_tool_result_chars = 20000
    max_tool_result_chars = max(2000, min(1_000_000, max_tool_result_chars))

    try:
        max_tool_result_field_chars = int(
            arguments.get("max_tool_result_field_chars", 8000)
        )
    except Exception:
        max_tool_result_field_chars = 8000
    max_tool_result_field_chars = max(1000, min(200_000, max_tool_result_field_chars))

    try:
        timeout_seconds = float(arguments.get("timeout_seconds", 90))
    except Exception:
        timeout_seconds = 90.0
    timeout_seconds = max(1.0, min(600.0, timeout_seconds))

    raw_context = arguments.get("context")
    context = raw_context if isinstance(raw_context, list) else None

    llm_client = get_llm_client()
    catalogue = build_default_catalogue()
    transport = InternalMCPTransport()
    base_gateway = InternalMCPGateway(
        catalogue=catalogue, transport=transport, enabled=True
    )
    gateway = _RestrictedGateway(
        gateway=base_gateway, allow_writes=effective_allow_writes
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        logger=_LOG.getChild("von_chat_run"),
        max_tool_invocations=max_tool_invocations,
        tool_batch_cap=INTERNAL_MCP_TOOL_BATCH_CAP_DEFAULT,
        default_gmail_profile=None,
        max_context_chars=max_context_chars,
        max_tool_result_chars=max_tool_result_chars,
        max_tool_result_field_chars=max_tool_result_field_chars,
    )

    try:

        def _run_orchestrator_sync():
            return orchestrator.run(
                prompt=prompt,
                context=context,
                llm_client=llm_client,
                model=model_name,
                user_namespace=user_namespace,
                gmail_profile=gmail_profile,
                auxiliary_system_prompt=auxiliary_system_prompt,
                preferred_language=get_preferred_language(),
                user_concept_id=user_concept_id,
                org_concept_id=org_concept_id,
            )

        orchestrator_result = await _run_blocking_with_timeout(
            _run_orchestrator_sync,
            timeout_seconds=timeout_seconds,
        )
        payload = {
            "success": True,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
            "access_profile": access_profile_summary,
            "timeout_seconds": timeout_seconds,
            "response_text": _truncate_string(
                orchestrator_result.response_text, max_chars=max_string_chars
            ),
            "tool_invocations": _redact_debug_value(
                list(orchestrator_result.tool_invocations),
                max_string_chars=max_string_chars,
            ),
            "tool_messages": _redact_debug_value(
                list(orchestrator_result.extra_messages),
                max_string_chars=max_string_chars,
            ),
        }
    except VonChatRunTimeout as exc:
        payload = {
            "success": False,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
            "access_profile": access_profile_summary,
            "timeout_seconds": timeout_seconds,
            "error": str(exc),
            "timeout_debug": {
                "pid": exc.pid,
                "thread_id": exc.thread_id,
                "note": "If this remains stuck, terminate the MCP server process by PID. Python threads cannot be safely killed directly.",
            },
        }
    except ToolCallParsingError as exc:
        payload = {
            "success": False,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
            "access_profile": access_profile_summary,
            "timeout_seconds": timeout_seconds,
            "error": f"Tool call parsing error: {exc}",
        }
    except Exception as exc:
        payload = {
            "success": False,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
            "access_profile": access_profile_summary,
            "timeout_seconds": timeout_seconds,
            "error": str(exc),
        }

    return [_json_text(payload)]


async def _handle_upsert_text_relation(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle upsert_text_relation tool call."""
    namespace = (
        arguments.get("namespace")
        if isinstance(arguments.get("namespace"), str)
        else None
    )
    concept_id = arguments.get("concept_id")
    predicate = arguments.get("predicate")
    text = arguments.get("text")
    language = arguments.get("language", "en-NZ")
    context = arguments.get("context")

    if not concept_id:
        return [_json_text({"success": False, "error": "Missing concept_id parameter"})]
    if not predicate:
        return [_json_text({"success": False, "error": "Missing predicate parameter"})]
    if not text:
        return [_json_text({"success": False, "error": "Missing text parameter"})]

    try:
        predicate_resolution = resolve_text_relation_predicate_for_write(predicate)
        storage_predicate = predicate_resolution.storage_predicate
        result = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=storage_predicate,
            text=text,
            lang=language,
            context=context,
        )

        maybe_sync_concept_text_relations_to_rag(
            namespace=namespace,
            concept_id=concept_id,
            predicate=storage_predicate,
        )

        text_preview = text[:100] + "..." if len(text) > 100 else text
        payload = {
            "success": True,
            "text_value_id": str(result.get("text_value_id")),
            "relation_id": str(result.get("relation_id")),
            "relation_created": result.get("relation_created"),
            "predicate": storage_predicate,
            "input_predicate": predicate_resolution.input_predicate,
            "predicate_concept_id": predicate_resolution.predicate_concept_id,
            "text_preview": text_preview,
            "language": language,
        }
        return [_json_text(payload)]
    except TextRelationPredicateResolutionError as exc:
        return [
            _json_error(
                str(exc),
                error_code=exc.error_code,
                details=exc.details,
                suggestions=exc.suggestions,
            )
        ]
    except Exception as exc:
        return [
            _json_text(
                {"success": False, "error": f"Failed to upsert text relation: {exc}"}
            )
        ]


async def _handle_get_text_relations(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle get_text_relations tool call."""
    concept_id = arguments.get("concept_id")
    predicate = arguments.get("predicate")
    language = arguments.get("language")
    limit = arguments.get("limit", 50)

    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to get text relations for",
                    "Use search_concepts to find the concept first",
                ],
            )
        ]

    try:
        relations = get_texts_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            lang=language,
            limit=limit,
        )

        # Add text previews for long content
        for relation in relations:
            text = relation.get("text", "")
            if len(text) > 200:
                relation["text_preview"] = text[:200] + "..."

        payload = {
            "concept_id": concept_id,
            "relations_found": len(relations),
            "relations": relations,
        }
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to get text relations: {exc}",
                error_code="operation_failed",
                suggestions=["Verify the concept_id exists"],
                related_concept_ids=[concept_id],
            )
        ]


def _warn_if_underscore_replaced(old_text: str, new_text: str) -> list[str]:
    if not old_text or not new_text:
        return []
    if "_" in old_text and " " in new_text and old_text.replace("_", " ") == new_text:
        return [
            "new_text replaces underscores with spaces; avoid renaming text values unless explicitly intended"
        ]
    return []


async def _handle_update_text_relation(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle update_text_relation tool call."""
    namespace = (
        arguments.get("namespace")
        if isinstance(arguments.get("namespace"), str)
        else None
    )
    concept_id = arguments.get("concept_id")
    relation_id = arguments.get("relation_id")
    new_text = arguments.get("new_text")
    language = arguments.get("language", "en-NZ")

    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id that owns the text relation",
                    "Use fetch_concept with include_text_relations_arg1=true to see available relations",
                ],
            )
        ]
    if not relation_id:
        return [
            _json_error(
                "Missing relation_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the relation_id of the text relation to update",
                    "Use fetch_concept with include_text_relations_arg1=true to find relation IDs",
                ],
            )
        ]
    if not new_text:
        return [
            _json_error(
                "Missing new_text parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the new_text content to replace the existing text"
                ],
            )
        ]

    try:
        result = update_text_relation_text(
            subject_concept_id=concept_id,
            relation_id=relation_id,
            new_text=new_text,
            lang=language,
        )

        maybe_sync_concept_text_relations_to_rag(
            namespace=namespace,
            concept_id=concept_id,
        )

        old_text = str(result.get("old_text", ""))
        old_preview = old_text[:100]
        new_preview = new_text[:100] + "..." if len(new_text) > 100 else new_text
        warnings = _warn_if_underscore_replaced(old_text, new_text)

        payload = {
            "success": True,
            "relation_id": relation_id,
            "old_text_preview": old_preview,
            "new_text_preview": new_preview,
            "text_value_id": str(result.get("text_value_id")),
        }
        if warnings:
            payload["warnings"] = warnings
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to update text relation: {exc}",
                error_code="update_failed",
                suggestions=[
                    "Verify the concept_id and relation_id exist",
                    "Check that new_text is a valid string",
                ],
                related_concept_ids=[concept_id] if concept_id else None,
            )
        ]


async def _handle_delete_text_relation(arguments: dict[str, Any]) -> list[TextContent]:
    """Handle delete_text_relation tool call."""
    namespace = (
        arguments.get("namespace")
        if isinstance(arguments.get("namespace"), str)
        else None
    )
    concept_id = arguments.get("concept_id")
    relation_id = arguments.get("relation_id")
    predicate = arguments.get("predicate")
    text = arguments.get("text")
    language = arguments.get("language")
    garbage_collect = bool(arguments.get("garbage_collect"))

    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id that owns the text relation",
                    "Use fetch_concept with include_text_relations_arg1=true to see available relations",
                ],
            )
        ]

    try:
        if relation_id:
            # Delete by relation ID (preferred)
            result = delete_text_relation(
                subject_concept_id=concept_id,
                relation_id=relation_id,
                garbage_collect=garbage_collect,
            )

            maybe_delete_text_relation_doc_from_rag(
                namespace=namespace,
                relation_id=relation_id,
            )

            payload = {
                "success": True,
                "deleted_relation_id": relation_id,
                "deleted_text_preview": None,
                "text_value_cleaned_up": bool(
                    result.get(
                        "orphaned_text_value_deleted",
                        result.get("text_value_cleaned_up", False),
                    )
                ),
            }
        elif predicate and text:
            # Delete by predicate + text match
            result = delete_text_relation_by_predicate_and_text(
                subject_concept_id=concept_id,
                predicate=predicate,
                text=text,
                lang=language,
                garbage_collect=garbage_collect,
            )

            maybe_delete_text_relation_doc_from_rag(
                namespace=namespace,
                relation_id=result.get("relation_id"),
            )

            payload = {
                "success": True,
                "deleted_relation_id": result.get("relation_id"),
                "deleted_text_preview": text[:100],
                "text_value_cleaned_up": result.get(
                    "orphaned_text_value_deleted", False
                ),
            }
        else:
            return [
                _json_error(
                    "Must provide either relation_id or both predicate and text",
                    error_code="invalid_parameter",
                    suggestions=[
                        "Provide relation_id for exact deletion",
                        "Or provide both predicate and text for pattern match deletion",
                    ],
                )
            ]

        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to delete text relation: {exc}",
                error_code="operation_failed",
                suggestions=["Verify the concept_id and identifiers are correct"],
                related_concept_ids=[concept_id] if concept_id else None,
            )
        ]


async def _handle_add_names(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    names = arguments.get("names")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id of the concept to add names to",
                    "Use search_concepts to find the concept first",
                ],
            )
        ]
    if not names or not isinstance(names, list):
        return [
            _json_error(
                "Missing or invalid names array",
                error_code="invalid_parameter",
                suggestions=[
                    "Provide names as an array of strings or objects with name, language, name_type",
                    "Example: ['Name1', {'name': 'Name2', 'language': 'en-NZ', 'name_type': 'NL'}]",
                ],
            )
        ]

    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not concept:
        return [
            _json_error(
                f"Concept '{concept_id}' not found",
                error_code="concept_not_found",
                suggestions=[
                    "Check the concept_id spelling",
                    "Use search_concepts to verify the concept exists",
                ],
                related_concept_ids=[concept_id],
            )
        ]

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for idx, name_obj in enumerate(names):
        if isinstance(name_obj, str):
            name_text = name_obj
            language = "en-NZ"
            name_type = "NL"
        elif isinstance(name_obj, dict):
            name_text = name_obj.get("name")
            language = name_obj.get("language", "en-NZ")
            name_type = name_obj.get("name_type", "NL")
        else:
            errors.append({"index": idx, "error": "Invalid name format"})
            continue

        if not name_text or not isinstance(name_text, str) or not name_text.strip():
            errors.append({"index": idx, "error": "Missing or invalid name text"})
            continue

        try:
            result = upsert_text_for_concept(
                subject_concept_id=concept_id,
                predicate="hasName",
                text=name_text.strip(),
                lang=language,
                context={"name_type": name_type},
            )
            if result:
                results.append(
                    {
                        "index": idx,
                        "name": name_text.strip(),
                        "language": language,
                        "name_type": name_type,
                        "text_value_id": str(result.get("text_value_id")),
                        "relation_id": str(result.get("relation_id")),
                    }
                )
            else:
                errors.append(
                    {"index": idx, "name": name_text, "error": "Failed to add"}
                )
        except Exception as exc:
            errors.append({"index": idx, "name": name_text, "error": str(exc)})

    payload = {
        "success": len(errors) == 0,
        "concept_id": concept_id,
        "added_count": len(results),
        "error_count": len(errors),
        "results": results,
        "errors": errors if errors else [],
    }
    return [_json_text(payload)]


async def _handle_get_tree(arguments: dict[str, Any]) -> list[TextContent]:
    return [_json_text(get_vontology_tree())]


async def _handle_fetch_concept(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to fetch",
                    "Use search_concepts to find concepts by name or description",
                ],
            )
        ]

    try:
        concept = get_concept_by_concept_id(concept_id)
        if not concept:
            return [
                _json_error(
                    f"Concept '{concept_id}' not found",
                    error_code="concept_not_found",
                    suggestions=[
                        "Check the concept_id spelling",
                        "Use search_concepts to find available concepts",
                    ],
                    related_concept_ids=[concept_id],
                )
            ]

        concept = enrich_concept_with_text_relations(concept)

        # NOTE: mcp_stdio_server runs as a top-level script in stdio mode.
        # Relative imports fail in that runtime ("no known parent package"),
        # so this import must remain absolute.
        from src.backend.services.relationship_write_service import (
            detect_vacuous_typing,
        )

        vacuous_warning = detect_vacuous_typing(concept)
        if vacuous_warning:
            concept["_vacuous_typing_warning"] = vacuous_warning

        include_relations_arg1 = bool(arguments.get("include_relations_arg1"))
        include_relations_any_arg = bool(arguments.get("include_relations_any_arg"))
        include_text_relations_arg1 = arguments.get(
            "include_text_relations_arg1", False
        )
        predicate_filter = arguments.get("predicate_filter")
        limit = arguments.get("limit")
        offset = arguments.get("offset")
        include_concept_preview = arguments.get("include_concept_preview", True)
        include_uncertain = bool(arguments.get("include_uncertain", False))
        uncertainty_mode = arguments.get("uncertainty_mode")
        uncertainty_statuses = arguments.get("uncertainty_statuses")

        if predicate_filter is not None and not isinstance(predicate_filter, list):
            if isinstance(predicate_filter, (tuple, set)):
                predicate_filter = list(predicate_filter)
            else:
                predicate_filter = [predicate_filter]

        if any(
            [
                include_relations_arg1,
                include_relations_any_arg,
                include_text_relations_arg1,
            ]
        ):
            relations_payload = build_concept_relations_payload(
                concept,
                include_relations_arg1=include_relations_arg1,
                include_relations_any_arg=include_relations_any_arg,
                include_text_relations_arg1=include_text_relations_arg1,
                predicate_filter=predicate_filter,
                limit=limit,
                offset=offset,
                include_concept_preview=include_concept_preview,
                include_uncertain=include_uncertain,
                uncertainty_mode=uncertainty_mode,
                uncertainty_statuses=uncertainty_statuses,
            )
            concept["relations"] = relations_payload
        return [_json_text(concept)]
    except Exception as exc:
        return [
            _json_error(
                str(exc),
                error_code="operation_failed",
                suggestions=["Verify the concept_id exists", "Check parameter values"],
                related_concept_ids=[concept_id] if concept_id else None,
            )
        ]


async def _handle_find_relations_with_argument(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if concept_id is None or not str(concept_id).strip():
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to search for",
                    "Use search_concepts if you need to discover concept IDs first",
                ],
            )
        ]

    try:
        payload = find_relations_with_argument(
            concept_id=str(concept_id),
            argument_index=arguments.get("argument_index"),
            predicate_filter=arguments.get("predicate_filter"),
            relation_kind=arguments.get("relation_kind"),
            scope=arguments.get("scope"),
            include_text_snippets=bool(arguments.get("include_text_snippets", False)),
            include_concept_preview=bool(
                arguments.get("include_concept_preview", True)
            ),
            limit=arguments.get("limit"),
            offset=arguments.get("offset"),
            sort_by=arguments.get("sort_by"),
            include_uncertain=bool(arguments.get("include_uncertain", False)),
            uncertainty_mode=arguments.get("uncertainty_mode"),
            uncertainty_statuses=arguments.get("uncertainty_statuses"),
        )
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to search relations: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Check argument_index (integer >= 1 or 'any')",
                    "Use relation_kind in {any, binary, text}",
                ],
                related_concept_ids=[str(concept_id)],
            )
        ]


async def _handle_get_predicate_incidence(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    instance_of = arguments.get("instance_of")
    if not (
        (isinstance(concept_id, str) and concept_id.strip())
        or (isinstance(instance_of, str) and instance_of.strip())
    ):
        return [
            _json_error(
                "Missing concept_id or instance_of parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide concept_id for entity-level predicate incidence",
                    "Provide instance_of for type-level predicate incidence",
                ],
            )
        ]

    try:
        payload = get_predicate_incidence(
            concept_id=(
                str(concept_id).strip() if isinstance(concept_id, str) else None
            ),
            instance_of=(
                str(instance_of).strip() if isinstance(instance_of, str) else None
            ),
            direct_instances_only=bool(arguments.get("direct_instances_only", False)),
            argument_index=arguments.get("argument_index"),
            predicate_filter=arguments.get("predicate_filter"),
            relation_kind=arguments.get("relation_kind"),
            scope=arguments.get("scope"),
            include_text_snippets=bool(arguments.get("include_text_snippets", False)),
            include_concept_preview=bool(
                arguments.get("include_concept_preview", True)
            ),
            limit=arguments.get("limit"),
            offset=arguments.get("offset"),
            sort_by=arguments.get("sort_by"),
            include_uncertain=bool(arguments.get("include_uncertain", False)),
            uncertainty_mode=arguments.get("uncertainty_mode"),
            uncertainty_statuses=arguments.get("uncertainty_statuses"),
            include_argument_type_counts=bool(
                arguments.get("include_argument_type_counts", False)
            ),
            type_count_mode=arguments.get("type_count_mode"),
            include_untyped_bucket=bool(arguments.get("include_untyped_bucket", True)),
            max_types_per_predicate=arguments.get("max_types_per_predicate"),
            max_sample_concepts_per_type=arguments.get("max_sample_concepts_per_type"),
            role_expansion_mode=arguments.get("role_expansion_mode"),
            role_node_type_filter=arguments.get("role_node_type_filter"),
            role_predicate_filter=arguments.get("role_predicate_filter"),
            role_expansion_depth=arguments.get("role_expansion_depth"),
        )
        return [_json_text(payload)]
    except Exception as exc:
        related_ids = [
            value
            for value in (
                str(concept_id).strip() if isinstance(concept_id, str) else None,
                str(instance_of).strip() if isinstance(instance_of, str) else None,
            )
            if value
        ]
        return [
            _json_error(
                f"Failed to compute predicate incidence: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Provide exactly one of concept_id or instance_of",
                    "Use relation_kind in {any, binary, text}",
                ],
                related_concept_ids=related_ids or None,
            )
        ]


async def _handle_get_concept_usage_profile(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=["Provide the concept_id to profile"],
            )
        ]

    try:
        payload = build_concept_usage_profile(
            concept_id=str(concept_id).strip(),
            relation_limit=arguments.get("relation_limit")
            or arguments.get("limit")
            or 200,
            text_limit=arguments.get("text_limit") or 200,
            minimum_total_usage=arguments.get("minimum_total_usage"),
        )
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to build concept usage profile: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Verify the concept_id exists",
                    "Lower relation_limit or text_limit for a cheaper bounded profile",
                ],
                related_concept_ids=[str(concept_id)] if concept_id else None,
            )
        ]


async def _handle_get_text_relations_summary(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to get text relations summary for",
                    "Use search_concepts to find concepts first",
                ],
            )
        ]

    try:
        payload = get_text_relations_summary(
            concept_id,
            predicates=arguments.get("predicates"),
            languages=arguments.get("languages"),
            max_relation_ids_per_group=arguments.get("max_relation_ids_per_group", 25),
        )
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to summarise text relations: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Verify the concept_id exists",
                    "Check predicate names are valid",
                ],
                related_concept_ids=[concept_id],
            )
        ]


async def _handle_upsert_singleton_text_relation(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    predicate = arguments.get("predicate")
    text = arguments.get("text")

    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to add the text relation to",
                    "Use search_concepts to find the concept first",
                ],
            )
        ]
    if not predicate:
        return [
            _json_error(
                "Missing predicate parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide a predicate (e.g. 'hasDescription', 'hasContent', 'hasName')",
                    "Use search_concepts to find predicate concepts",
                ],
            )
        ]
    if not text:
        return [
            _json_error(
                "Missing text parameter",
                error_code="missing_parameter",
                suggestions=["Provide the text content to associate with the concept"],
            )
        ]

    try:
        predicate_resolution = resolve_text_relation_predicate_for_write(predicate)
        payload = upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=predicate_resolution.storage_predicate,
            text=text,
            lang=arguments.get("language", "en-NZ"),
            policy=arguments.get("policy", "replace_others"),
            provenance=arguments.get("provenance"),
            context=arguments.get("context"),
            garbage_collect=arguments.get("garbage_collect", True),
        )
        payload["input_predicate"] = predicate_resolution.input_predicate
        payload["predicate_concept_id"] = predicate_resolution.predicate_concept_id
        return [_json_text(payload)]
    except TextRelationPredicateResolutionError as exc:
        return [
            _json_error(
                str(exc),
                error_code=exc.error_code,
                details=exc.details,
                suggestions=exc.suggestions,
            )
        ]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to upsert singleton text relation: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Verify the concept_id exists",
                    "Check that predicate is a valid predicate concept_id or name",
                ],
                related_concept_ids=[concept_id],
            )
        ]


async def _handle_audit_concept_text_relations(
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Audit text relations for a concept, showing accessibility status.

    This is used before rename operations to identify inaccessible text relations
    that would block the rename.
    """
    from src.backend.services.text_value_service import audit_concept_text_relations

    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to audit text relations for",
                    "Use search_concepts to find the concept first",
                ],
            )
        ]

    try:
        payload = audit_concept_text_relations(
            concept_id,
            include_text_preview=arguments.get("include_text_preview", True),
            max_preview_length=arguments.get("max_preview_length", 100),
        )
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to audit text relations: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Verify the concept_id exists",
                    "Check the concept_id format (e.g., '#V#concept_name')",
                ],
                related_concept_ids=[concept_id],
            )
        ]


async def _handle_concept_exists(arguments: dict[str, Any]) -> list[TextContent]:
    from src.backend.security.access_control import can_access_concept
    from src.backend.vontology.code_concepts_registry import is_code_concept_id

    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=["Provide the concept_id to check existence for"],
            )
        ]

    try:
        collection = ConceptsRepository.collection()
        if collection is None:
            return [
                _json_error(
                    "Database connection unavailable",
                    error_code="db_unavailable",
                    details={
                        "collection": "concepts",
                        "reason": "concepts_collection_unavailable",
                    },
                    suggestions=[
                        "Retry once the MongoDB connection is healthy",
                        "Check Mongo DNS/direct-host fallback configuration if this persists",
                    ],
                )
            ]
        doc = ConceptsRepository.find_one({"concept_id": concept_id}, {"_id": 1})
        payload = {
            "success": True,
            "concept_id": concept_id,
            "exists": bool(doc) or is_code_concept_id(concept_id),
            "accessible": can_access_concept(concept_id),
        }
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to check concept existence: {exc}",
                error_code="operation_failed",
                related_concept_ids=[concept_id],
            )
        ]


async def _handle_fetch_concept_content(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to fetch content for",
                    "Use search_concepts to find concepts by name",
                ],
            )
        ]

    reconstruct_md = arguments.get("reconstruct_md", True)
    try:
        payload = get_vontology_node_content(
            concept_id, reconstruct_md=bool(reconstruct_md)
        )
        payload["success"] = "error" not in payload
        return [_json_text(payload)]
    except Exception as exc:
        return [
            _json_error(
                f"Failed to fetch concept content: {exc}",
                error_code="operation_failed",
                suggestions=[
                    "Verify the concept_id exists",
                    "Use concept_exists to check if the concept is accessible",
                ],
                related_concept_ids=[concept_id],
            )
        ]


async def _handle_search_concepts(arguments: dict[str, Any]) -> list[TextContent]:
    search_result = search_concepts(**arguments)
    return [_json_text(search_result)]


async def _handle_get_concept_index_status(
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Get concept embedding index status - either for a specific concept or aggregate stats."""
    from src.backend.services.concept_embedding_service import (
        get_concept_embedding_status,
    )

    concept_id = arguments.get("concept_id")

    if concept_id:
        # Get status for specific concept
        status = get_concept_embedding_status(concept_id)
        if status is None:
            return [
                _json_error(
                    f"Concept not found: {concept_id}",
                    error_code="concept_not_found",
                    suggestions=[
                        "Check the concept_id spelling",
                        "Use search_concepts to verify the concept exists",
                    ],
                )
            ]
        return [_json_text({"success": True, "concept_status": status})]

    # Get aggregate stats
    stats = get_concept_embedding_stats()
    return [_json_text({"success": True, "index_stats": stats})]


async def _handle_resolve_concept_by_name(
    arguments: dict[str, Any],
) -> list[TextContent]:
    from src.backend.services.concept_resolution_service import resolve_concept_by_name

    name = arguments.get("name")
    if name is None or not str(name).strip():
        return [
            _json_text(
                {"success": False, "status": "not_found", "error": "Missing 'name'"}
            )
        ]

    payload = resolve_concept_by_name(
        name=str(name),
        preferred_languages=arguments.get("preferred_languages"),
        allowed_languages=arguments.get("allowed_languages"),
        instance_of=arguments.get("instance_of"),
        match_code_strings=bool(arguments.get("match_code_strings", True)),
        normalisation_level=str(arguments.get("normalisation_level", "default")),
        max_results=int(arguments.get("max_results", 5)),
    )
    return [_json_text(payload)]


async def _handle_extract_annotations(arguments: dict[str, Any]) -> list[TextContent]:
    input_text = arguments.get("input_text")
    context_concept_id = arguments.get("context_concept_id")
    if not input_text:
        return [
            _json_error(
                "Missing input_text parameter",
                error_code="missing_parameter",
                suggestions=["Provide the text to extract annotations from"],
            )
        ]

    annotations_result = extract_annotations(text=input_text)
    if context_concept_id:
        annotations_result = {
            "context_concept_id": context_concept_id,
            "annotations": annotations_result,
        }
    return [_json_text(annotations_result)]


async def _handle_search_arxiv(arguments: dict[str, Any]) -> list[TextContent]:
    try:
        proxy = get_arxiv_proxy()
        result = proxy.search_arxiv(
            query=arguments.get("query", ""),
            max_results=arguments.get("max_results", 10),
            sort_by=arguments.get("sort_by", "relevance"),
            sort_order=arguments.get("sort_order", "descending"),
        )
        return [_json_text(result)]
    except ArxivProxyError as exc:
        return [_json_text({"error": str(exc), "success": False})]
    except Exception as exc:
        return [
            _json_text({"error": f"Unexpected error: {str(exc)}", "success": False})
        ]


async def _handle_get_paper_metadata(arguments: dict[str, Any]) -> list[TextContent]:
    arxiv_id = arguments.get("arxiv_id")
    if not arxiv_id:
        return [
            _json_error(
                "Missing required parameter: arxiv_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the arXiv paper ID (e.g. '2301.00001')",
                    "Use search_arxiv to find papers first",
                ],
            )
        ]

    try:
        proxy = get_arxiv_proxy()
        result = proxy.get_paper_metadata(arxiv_id=arxiv_id)
        return [_json_text(result)]
    except ArxivProxyError as exc:
        return [_json_text({"error": str(exc), "success": False})]
    except Exception as exc:
        return [
            _json_text({"error": f"Unexpected error: {str(exc)}", "success": False})
        ]


async def _handle_download_paper(arguments: dict[str, Any]) -> list[TextContent]:
    arxiv_id = arguments.get("arxiv_id")
    if not arxiv_id:
        return [
            _json_error(
                "Missing required parameter: arxiv_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the arXiv paper ID (e.g. '2301.00001')",
                    "Use search_arxiv to find papers first",
                ],
            )
        ]

    try:
        from src.backend.integrations.internal_mcp import (
            catalogue as internal_catalogue,
        )

        result = internal_catalogue._download_paper(
            arxiv_id=arxiv_id,
            filename=arguments.get("filename"),
            delete_local_cache=arguments.get("delete_local_cache"),
            namespace=arguments.get("namespace"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text({"error": f"Unexpected error: {str(exc)}", "success": False})
        ]


async def _handle_finalise_cached_paper(arguments: dict[str, Any]) -> list[TextContent]:
    arxiv_id = arguments.get("arxiv_id")
    if not arxiv_id:
        return [
            _json_error(
                "Missing required parameter: arxiv_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the arXiv paper ID to finalise",
                    "Use download_paper first to cache the paper",
                ],
            )
        ]

    try:
        from src.backend.integrations.internal_mcp import (
            catalogue as internal_catalogue,
        )

        result = internal_catalogue._finalise_cached_paper(
            arxiv_id=arxiv_id,
            name=arguments.get("filename") or arguments.get("name"),
            delete_local_cache=arguments.get("delete_local_cache"),
            include_markdown=arguments.get("include_markdown"),
            namespace=arguments.get("namespace"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text({"error": f"Unexpected error: {str(exc)}", "success": False})
        ]


async def _handle_materialise_scholarly_representation_for_file_copy(
    arguments: dict[str, Any],
) -> list[TextContent]:
    concept_id = arguments.get("concept_id") or arguments.get("file_copy_concept_id")
    if not concept_id:
        return [
            _json_error(
                "Missing required parameter: concept_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the #V#computer_file_copy concept_id to materialise",
                ],
            )
        ]

    try:
        from src.backend.integrations.internal_mcp import (
            catalogue as internal_catalogue,
        )

        result = (
            internal_catalogue._materialise_scholarly_representation_for_file_copy_tool(
                **arguments
            )
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text({"error": f"Unexpected error: {str(exc)}", "success": False})
        ]


async def _handle_search_web(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    if not query:
        return [
            _json_error(
                "Missing query parameter",
                error_code="missing_parameter",
                suggestions=["Provide a search query string"],
            )
        ]
    try:
        proxy = await get_search_proxy()
        result = await proxy.search(
            query=query,
            max_results=arguments.get("max_results", 10),
            search_depth=arguments.get("search_depth", "basic"),
            include_domains=arguments.get("include_domains"),
            exclude_domains=arguments.get("exclude_domains"),
            include_answer=arguments.get("include_answer", False),
            include_raw_content=arguments.get("include_raw_content", False),
            include_images=arguments.get("include_images", False),
        )
        return [_json_text(result)]
    except SearchProxyError as exc:
        return [
            _json_error(
                str(exc),
                error_code="search_failed",
                suggestions=["Check your internet connection", "Try a different query"],
            )
        ]
    except Exception as exc:
        return [
            _json_error(
                f"Unexpected error: {exc}",
                error_code="unexpected_error",
            )
        ]


async def _handle_context_search(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    context_value = arguments.get("context")
    if not query or not context_value:
        return [
            _json_error(
                "Missing required parameters: query and context",
                error_code="missing_parameter",
                suggestions=[
                    "Provide both 'query' (what to search for) and 'context' (background context for the search)"
                ],
            )
        ]
    try:
        proxy = await get_search_proxy()
        result = await proxy.context_search(
            query=query,
            context=context_value,
            max_results=arguments.get("max_results", 10),
            search_depth=arguments.get("search_depth", "basic"),
            include_answer=arguments.get("include_answer", False),
        )
        return [_json_text(result)]
    except SearchProxyError as exc:
        return [
            _json_error(
                str(exc),
                error_code="search_failed",
                suggestions=[
                    "Check your internet connection",
                    "Try a different query or context",
                ],
            )
        ]
    except Exception as exc:
        return [
            _json_error(
                f"Unexpected error: {exc}",
                error_code="unexpected_error",
            )
        ]


async def _handle_qna_search(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    if not query:
        return [
            _json_error(
                "Missing query parameter",
                error_code="missing_parameter",
                suggestions=["Provide the question to search for answers to"],
            )
        ]
    try:
        proxy = await get_search_proxy()
        result = await proxy.qna_search(
            query=query,
            max_results=arguments.get("max_results", 5),
            search_depth=arguments.get("search_depth", "advanced"),
        )
        return [_json_text(result)]
    except SearchProxyError as exc:
        return [
            _json_error(
                str(exc),
                error_code="search_failed",
                suggestions=[
                    "Check your internet connection",
                    "Try rephrasing your question",
                ],
            )
        ]
    except Exception as exc:
        return [
            _json_error(
                f"Unexpected error: {exc}",
                error_code="unexpected_error",
            )
        ]


async def _handle_extract_url(arguments: dict[str, Any]) -> list[TextContent]:
    url = arguments.get("url")
    if not url:
        return [
            _json_error(
                "Missing url parameter",
                error_code="missing_parameter",
                suggestions=["Provide the URL to extract content from"],
            )
        ]
    try:
        proxy = await get_search_proxy()
        result = await proxy.extract(url=url)
        return [_json_text(result)]
    except SearchProxyError as exc:
        return [
            _json_error(
                str(exc),
                error_code="extraction_failed",
                suggestions=[
                    "Check that the URL is accessible",
                    "Verify the URL is correct",
                ],
            )
        ]
    except Exception as exc:
        return [
            _json_error(
                f"Unexpected error: {exc}",
                error_code="unexpected_error",
            )
        ]


def _gmail_audit_context(tool: str) -> dict[str, str]:
    return {"source": "mcp_stdio", "tool": tool}


async def _handle_gmail_list_messages(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    if not profile:
        return [
            _json_error(
                "Missing required parameter: profile",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the Gmail profile ID",
                    "Use gmail_list_labels to verify available profiles",
                ],
            )
        ]
    try:
        result = gmail_service.list_messages(
            profile_id=profile,
            query=arguments.get("query"),
            label_ids=arguments.get("label_ids"),
            max_results=arguments.get("max_results", 25),
            audit_context=_gmail_audit_context("gmail_list_messages"),
            bypass_profile_query_prefix=bool(
                arguments.get("bypass_profile_query_prefix") or False
            ),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail list failed: {exc}",
                error_code="gmail_error",
                suggestions=[
                    "Verify the profile ID is correct",
                    "Ensure Gmail integration is configured",
                ],
            )
        ]


async def _handle_gmail_get_message(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    message_id = arguments.get("message_id")
    if not profile or not message_id:
        return [
            _json_error(
                "Missing required parameters: profile and message_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide both profile ID and message_id",
                    "Use gmail_list_messages to find message IDs",
                ],
            )
        ]
    try:
        result = gmail_service.get_message(
            profile_id=profile,
            message_id=message_id,
            format=arguments.get("format", "metadata"),
            audit_context=_gmail_audit_context("gmail_get_message"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail get message failed: {exc}",
                error_code="gmail_error",
                suggestions=["Verify the message_id and profile are correct"],
            )
        ]


async def _handle_gmail_send_message(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    to = (
        arguments.get("to")
        or arguments.get("recipient")
        or arguments.get("recipients")
    )
    subject = arguments.get("subject")
    body_text = arguments.get("body_text") or arguments.get("body") or arguments.get(
        "message"
    )
    profile_text = profile if isinstance(profile, str) and profile.strip() else None
    to_value = to if isinstance(to, (str, list)) and to else None
    subject_text = subject if isinstance(subject, str) and subject.strip() else None
    body_text_value = (
        body_text if isinstance(body_text, str) and body_text.strip() else None
    )
    allow_send = bool(arguments.get("allow_send"))
    missing = [
        field
        for field, value in (
            ("profile", profile_text),
            ("to", to_value),
            ("subject", subject_text),
            ("body_text", body_text_value),
        )
        if value in (None, "")
    ]
    if missing:
        return [
            _json_error(
                "Missing required parameters for Gmail send",
                error_code="missing_parameter",
                suggestions=[
                    "Provide profile, to, subject, body_text, and allow_send=true",
                ],
            )
        ]
    assert profile_text is not None
    assert to_value is not None
    assert subject_text is not None
    assert body_text_value is not None
    if not allow_send:
        return [
            _json_error(
                "allow_send must be true to send Gmail messages",
                error_code="send_not_allowed",
                suggestions=[
                    "Set allow_send=true only when explicitly authorised to send this message",
                ],
            )
        ]
    try:
        result = gmail_service.send_message(
            profile_id=profile_text,
            to=cast(str | list[str], to_value),
            subject=subject_text,
            body_text=body_text_value,
            cc=arguments.get("cc"),
            bcc=arguments.get("bcc"),
            reply_to=arguments.get("reply_to"),
            body_html=arguments.get("body_html"),
            allow_send=allow_send,
            audit_context=_gmail_audit_context("gmail_send_message"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail send failed: {exc}",
                error_code="gmail_error",
                suggestions=[
                    "Verify the profile ID is correct",
                    "Ensure Gmail integration is configured with a send-capable OAuth scope",
                ],
            )
        ]


async def _handle_gmail_get_attachment(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    message_id = arguments.get("message_id")
    attachment_id = arguments.get("attachment_id")
    if not profile or not message_id or not attachment_id:
        return [
            _json_error(
                "Missing required parameters: profile, message_id, attachment_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide profile, message_id, and attachment_id",
                    "Use gmail_get_message to find attachment IDs",
                ],
            )
        ]
    try:
        result = gmail_service.get_attachment(
            profile_id=profile,
            message_id=message_id,
            attachment_id=attachment_id,
            audit_context=_gmail_audit_context("gmail_get_attachment"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail get attachment failed: {exc}",
                error_code="gmail_error",
                suggestions=[
                    "Verify the attachment_id, message_id, and profile are correct"
                ],
            )
        ]


async def _handle_gmail_list_labels(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    if not profile:
        return [
            _json_error(
                "Missing required parameter: profile",
                error_code="missing_parameter",
                suggestions=["Provide the Gmail profile ID"],
            )
        ]
    try:
        result = gmail_service.list_labels(
            profile_id=profile,
            audit_context=_gmail_audit_context("gmail_list_labels"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail list labels failed: {exc}",
                error_code="gmail_error",
                suggestions=[
                    "Verify the profile ID is correct",
                    "Ensure Gmail integration is configured",
                ],
            )
        ]


async def _handle_gmail_modify_labels(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    message_id = arguments.get("message_id")
    allow_mutation = bool(arguments.get("allow_mutation"))
    if not profile or not message_id:
        return [
            _json_error(
                "Missing required parameters: profile and message_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide both profile ID and message_id",
                    "Use gmail_list_messages to find message IDs",
                ],
            )
        ]
    if not allow_mutation:
        return [
            _json_error(
                "allow_mutation must be true to modify labels",
                error_code="mutation_not_allowed",
                suggestions=["Set allow_mutation=true to confirm label modification"],
            )
        ]
    try:
        result = gmail_service.modify_labels(
            profile_id=profile,
            message_id=message_id,
            add_labels=arguments.get("add_labels"),
            remove_labels=arguments.get("remove_labels"),
            allow_mutation=allow_mutation,
            audit_context=_gmail_audit_context("gmail_modify_labels"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail modify labels failed: {exc}",
                error_code="gmail_error",
                suggestions=[
                    "Verify the message_id and profile are correct",
                    "Ensure the labels exist",
                ],
            )
        ]


async def _handle_add_relationship(arguments: dict[str, Any]) -> list[TextContent]:
    source_id = arguments.get("source_id")
    predicate = arguments.get("predicate")
    target = arguments.get("target")
    if not source_id or not predicate or not target:
        missing: list[str] = []
        if not source_id:
            missing.append("source_id")
        if not predicate:
            missing.append("predicate")
        if not target:
            missing.append("target")
        return [
            _json_text(
                {
                    "success": False,
                    "error": "Missing required parameters: source_id, predicate, and target",
                    "error_code": "missing_parameter",
                    "error_details": {"missing": missing},
                }
            )
        ]
    try:
        result = _add_relationship(
            source_id=source_id, predicate=predicate, target=target
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text(
                {
                    "success": False,
                    "error": f"Failed to add relationship: {str(exc)}",
                    "error_code": "exception",
                    "error_details": {"exception_type": type(exc).__name__},
                }
            )
        ]


async def _handle_remove_relationship(arguments: dict[str, Any]) -> list[TextContent]:
    relation_id = arguments.get("relation_id")
    source_id = arguments.get("source_id")
    predicate = arguments.get("predicate")
    target = arguments.get("target")
    if not relation_id and (not source_id or not predicate or not target):
        missing: list[str] = []
        if not relation_id:
            if not source_id:
                missing.append("source_id")
            if not predicate:
                missing.append("predicate")
            if not target:
                missing.append("target")
        return [
            _json_text(
                {
                    "success": False,
                    "error": "Missing required parameters: relation_id OR source_id/predicate/target",
                    "error_code": "missing_parameter",
                    "error_details": {"missing": missing},
                }
            )
        ]
    try:
        result = _remove_relationship(
            relation_id=relation_id,
            source_id=source_id,
            predicate=predicate,
            target=target,
            mode=arguments.get("mode"),
            cascade=arguments.get("cascade"),
            dry_run=arguments.get("dry_run", False),
            confirmed=arguments.get("confirmed", False),
            operator_override=arguments.get("operator_override", False),
            reason=arguments.get("reason"),
            request_id=arguments.get("request_id"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text(
                {
                    "success": False,
                    "error": f"Failed to remove relationship: {str(exc)}",
                    "error_code": "exception",
                    "error_details": {"exception_type": type(exc).__name__},
                }
            )
        ]


async def _handle_preview_remove_relationship(
    arguments: dict[str, Any],
) -> list[TextContent]:
    try:
        result = _preview_remove_relationship(
            relation_id=arguments.get("relation_id"),
            source_id=arguments.get("source_id"),
            predicate=arguments.get("predicate"),
            target=arguments.get("target"),
            request_id=arguments.get("request_id"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text(
                {
                    "success": False,
                    "error": f"Failed to preview relationship removal: {str(exc)}",
                    "error_code": "exception",
                    "error_details": {"exception_type": type(exc).__name__},
                }
            )
        ]


async def _handle_remove_relationships_bulk(
    arguments: dict[str, Any],
) -> list[TextContent]:
    try:
        result = _remove_relationships_bulk(
            relation_ids=arguments.get("relation_ids"),
            relations=arguments.get("relations"),
            filter=arguments.get("filter"),
            mode=arguments.get("mode"),
            cascade=arguments.get("cascade"),
            dry_run=arguments.get("dry_run", False),
            confirmed=arguments.get("confirmed", False),
            operator_override=arguments.get("operator_override", False),
            reason=arguments.get("reason"),
            request_id=arguments.get("request_id"),
            stop_on_error=arguments.get("stop_on_error", False),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text(
                {
                    "success": False,
                    "error": f"Failed to bulk-remove relationships: {str(exc)}",
                    "error_code": "exception",
                    "error_details": {"exception_type": type(exc).__name__},
                }
            )
        ]


async def _handle_undo_relationship_removal(
    arguments: dict[str, Any],
) -> list[TextContent]:
    undo_token = arguments.get("undo_token")
    if not undo_token:
        return [
            _json_text(
                {
                    "success": False,
                    "error": "Missing required parameter: undo_token",
                    "error_code": "missing_parameter",
                    "error_details": {"missing": ["undo_token"]},
                }
            )
        ]
    try:
        result = _undo_relationship_removal(
            undo_token=undo_token,
            request_id=arguments.get("request_id"),
            confirmed=arguments.get("confirmed", True),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_text(
                {
                    "success": False,
                    "error": f"Failed to undo relationship removal: {str(exc)}",
                    "error_code": "exception",
                    "error_details": {"exception_type": type(exc).__name__},
                }
            )
        ]


async def _handle_delete_concept(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    simulate = arguments.get("simulate", True)
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to delete",
                    "Use search_concepts to find the concept first",
                ],
            )
        ]
    result = simulate_or_delete_concept(concept_id, execute=not simulate)
    return [_json_text(result)]


async def _handle_merge_concepts(arguments: dict[str, Any]) -> list[TextContent]:
    source_id = arguments.get("source_id")
    target_id = arguments.get("target_id")
    simulate = arguments.get("simulate", True)
    if not source_id or not target_id:
        return [
            _json_error(
                "Missing source_id or target_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide both source_id (concept to merge FROM) and target_id (concept to merge INTO)",
                    "Use search_concepts to find concept IDs",
                ],
            )
        ]
    result = merge_concepts(source_id, target_id, simulate=simulate)
    return [_json_text(result)]


async def _handle_update_concept(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    update_data = arguments.get("update_data")
    if not concept_id:
        return [
            _json_error(
                "Missing concept_id parameter",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the concept_id to update",
                    "Use search_concepts to find the concept first",
                ],
            )
        ]
    if not update_data or not isinstance(update_data, dict):
        return [
            _json_error(
                "Missing or invalid update_data dictionary",
                error_code="invalid_parameter",
                suggestions=[
                    "Provide update_data as a dictionary with fields to update",
                    'Example: {"description": "new description"}',
                ],
            )
        ]
    try:
        result = update_concept(concept_id=concept_id, update_data=update_data)
        if result:
            return [
                _json_text(
                    {
                        "success": True,
                        "concept_id": concept_id,
                        "updated_fields": list(update_data.keys()),
                    }
                )
            ]
        return [
            _json_text(
                {"error": "Update failed or concept not found", "success": False}
            )
        ]
    except Exception as exc:
        return [_json_text({"error": str(exc), "success": False})]


async def _handle_search_knowledge_base(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    if not query:
        return [
            _json_error(
                "Missing query parameter",
                error_code="missing_parameter",
                suggestions=["Provide a search query for the knowledge base"],
            )
        ]
    try:
        service = get_rag_service()
        permissions_context = {}
        try:
            from flask import session as flask_session

            if flask_session.get("user_id"):
                permissions_context["user_id"] = flask_session.get("user_id")
            if flask_session.get("org_id"):
                permissions_context["organisation_concept_id"] = flask_session.get(
                    "org_id"
                )
        except (ImportError, RuntimeError):
            pass

        results = service.query(
            query_text=query,
            top_k=arguments.get("top_k", 5),
            namespace=arguments.get("namespace"),
            permissions_context=(permissions_context if permissions_context else None),
        )
        return [
            _json_text({"results": results, "count": len(results), "success": True})
        ]
    except RAGBackendUnavailable as exc:
        return [
            _json_text({"error": f"RAG service unavailable: {exc}", "success": False})
        ]
    except Exception as exc:
        return [_json_text({"error": f"Unexpected error: {exc}", "success": False})]


def _run_catalogue_proxy_handler(
    handler: Callable[..., Any],
    arguments: dict[str, Any],
    *,
    tool_family_label: str,
    suggestions: list[str] | None = None,
) -> list[TextContent]:
    try:
        result = handler(**(arguments or {}))
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"{tool_family_label} tool failed: {exc}",
                error_code="operation_failed",
                details={"exception_type": type(exc).__name__},
                suggestions=(
                    suggestions
                    if suggestions
                    else ["Check tool arguments and service availability"]
                ),
            )
        ]


async def _handle_jira_search(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_search,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_get_issue,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_project_issue_types(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_get_project_issue_types,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_bulk_operation_progress(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_get_bulk_operation_progress,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_transitions(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_get_transitions,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_add_comment(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_add_comment,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_add_attachment(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_add_attachment,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_transition(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_transition_issue,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_create_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_create_issue,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_update_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_update_issue,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_move_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_move_issue,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_link_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_link_issue,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_delete_issue_link(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_delete_issue_link,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_myself(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_get_myself,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_auth_config(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_get_auth_config,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_list_recent_screenshots(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _list_recent_screenshots,
        arguments,
        tool_family_label="Files",
        suggestions=[
            "Check screenshot directory availability",
            "Install Pillow for clipboard image matching support",
        ],
    )


async def _handle_workflow_list_definitions(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_definitions,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_validate_candidate(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_validate_candidate,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_list_use_episodes(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_use_episodes,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_renderer_resolve_applicability(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _renderer_resolve_applicability,
        arguments,
        tool_family_label="Renderer",
    )


async def _handle_upsert_renderer_profile(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _upsert_renderer_profile,
        arguments,
        tool_family_label="Renderer",
    )


async def _handle_workflow_bind_event(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_bind_event,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_list_event_bindings(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_event_bindings,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_set_event_binding_enabled(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_set_event_binding_enabled,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_delete_event_binding(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_delete_event_binding,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_mcp_health_check(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_mcp_health_check,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_coding_agent_mcp_access_profile(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _coding_agent_mcp_access_profile,
        arguments,
        tool_family_label="Internal",
    )


async def _handle_workflow_materialisation_diagnostics(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_materialisation_diagnostics,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_concept_parity_audit(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_concept_parity_audit,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_create_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_create_instance,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_execute(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_execute,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_list_instances(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_instances,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_list_execution_traces(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_execution_traces,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_build_prediction_envelope(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_build_prediction_envelope,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_get_instance(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_get_instance,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_get_execution_trace(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_get_execution_trace,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_cancel_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_cancel_instance,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_retry_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_retry_instance,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_create_schedule(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_create_schedule,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_list_schedules(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_schedules,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_get_schedule(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_get_schedule,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_set_schedule_enabled(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_set_schedule_enabled,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_delete_schedule(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_delete_schedule,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_workflow_trigger_schedule(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_trigger_schedule,
        arguments,
        tool_family_label="Workflow",
    )


async def _handle_chat_history_get_segments(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _chat_history_get_segments,
        arguments,
        tool_family_label="ChatHistory",
    )


async def _handle_chat_history_get_debug_entry(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _chat_history_get_debug_entry,
        arguments,
        tool_family_label="ChatHistory",
    )


async def _handle_conversation_telemetry_get_locator(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _conversation_telemetry_get_locator,
        arguments,
        tool_family_label="ChatHistory",
    )


async def _handle_turn_execution_list(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_list,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_get(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_get,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_get_diagnostics(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_get_diagnostics,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_get_live_progress(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_get_live_progress,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_get_critic_bundle(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_get_critic_bundle,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_search_failures(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_search_failures,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_build_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_build_benchmark,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_build_selector_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_build_selector_benchmark,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_build_context_answering_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_build_context_answering_benchmark,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_build_dashboard(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_build_dashboard,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_build_paper_recommendations(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _build_paper_recommendations,
        arguments,
        tool_family_label="PaperRecommendation",
    )


async def _handle_skill_catalogue_list(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _skill_catalogue_list,
        arguments,
        tool_family_label="SkillCatalogue",
    )


async def _handle_skill_catalogue_sync(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _skill_catalogue_sync,
        arguments,
        tool_family_label="SkillCatalogue",
    )


async def _handle_turn_execution_backfill_from_chat_history(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_backfill_from_chat_history,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_turn_execution_namespace_coverage_report(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _turn_execution_namespace_coverage_report,
        arguments,
        tool_family_label="TurnExecution",
    )


async def _handle_experiment_run_list(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_run_list,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_run_get(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_run_get,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_episode_critique_memory_list(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _episode_critique_memory_list,
        arguments,
        tool_family_label="EpisodeCritique",
    )


async def _handle_episode_critique_build_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _episode_critique_build_benchmark,
        arguments,
        tool_family_label="EpisodeCritique",
    )


async def _handle_episode_critique_memory_get(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _episode_critique_memory_get,
        arguments,
        tool_family_label="EpisodeCritique",
    )


async def _handle_repo_dossier_file_snapshot(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _repo_dossier_file_snapshot,
        arguments,
        tool_family_label="RepoDossier",
    )


async def _handle_repo_dossier_search(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _repo_dossier_search,
        arguments,
        tool_family_label="RepoDossier",
    )


async def _handle_repo_dossier_workflow_definition_get(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _repo_dossier_workflow_definition_get,
        arguments,
        tool_family_label="RepoDossier",
    )


async def _handle_repo_dossier_prompt_definition_get(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _repo_dossier_prompt_definition_get,
        arguments,
        tool_family_label="RepoDossier",
    )


async def _handle_repo_dossier_git_metadata(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _repo_dossier_git_metadata,
        arguments,
        tool_family_label="RepoDossier",
    )


async def _handle_context_bundle_resolve_effective_context(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _context_bundle_resolve_effective_context,
        arguments,
        tool_family_label="ContextBundle",
    )


async def _handle_context_bundle_assemble_context_dossier(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _context_bundle_assemble_context_dossier,
        arguments,
        tool_family_label="ContextBundle",
    )


async def _handle_context_bundle_update_report_revision(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _context_bundle_update_report_revision,
        arguments,
        tool_family_label="ContextBundle",
    )


async def _handle_context_bundle_build_reconstructed_workspace(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _context_bundle_build_reconstructed_workspace,
        arguments,
        tool_family_label="ContextBundle",
    )


async def _handle_context_bundle_build_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _context_bundle_build_benchmark,
        arguments,
        tool_family_label="ContextBundle",
    )


async def _handle_testing_theory_create_slice(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_create_slice,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_testing_theory_import_canonical_context(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_import_canonical_context,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_testing_theory_assert_local_claims(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_assert_local_claims,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_testing_theory_compute_diff(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_compute_diff,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_testing_theory_rollback_local_writes(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_rollback_local_writes,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_testing_theory_promote_validated_claims(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_promote_validated_claims,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_testing_theory_gc_expired(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_theory_gc_expired,
        arguments,
        tool_family_label="TestingTheory",
    )


async def _handle_experiment_create_spec(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_create_spec,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_start_run(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_start_run,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_record_observation(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_record_observation,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_compute_verdict(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_compute_verdict,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_emit_learning_signal(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_emit_learning_signal,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_execute_target_workflow(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_execute_target_workflow,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_experiment_execute_regression_suite(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _experiment_execute_regression_suite,
        arguments,
        tool_family_label="Experiment",
    )


async def _handle_testing_prepare_meeting_invitation_spec(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_prepare_meeting_invitation_spec,
        arguments,
        tool_family_label="TestingWorkflow",
    )


async def _handle_testing_prepare_experiment_spec(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_prepare_experiment_spec,
        arguments,
        tool_family_label="TestingWorkflow",
    )


async def _handle_testing_prepare_arxiv_paper_ingestion_fixture(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_prepare_arxiv_paper_ingestion_fixture,
        arguments,
        tool_family_label="TestingWorkflow",
    )


async def _handle_testing_verify_arxiv_paper_ingestion_result(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_verify_arxiv_paper_ingestion_result,
        arguments,
        tool_family_label="TestingWorkflow",
    )


async def _handle_testing_cleanup_arxiv_paper_ingestion_artifacts(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _testing_cleanup_arxiv_paper_ingestion_artifacts,
        arguments,
        tool_family_label="TestingWorkflow",
    )


# Task management handlers (JVNAUTOSCI-1040)
async def _handle_create_task(arguments: dict[str, Any]) -> list[TextContent]:
    title = arguments.get("title")
    description = arguments.get("description")
    if not title or not description:
        return [
            _json_error(
                "Missing required parameters: title and description",
                error_code="missing_parameter",
                suggestions=["Provide both title and description for the task"],
            )
        ]

    try:
        from src.backend.services.task_management_service import create_task

        result = create_task(
            title=title,
            description=description,
            assignee_concept_id=arguments.get("assignee_concept_id"),
            created_by_concept_id=arguments.get("creator_concept_id"),
            originating_session_id=arguments.get("session_id"),
            start_date=arguments.get("start_date"),
            due_date=arguments.get("due_date"),
            epic_task_concept_id=arguments.get("epic_task_concept_id"),
            priority=arguments.get("priority", "medium"),
            organisation_concept_id=arguments.get("organisation_concept_id"),
            task_type_ids=arguments.get("task_type_ids")
            or arguments.get("task_type_id"),
            task_source_id=arguments.get("task_source_id")
            or arguments.get("source_id"),
            report_to_concept_id=arguments.get("report_to_concept_id")
            or arguments.get("reports_to_concept_id"),
            task_role=arguments.get("task_role"),
            next_checkpoint=arguments.get("next_checkpoint"),
            progress_signal=arguments.get("progress_signal"),
            evidence=arguments.get("evidence"),
            notes=arguments.get("notes"),
            reference_code=arguments.get("reference_code"),
        )
        return [_json_text({"success": True, **result})]
    except Exception as exc:
        return [_json_text({"error": str(exc), "success": False})]


async def _handle_get_task(arguments: dict[str, Any]) -> list[TextContent]:
    task_concept_id = arguments.get("task_concept_id")
    if not task_concept_id:
        return [
            _json_error(
                "Missing required parameter: task_concept_id",
                error_code="missing_parameter",
                suggestions=[
                    "Provide the task_concept_id of the task to retrieve",
                    "Use list_my_tasks to find task IDs",
                ],
            )
        ]

    try:
        from src.backend.services.task_management_service import get_task

        result = get_task(task_concept_id)
        return [_json_text({"success": True, **result})]
    except Exception as exc:
        return [_json_text({"error": str(exc), "success": False})]


async def _handle_list_my_tasks(arguments: dict[str, Any]) -> list[TextContent]:
    user_concept_id = arguments.get("user_concept_id")
    if not user_concept_id:
        return [
            _json_error(
                "Missing required parameter: user_concept_id",
                error_code="missing_parameter",
                suggestions=["Provide the user_concept_id to list tasks for"],
            )
        ]

    try:
        from src.backend.services.task_management_service import get_tasks_for_user

        tasks = get_tasks_for_user(
            user_concept_id=user_concept_id,
            status_filter=arguments.get("status_filter"),
            include_created=arguments.get("include_created", False),
        )
        return [_json_text({"success": True, "tasks": tasks, "count": len(tasks)})]
    except Exception as exc:
        return [_json_text({"error": str(exc), "success": False})]


async def _handle_update_task_status(arguments: dict[str, Any]) -> list[TextContent]:
    task_concept_id = arguments.get("task_concept_id")
    status = arguments.get("status")
    if not task_concept_id or not status:
        return [
            _json_error(
                "Missing required parameters: task_concept_id and status",
                error_code="missing_parameter",
                suggestions=[
                    "Provide task_concept_id and new status",
                    "Valid statuses: pending, in_progress, completed, cancelled",
                ],
            )
        ]

    try:
        from src.backend.services.task_management_service import update_task_status

        result = update_task_status(task_concept_id, status)
        return [_json_text({"success": True, **result})]
    except Exception as exc:
        return [_json_text({"error": str(exc), "success": False})]


async def _handle_assign_task(arguments: dict[str, Any]) -> list[TextContent]:
    task_concept_id = arguments.get("task_concept_id")
    assignee_concept_id = arguments.get("assignee_concept_id")
    if not task_concept_id or not assignee_concept_id:
        return [
            _json_error(
                "Missing required parameters: task_concept_id and assignee_concept_id"
            )
        ]

    try:
        from src.backend.services.task_management_service import assign_task

        result = assign_task(task_concept_id, assignee_concept_id)
        return [_json_text({"success": True, **result})]
    except Exception as exc:
        return [_json_text({"error": str(exc), "success": False})]


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[list[TextContent]]]] = {
    "get_context": _handle_get_context,
    "create_concepts": _handle_create_concepts,
    "find_subconcepts": _handle_find_subconcepts,
    "find_concepts_by_name": _handle_find_concepts_by_name,
    "von_chat_run": _handle_von_chat_run,
    "upsert_text_relation": _handle_upsert_text_relation,
    "get_text_relations": _handle_get_text_relations,
    "update_text_relation": _handle_update_text_relation,
    "delete_text_relation": _handle_delete_text_relation,
    "get_text_relations_summary": _handle_get_text_relations_summary,
    "upsert_singleton_text_relation": _handle_upsert_singleton_text_relation,
    "audit_concept_text_relations": _handle_audit_concept_text_relations,
    "add_names": _handle_add_names,
    "get_tree": _handle_get_tree,
    "fetch_concept": _handle_fetch_concept,
    "find_relations_with_argument": _handle_find_relations_with_argument,
    "get_predicate_incidence": _handle_get_predicate_incidence,
    "get_concept_usage_profile": _handle_get_concept_usage_profile,
    "fetch_concept_content": _handle_fetch_concept_content,
    "concept_exists": _handle_concept_exists,
    "search_concepts": _handle_search_concepts,
    "vontology_concept_search": _handle_search_concepts,
    "get_concept_index_status": _handle_get_concept_index_status,
    "resolve_concept_by_name": _handle_resolve_concept_by_name,
    "extract_annotations": _handle_extract_annotations,
    "search_arxiv": _handle_search_arxiv,
    "get_paper_metadata": _handle_get_paper_metadata,
    "download_paper": _handle_download_paper,
    "build_paper_recommendations": _handle_build_paper_recommendations,
    "skill_catalogue_list": _handle_skill_catalogue_list,
    "skill_catalogue_sync": _handle_skill_catalogue_sync,
    "finalise_cached_paper": _handle_finalise_cached_paper,
    "materialise_scholarly_representation_for_file_copy": _handle_materialise_scholarly_representation_for_file_copy,
    "search_web": _handle_search_web,
    "context_search": _handle_context_search,
    "qna_search": _handle_qna_search,
    "extract_url": _handle_extract_url,
    "gmail_list_messages": _handle_gmail_list_messages,
    "gmail_get_message": _handle_gmail_get_message,
    "gmail_send_message": _handle_gmail_send_message,
    "gmail_get_attachment": _handle_gmail_get_attachment,
    "gmail_list_labels": _handle_gmail_list_labels,
    "gmail_modify_labels": _handle_gmail_modify_labels,
    "add_relationship": _handle_add_relationship,
    "remove_relationship": _handle_remove_relationship,
    "preview_remove_relationship": _handle_preview_remove_relationship,
    "remove_relationships_bulk": _handle_remove_relationships_bulk,
    "undo_relationship_removal": _handle_undo_relationship_removal,
    "delete_concept": _handle_delete_concept,
    "merge_concepts": _handle_merge_concepts,
    "update_concept": _handle_update_concept,
    "search_knowledge_base": _handle_search_knowledge_base,
    "experiment_run_list": _handle_experiment_run_list,
    "experiment_run_get": _handle_experiment_run_get,
    "episode_critique_build_benchmark": _handle_episode_critique_build_benchmark,
    "episode_critique_memory_list": _handle_episode_critique_memory_list,
    "episode_critique_memory_get": _handle_episode_critique_memory_get,
    "repo_dossier_file_snapshot": _handle_repo_dossier_file_snapshot,
    "repo_dossier_search": _handle_repo_dossier_search,
    "repo_dossier_workflow_definition_get": _handle_repo_dossier_workflow_definition_get,
    "repo_dossier_prompt_definition_get": _handle_repo_dossier_prompt_definition_get,
    "repo_dossier_git_metadata": _handle_repo_dossier_git_metadata,
    "context_bundle_resolve_effective_context": _handle_context_bundle_resolve_effective_context,
    "context_bundle_assemble_context_dossier": _handle_context_bundle_assemble_context_dossier,
    "context_bundle_update_report_revision": _handle_context_bundle_update_report_revision,
    "context_bundle_build_reconstructed_workspace": _handle_context_bundle_build_reconstructed_workspace,
    "context_bundle_build_benchmark": _handle_context_bundle_build_benchmark,
    "testing_theory_create_slice": _handle_testing_theory_create_slice,
    "testing_theory_import_canonical_context": _handle_testing_theory_import_canonical_context,
    "testing_theory_assert_local_claims": _handle_testing_theory_assert_local_claims,
    "testing_theory_compute_diff": _handle_testing_theory_compute_diff,
    "testing_theory_rollback_local_writes": _handle_testing_theory_rollback_local_writes,
    "testing_theory_promote_validated_claims": _handle_testing_theory_promote_validated_claims,
    "testing_theory_gc_expired": _handle_testing_theory_gc_expired,
    "experiment_create_spec": _handle_experiment_create_spec,
    "experiment_start_run": _handle_experiment_start_run,
    "experiment_record_observation": _handle_experiment_record_observation,
    "experiment_compute_verdict": _handle_experiment_compute_verdict,
    "experiment_emit_learning_signal": _handle_experiment_emit_learning_signal,
    "experiment_execute_target_workflow": _handle_experiment_execute_target_workflow,
    "experiment_execute_regression_suite": _handle_experiment_execute_regression_suite,
    "testing_prepare_experiment_spec": _handle_testing_prepare_experiment_spec,
    "testing_prepare_meeting_invitation_spec": _handle_testing_prepare_meeting_invitation_spec,
    "testing_prepare_arxiv_paper_ingestion_fixture": _handle_testing_prepare_arxiv_paper_ingestion_fixture,
    "testing_verify_arxiv_paper_ingestion_result": _handle_testing_verify_arxiv_paper_ingestion_result,
    "testing_cleanup_arxiv_paper_ingestion_artifacts": _handle_testing_cleanup_arxiv_paper_ingestion_artifacts,
    "chat_history_get_segments": _handle_chat_history_get_segments,
    "chat_history_get_debug_entry": _handle_chat_history_get_debug_entry,
    "conversation_telemetry_get_locator": _handle_conversation_telemetry_get_locator,
    "turn_execution_list": _handle_turn_execution_list,
    "turn_execution_get": _handle_turn_execution_get,
    "turn_execution_get_diagnostics": _handle_turn_execution_get_diagnostics,
    "turn_execution_get_live_progress": _handle_turn_execution_get_live_progress,
    "turn_execution_get_critic_bundle": _handle_turn_execution_get_critic_bundle,
    "turn_execution_search_failures": _handle_turn_execution_search_failures,
    "turn_execution_build_benchmark": _handle_turn_execution_build_benchmark,
    "turn_execution_build_context_answering_benchmark": _handle_turn_execution_build_context_answering_benchmark,
    "turn_execution_build_selector_benchmark": _handle_turn_execution_build_selector_benchmark,
    "turn_execution_build_dashboard": _handle_turn_execution_build_dashboard,
    "turn_execution_backfill_from_chat_history": _handle_turn_execution_backfill_from_chat_history,
    "turn_execution_namespace_coverage_report": _handle_turn_execution_namespace_coverage_report,
    "jira_search": _handle_jira_search,
    "jira_get_issue": _handle_jira_get_issue,
    "jira_get_project_issue_types": _handle_jira_get_project_issue_types,
    "jira_get_bulk_operation_progress": _handle_jira_get_bulk_operation_progress,
    "jira_get_transitions": _handle_jira_get_transitions,
    "jira_add_comment": _handle_jira_add_comment,
    "jira_add_attachment": _handle_jira_add_attachment,
    "jira_transition": _handle_jira_transition,
    "jira_create_issue": _handle_jira_create_issue,
    "jira_update_issue": _handle_jira_update_issue,
    "jira_move_issue": _handle_jira_move_issue,
    "jira_link_issue": _handle_jira_link_issue,
    "jira_delete_issue_link": _handle_jira_delete_issue_link,
    "jira_get_myself": _handle_jira_get_myself,
    "jira_get_auth_config": _handle_jira_get_auth_config,
    "list_recent_screenshots": _handle_list_recent_screenshots,
    "renderer_resolve_applicability": _handle_renderer_resolve_applicability,
    "upsert_renderer_profile": _handle_upsert_renderer_profile,
    "workflow_list_definitions": _handle_workflow_list_definitions,
    "workflow_validate_candidate": _handle_workflow_validate_candidate,
    "workflow_list_use_episodes": _handle_workflow_list_use_episodes,
    "workflow_bind_event": _handle_workflow_bind_event,
    "workflow_list_event_bindings": _handle_workflow_list_event_bindings,
    "workflow_set_event_binding_enabled": _handle_workflow_set_event_binding_enabled,
    "workflow_delete_event_binding": _handle_workflow_delete_event_binding,
    "workflow_mcp_health_check": _handle_workflow_mcp_health_check,
    "coding_agent_mcp_access_profile": _handle_coding_agent_mcp_access_profile,
    "workflow_materialisation_diagnostics": _handle_workflow_materialisation_diagnostics,
    "workflow_create_instance": _handle_workflow_create_instance,
    "workflow_execute": _handle_workflow_execute,
    "workflow_list_instances": _handle_workflow_list_instances,
    "workflow_list_execution_traces": _handle_workflow_list_execution_traces,
    "workflow_build_prediction_envelope": _handle_workflow_build_prediction_envelope,
    "workflow_get_instance": _handle_workflow_get_instance,
    "workflow_get_execution_trace": _handle_workflow_get_execution_trace,
    "workflow_cancel_instance": _handle_workflow_cancel_instance,
    "workflow_retry_instance": _handle_workflow_retry_instance,
    "workflow_concept_parity_audit": _handle_workflow_concept_parity_audit,
    "workflow_create_schedule": _handle_workflow_create_schedule,
    "workflow_list_schedules": _handle_workflow_list_schedules,
    "workflow_get_schedule": _handle_workflow_get_schedule,
    "workflow_set_schedule_enabled": _handle_workflow_set_schedule_enabled,
    "workflow_delete_schedule": _handle_workflow_delete_schedule,
    "workflow_trigger_schedule": _handle_workflow_trigger_schedule,
    # Task management handlers (JVNAUTOSCI-1040)
    "create_task": _handle_create_task,
    "get_task": _handle_get_task,
    "list_my_tasks": _handle_list_my_tasks,
    "update_task_status": _handle_update_task_status,
    "assign_task": _handle_assign_task,
}


async def main():
    """Run the MCP server over stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
