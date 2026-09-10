#!/usr/bin/env python3
"""
MCP stdio server wrapper for Vontology operations.
This provides a Model Context Protocol interface over stdin/stdout
while reusing the existing Flask endpoint logic.
"""

import sys
import os
import asyncio
import concurrent.futures
import importlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from datetime import datetime, timezone
    from src.backend.vontology.utils_vontology import (
        get_vontology_node_content,
        get_vontology_tree,
    )
    from src.backend.db.repositories.concepts_repository import ConceptsRepository
    from src.backend.services.concept_service import (
        enrich_concept_with_text_relations,
        get_concept_by_concept_id,
        get_concept_display_name_with_names_fallback,
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
        get_text_relations_summary,
        get_texts_for_concept,
    )
    from src.backend.services.annotation_extraction_service import extract_annotations
    from src.backend.services.settings_service import (
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
        _failure_case_intake_collect,
        _failure_case_reference_resolve,
        _jira_add_attachment,
        _jira_add_comment,
        _jira_create_issue,
        _jira_delete_issue_link,
        _jira_get_auth_config,
        _jira_get_bulk_operation_progress,
        _jira_get_comments,
        _jira_get_issue,
        _jira_get_project_issue_types,
        _jira_get_myself,
        _jira_get_transitions,
        _jira_link_issue,
        _jira_move_issue,
        _jira_search,
        _jira_transition_issue,
        _jira_update_issue,
        _learning_candidate_capture,
        _learning_candidate_get,
        _learning_candidate_list,
        _learning_candidate_revise,
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
        _conversation_get,
        _conversation_inspect_batch,
        _conversation_list,
        _conversation_manage,
        _conversation_manage_batch,
        _conversation_search,
        _conversation_transcript_page,
        _conversation_telemetry_get_locator,
        _mongo_query_diagnostics_report,
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
        _workflow_resume_instance,
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
    from src.backend.services.rag_service import RAGBackendUnavailable

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
    from mcp.types import Icon, Tool, TextContent
except ImportError:
    print("Error: MCP package not installed. Run: pdm add mcp", file=sys.stderr)
    sys.exit(1)

von_mcp_icons = importlib.import_module(
    "src.backend.mcp_server.icon_metadata"
).von_mcp_icons


def _bind_imports(module_name: str, names: list[str]) -> None:
    module = importlib.import_module(module_name)
    globals().update({name: getattr(module, name) for name in names})


_bind_imports("datetime", ["datetime", "timezone"])
_bind_imports(
    "src.backend.vontology.utils_vontology",
    [
        "get_vontology_node_content",
        "get_vontology_tree",
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
        "get_texts_for_concept",
        "get_text_relations_summary",
    ],
)
_bind_imports(
    "src.backend.services.annotation_extraction_service",
    ["extract_annotations"],
)
_bind_imports(
    "src.backend.services.settings_service",
    [
        "resolve_llm_setting",
        "get_preferred_language",
        "get_setting",
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
        "_jira_get_comments",
        "_jira_get_issue",
        "_jira_get_project_issue_types",
        "_jira_get_myself",
        "_jira_get_transitions",
        "_jira_link_issue",
        "_jira_move_issue",
        "_jira_search",
        "_jira_transition_issue",
        "_jira_update_issue",
        "_learning_candidate_capture",
        "_learning_candidate_get",
        "_learning_candidate_list",
        "_learning_candidate_revise",
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
        "_failure_case_intake_collect",
        "_failure_case_reference_resolve",
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
        "_conversation_get",
        "_conversation_inspect_batch",
        "_conversation_list",
        "_conversation_manage",
        "_conversation_manage_batch",
        "_conversation_search",
        "_conversation_transcript_page",
        "_conversation_telemetry_get_locator",
        "_mongo_query_diagnostics_report",
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
        "_workflow_resume_instance",
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
internal_mcp_gateway_module = importlib.import_module(
    "src.backend.integrations.internal_mcp.gateway"
)
InternalMCPGateway = internal_mcp_module.InternalMCPGateway
InternalMCPTransport = internal_mcp_module.InternalMCPTransport
build_default_catalogue = internal_mcp_module.build_default_catalogue
bind_internal_mcp_actor_context_source = (
    internal_mcp_gateway_module.bind_internal_mcp_actor_context_source
)
gmail_service = importlib.import_module("src.backend.integrations.google.gmail_service")
_bind_imports(
    "src.backend.services.rag_service",
    ["RAGBackendUnavailable"],
)

# Create MCP server instance
_VON_MCP_ICONS = von_mcp_icons(project_root)

app = Server("vontology-mcp", icons=_VON_MCP_ICONS)


_LOG = logging.getLogger(__name__)

_STDIO_MAX_RESPONSE_CHARS_DEFAULT = 100_000
_STDIO_MAX_RESPONSE_CHARS_MIN = 10_000
_VON_CHAT_RUN_MAX_WORKERS = 4
_VON_CHAT_RUN_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_VON_CHAT_RUN_MAX_WORKERS,
    thread_name_prefix="von_chat_run",
)
_VON_CHAT_RUN_WORKER_SLOTS = threading.BoundedSemaphore(
    value=_VON_CHAT_RUN_MAX_WORKERS
)


class VonChatRunCapacityUnavailable(RuntimeError):
    """Raised when all bounded ``von_chat_run`` worker slots are occupied."""


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
    icons = getattr(tool, "icons", None) or []
    return {
        "name": str(getattr(tool, "name", "")),
        "description": str(getattr(tool, "description", "")),
        "inputSchema": getattr(tool, "inputSchema", {}) or {},
        "icons": [icon.model_dump(by_alias=True, exclude_none=True) for icon in icons],
    }


def _tool_from_surface_payload(tool_payload: dict[str, Any]) -> Tool:
    input_schema = tool_payload.get("inputSchema")
    if not isinstance(input_schema, dict):
        input_schema = {}
    return Tool(
        name=str(tool_payload["name"]),
        description=str(tool_payload.get("description") or ""),
        inputSchema=input_schema,
        icons=_VON_MCP_ICONS,
    )


def _parse_icon_payloads(raw_icons: Any) -> list[Icon]:
    if not isinstance(raw_icons, list):
        return []

    icons: list[Icon] = []
    for raw_icon in raw_icons:
        if not isinstance(raw_icon, dict):
            continue
        src = raw_icon.get("src")
        if not isinstance(src, str) or not src.strip():
            continue
        sizes = raw_icon.get("sizes")
        icons.append(
            Icon(
                src=src.strip(),
                mimeType=(
                    raw_icon.get("mimeType")
                    if isinstance(raw_icon.get("mimeType"), str)
                    else None
                ),
                sizes=sizes if isinstance(sizes, list) else None,
            )
        )
    return icons


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
        icons = _parse_icon_payloads(raw.get("icons")) or _VON_MCP_ICONS
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(input_schema, dict):
            input_schema = {}
        tools.append(
            Tool(
                name=name.strip(),
                description=str(description or ""),
                inputSchema=input_schema,
                icons=icons,
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


async def _run_blocking_with_timeout(func, *, timeout_seconds: float):
    """Run a blocking callable with an advisory elapsed threshold.

    Crossing the threshold is logged, but the callable's usable result is
    retained. Explicit coroutine cancellation and callable failures still
    propagate.

    Export for testing.
    """

    import asyncio
    import os
    pid = os.getpid()
    thread_id_holder: dict[str, int | None] = {"thread_id": None}

    if not _VON_CHAT_RUN_WORKER_SLOTS.acquire(blocking=False):
        raise VonChatRunCapacityUnavailable(
            f"All {_VON_CHAT_RUN_MAX_WORKERS} supervised von_chat_run worker "
            "slots are active. No additional work was queued; retry after a "
            "running call completes, or restart the supervised MCP process if "
            "the calls are wedged."
        )

    def _wrapped():
        thread_id_holder["thread_id"] = threading.get_ident()
        try:
            return func()
        finally:
            _VON_CHAT_RUN_WORKER_SLOTS.release()

    # A shared bounded pool prevents slow calls from creating an unbounded
    # family of live threads.
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(_VON_CHAT_RUN_EXECUTOR, _wrapped)
    except BaseException:
        _VON_CHAT_RUN_WORKER_SLOTS.release()
        raise
    try:
        if timeout_seconds <= 0:
            return await future
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        _LOG.warning(
            "von_chat_run crossed its %.1fs advisory; continuing to wait "
            "(pid=%s, thread_id=%s)",
            timeout_seconds,
            pid,
            thread_id_holder["thread_id"],
        )
        return await future


class _RestrictedGateway:
    """Read-capability view of the gateway used by ``von_chat_run``."""

    def __init__(self, *, gateway: Any, allow_writes: bool) -> None:
        self._gateway: Any = gateway
        self._allow_writes = bool(allow_writes)

    @property
    def enabled(self) -> bool:
        return self._gateway.enabled

    def describe_methods(self) -> dict[str, dict[str, Any]]:
        methods = self._gateway.describe_methods()
        if self._allow_writes:
            return methods
        return {
            name: metadata
            for name, metadata in methods.items()
            if metadata.get("category") == "read"
        }

    def get_method_definition(self, method_name: str) -> Any:
        definition = self._gateway.get_method_definition(method_name)
        if (
            self._allow_writes
            or definition is None
            or getattr(definition, "category", None) == "read"
        ):
            return definition
        return None

    def get_method_timeout_sec(self, method_name: str) -> float | None:
        return self._gateway.get_method_timeout_sec(method_name)

    def invoke(
        self,
        method_name: str,
        payload: dict[str, Any] | None = None,
        *,
        deadline_monotonic: float | None = None,
        late_completion_observer: Any | None = None,
        require_effect_admission_window: bool = False,
    ):
        if not self._allow_writes:
            meta = self._gateway.describe_methods().get(method_name) or {}
            category = meta.get("category")
            if category != "read":
                raise PermissionError(
                    "von_chat_run delegates read capabilities only. Use an "
                    "explicit authorised effect tool or workflow for writes."
                )
        return self._gateway.invoke(
            method_name,
            payload,
            deadline_monotonic=deadline_monotonic,
            late_completion_observer=late_completion_observer,
            require_effect_admission_window=require_effect_admission_window,
        )


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


_TRUSTED_LOCAL_OPERATOR_GMAIL_TOOLS = frozenset(
    {
        "gmail_list_messages",
        "gmail_get_message",
        "gmail_send_message",
        "gmail_get_attachment",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_modify_labels",
    }
)

_TRUSTED_LOCAL_OPERATOR_CONVERSATION_TOOLS = frozenset(
    {
        "conversation_list",
        "conversation_get",
        "conversation_manage",
        "conversation_manage_batch",
        "conversation_search",
    }
)

# Only these named handlers inherit the local stdio operator boundary. Jira
# writes still pass their existing write-profile/effect checks; this binding
# permits their nested actor-scoped read preflight. Keep
# execution, recovery, schedules and ontology mutations on their own authority
# paths; an input flag or actor identifier must never enlarge this set.
_TRUSTED_LOCAL_OPERATOR_DIAGNOSTIC_READ_TOOLS = frozenset(
    {
        "chat_history_get_debug_entry",
        "chat_history_get_segments",
        "conversation_inspect_batch",
        "conversation_telemetry_get_locator",
        "conversation_transcript_page",
        "task_get_source_archive",
        "task_add_attachment",
        "jira_get_comments",
        "jira_get_issue",
        "jira_get_myself",
        "jira_get_transitions",
        "jira_update_issue",
        "jira_search",
        "turn_execution_get",
        "turn_execution_get_diagnostics",
        "turn_execution_get_live_progress",
        "turn_execution_list",
        "workflow_get_execution_trace",
        "workflow_get_instance",
        "workflow_list_execution_traces",
        "workflow_list_instances",
    }
)

# Creation already has project and explicit-write-intent guardrails. Its
# nested metadata read must carry the same entry-point provenance as search.
# This is not a browser capability grant or a payload-selectable operator flag.
_TRUSTED_LOCAL_OPERATOR_JIRA_CREATE_TOOLS = frozenset(
    {"jira_create_issue", "jira_get_project_issue_types"}
)

_STDIO_GOVERNED_ONTOLOGY_METHODS = {
    "create_concepts": "create_concepts",
    "upsert_text_relation": "upsert_text_relation",
    "update_text_relation": "update_text_relation",
    "delete_text_relation": "delete_text_relation",
    "upsert_singleton_text_relation": "upsert_singleton_text_relation",
    "add_names": "add_names_to_concept",
    "add_relationship": "add_relationship",
    "remove_relationship": "remove_relationship",
    "remove_relationships_bulk": "remove_relationships_bulk",
    "delete_concept": "delete_concept",
    "merge_concepts": "merge_concepts",
    "update_concept": "update_concept",
}

# A direct stdio client has no authenticated user or organisation session. Do
# not let an MCP payload make its eventual artefacts appear to have been
# created by, scoped to, or authorised by an arbitrary person or organisation.
# The shared authority resolver derives any actual actor from trusted server
# context; here the only admissible authority carrier is the opaque,
# server-issued delegation above.
_STDIO_UNTRUSTED_ONTOLOGY_AUTHORITY_FIELDS = frozenset(
    {
        "actor",
        "actor_concept_id",
        "actor_id",
        "admin",
        "administrator",
        "allow_admin",
        "authority",
        "authority_role",
        "created_by",
        "created_by_concept_id",
        "namespace",
        "global_admin",
        "is_admin",
        "is_operator",
        "organisation_concept_id",
        "organisation_id",
        "organization_concept_id",
        "organization_id",
        "org_id",
        "operator",
        "operator_override",
        "role",
        "roles",
        "user",
        "user_concept_id",
        "user_id",
    }
)


def _strip_untrusted_ontology_authority_payload(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Drop client-supplied identity/scope claims before a canonical write."""

    return {
        key: value
        for key, value in arguments.items()
        if key not in _STDIO_UNTRUSTED_ONTOLOGY_AUTHORITY_FIELDS
    }


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:  # type: ignore[misc]
    """Handle tool calls by delegating to specific handlers."""

    # Never mutate the object supplied by the MCP implementation/caller: the
    # authority carrier is removed before dispatch and should not leak into
    # nested handler use or caller-side retry state.
    parsed_arguments = dict(arguments) if isinstance(arguments, dict) else {}
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

    if name == "undo_relationship_removal":
        # This action currently resolves its target only from an undo token;
        # stdio must not execute it until that selector is bound to the shared
        # ontology-mutation authority decision.  Keeping the operator write
        # toggle above is intentional: it remains a defence-in-depth safety
        # gate, never semantic authority.
        return [
            _json_error(
                "Relationship undo is unavailable on direct stdio until its target is governed.",
                error_code="ontology_mutation_not_governed",
                suggestions=[
                    "Use a governed HTTP or internal-MCP mutation path that can bind the undo token to its canonical target.",
                    "Use preview_remove_relationship or canonical read-back to inspect the relationship first.",
                ],
            )
        ]

    try:
        if name in _NATIVE_TASK_TOOLS or name in {
            "create_task",
            "get_task",
            "list_my_tasks",
            "assign_task",
            "update_task_status",
        }:
            configured_actor = os.environ.get(
                "VON_MCP_TASK_ACTOR_CONCEPT_ID", ""
            ).strip()
            configured_org = (
                os.environ.get("VON_MCP_TASK_ORGANISATION_CONCEPT_ID", "").strip()
                or None
            )
            inherited_source = (
                internal_mcp_gateway_module.get_internal_mcp_actor_context_source()
            )
            if configured_actor and inherited_source is None:
                # A local server installation may bind its task tools to its
                # operator once. Per-call payloads cannot select a principal;
                # this also lets native task work continue with Jira offline.
                if not configured_actor.startswith("#V#"):
                    return [
                        _json_error(
                            "Invalid configured task actor",
                            error_code="invalid_task_actor_configuration",
                        )
                    ]
                if configured_org and not configured_org.startswith("#V#"):
                    return [
                        _json_error(
                            "Invalid configured task organisation",
                            error_code="invalid_task_actor_configuration",
                        )
                    ]
                actor_fields = (
                    "acting_user_concept_id",
                    "actor_concept_id",
                    "created_by_concept_id",
                    "creator_concept_id",
                    "added_by_concept_id",
                    "author_concept_id",
                    "user_concept_id",
                )
                if any(
                    parsed_arguments.get(key) not in (None, "", configured_actor)
                    for key in actor_fields
                ):
                    return [
                        _json_error(
                            "Task actor differs from the configured local principal",
                            error_code="task_actor_mismatch",
                        )
                    ]
                from src.backend.security.access_control import (
                    override_current_actor,
                    force_access_control_enforcement,
                )

                task_arguments = dict(parsed_arguments)
                task_arguments.update({key: configured_actor for key in actor_fields})
                if not task_arguments.get("request_id"):
                    from uuid import uuid4

                    task_arguments["request_id"] = f"task-mcp-{uuid4().hex}"
                with (
                    override_current_actor(configured_actor, configured_org),
                    force_access_control_enforcement(),
                    internal_mcp_gateway_module.bind_internal_mcp_actor_context_source(
                        internal_mcp_gateway_module.INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
                        preexisting_actor_context=(configured_actor, configured_org),
                    ),
                ):
                    return await handler(task_arguments)
        governed_method = _STDIO_GOVERNED_ONTOLOGY_METHODS.get(name)
        if governed_method:
            # Stdio has no independently authenticated actor context. Binding
            # a delegation grantor before matching caller arguments to the
            # stored exact intent would expose a private read oracle, so this
            # surface remains fail-closed until it executes server-stored
            # canonical arguments directly.
            return [
                _json_text(
                    {
                        "success": False,
                        "effect_status": "not_started",
                        "mutation_outcome": "not_started",
                        "changed": False,
                        "error_code": ("ontology_sessionless_delegation_not_supported"),
                        "error": (
                            "Sessionless ontology delegation execution is "
                            "unavailable; use an actor-bound trusted surface."
                        ),
                    }
                )
            ]
        if name in (
            _TRUSTED_LOCAL_OPERATOR_DIAGNOSTIC_READ_TOOLS
            | _TRUSTED_LOCAL_OPERATOR_JIRA_CREATE_TOOLS
        ):
            from src.backend.security.access_control import (
                get_effective_organisation_concept_id,
                get_effective_user_concept_id,
            )

            # A supplied delegation keeps its narrower target and validation;
            # never silently replace an existing actor context with operator.
            source = internal_mcp_gateway_module.get_internal_mcp_actor_context_source()
            actor = (
                internal_mcp_gateway_module.get_internal_mcp_preexisting_actor_context()
            )
            actor = actor or (
                get_effective_user_concept_id(),
                get_effective_organisation_concept_id(),
            )
            has_reference = any(
                key in parsed_arguments
                for key in (
                    "conversation_ref",
                    "history_location_ref",
                    "turn_telemetry_ref",
                )
            )
            if has_reference:
                source = "tool_payload_fallback"
            elif source is None:
                source = (
                    "preexisting_authenticated_or_workflow_context"
                    if any(actor)
                    else internal_mcp_gateway_module.INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE
                )
            with bind_internal_mcp_actor_context_source(
                source, preexisting_actor_context=actor if any(actor) else None
            ):
                return await handler(parsed_arguments)
        if name in (
            _TRUSTED_LOCAL_OPERATOR_GMAIL_TOOLS
            | _TRUSTED_LOCAL_OPERATOR_CONVERSATION_TOOLS
            | {"otter_collect_now", "otter_collection_status"}
        ):
            # This stdio server is the deliberately local coding/operator
            # surface. Bind that provenance only around explicitly listed
            # actor-scoped handlers; generic chat/workflow proxy calls retain
            # their own provenance and cannot inherit operator authority.
            with bind_internal_mcp_actor_context_source(
                internal_mcp_gateway_module.INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE
            ):
                return await handler(parsed_arguments)
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


def _get_stdio_max_response_chars() -> int:
    raw_value = os.getenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS")
    if raw_value:
        try:
            parsed = int(raw_value)
        except ValueError:
            parsed = _STDIO_MAX_RESPONSE_CHARS_DEFAULT
        return max(_STDIO_MAX_RESPONSE_CHARS_MIN, parsed)
    return _STDIO_MAX_RESPONSE_CHARS_DEFAULT


def _json_text(payload: Any) -> TextContent:
    # Compact separators: indentation is pure transport cost on a machine-read
    # channel and roughly doubles payload size against the guard below.
    text = json.dumps(payload, separators=(",", ":"), default=str)
    max_response_chars = _get_stdio_max_response_chars()
    if len(text) <= max_response_chars:
        return TextContent(type="text", text=text)

    guarded_payload = make_error_response(
        error_code="payload_too_large",
        message=(
            "MCP tool response exceeded the safe stdio payload size; request a "
            "bounded projection or explicit detail section."
        ),
        details={
            "approximate_response_chars": len(text),
            "max_response_chars": max_response_chars,
            "response_guard": "vontology_stdio_text_content",
        },
        suggestions=[
            "For live turn progress, call turn_execution_get_live_progress without a section for the bounded snapshot.",
            "For live turn progress details, call turn_execution_get_live_progress with section, limit, and offset.",
            "For Jira comments, call jira_get_comments with start_at and max_results instead of jira_get_issue with fields=['comment'].",
            "Use a narrower query or paginated detail tool for large read results.",
        ],
    )
    return TextContent(
        type="text",
        text=json.dumps(guarded_payload, separators=(",", ":"), default=str),
    )


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
        "note": "Standalone stdio has no browser login session; any explicit user or organisation context is caller-scoped.",
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
    import secrets

    from src.backend.services.adaptive_turn_service import (
        execute_adaptive_turn,
    )
    from src.backend.languagemodels.llm_interface import (
        get_active_model_name,
        get_llm_client,
    )
    from src.backend.services.workflow_actor_scope_service import (
        WorkflowActorScopeError,
        resolve_provenance_bound_workflow_actor_scope,
    )
    from src.backend.integrations.internal_mcp.gateway import (
        get_internal_mcp_actor_context_source,
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

    # Raw stdio arguments are transport payload, not authenticated actor
    # authority.  Reject identity-bearing chat runs before model, retrieval, or
    # workflow work.  The deliberately configured local-operator gateway is
    # the sole payload-only identity exception.
    inherited_actor_source = get_internal_mcp_actor_context_source()
    actor_context_source = (
        "trusted_operator_payload_fallback"
        if inherited_actor_source == "trusted_operator_payload_fallback"
        else "tool_payload_fallback"
    )
    try:
        actor_scope = resolve_provenance_bound_workflow_actor_scope(
            claimed_user_id=user_concept_id,
            claimed_org_id=org_concept_id,
            claimed_namespace=user_namespace,
            actor_context_source=actor_context_source,
            preexisting_actor_context=None,
        )
    except WorkflowActorScopeError as exc:
        return [
            _json_text(
                {
                    "success": False,
                    "error": exc.reason,
                    "error_code": exc.reason,
                    "workflow_actor_scope": {
                        "schema_version": "workflow_actor_scope_resolution.v1",
                        "status": "rejected",
                        "reason": exc.reason,
                        "mismatch_fields": list(exc.mismatch_fields),
                    },
                }
            )
        ]
    user_concept_id = actor_scope.user_concept_id
    org_concept_id = actor_scope.organisation_concept_id
    user_namespace = actor_scope.namespace
    requested_gmail_profile = (
        arguments.get("gmail_profile")
        if isinstance(arguments.get("gmail_profile"), str)
        else None
    )
    gmail_profile: str | None = None
    if user_concept_id:
        from src.backend.integrations.google.gmail_service import (
            list_profile_ids_from_env,
        )
        from src.backend.services.mail_profile_resource_vontology_service import (
            resolve_authorised_gmail_profile_for_user,
        )
        from src.backend.security.access_control import override_current_actor

        with override_current_actor(user_concept_id, org_concept_id):
            gmail_authority = resolve_authorised_gmail_profile_for_user(
                user_concept_id=user_concept_id,
                requested_profile_id=requested_gmail_profile,
            )
        resolved_profile = gmail_authority.get("profile_id")
        if gmail_authority.get("success") and isinstance(
            resolved_profile,
            str,
        ):
            if resolved_profile in set(list_profile_ids_from_env()):
                gmail_profile = resolved_profile
            elif requested_gmail_profile:
                return [
                    _json_text(
                        {
                            "success": False,
                            "error_code": (
                                "authorised_gmail_profile_unavailable"
                            ),
                            "error": (
                                "The requested Gmail profile is represented "
                                "as authorised for this actor but is not "
                                "configured in this runtime."
                            ),
                        }
                    )
                ]
        elif requested_gmail_profile:
            return [
                _json_text(
                    {
                        "success": False,
                        "error_code": "gmail_profile_not_authorised",
                        "error": (
                            "The requested Gmail profile is not represented "
                            "as authorised for this actor."
                        ),
                    }
                )
            ]
    elif requested_gmail_profile:
        return [
            _json_text(
                {
                    "success": False,
                    "error_code": "gmail_profile_authentication_required",
                    "error": (
                        "Selecting a Gmail profile requires an authenticated "
                        "actor."
                    ),
                }
            )
        ]
    auxiliary_system_prompt = (
        arguments.get("auxiliary_system_prompt")
        if isinstance(arguments.get("auxiliary_system_prompt"), str)
        else None
    )

    _write_access_allowed, access_profile, _write_policy = _evaluate_stdio_write_access(
        "von_chat_run",
        arguments,
    )
    access_profile_summary = _build_access_profile_summary(access_profile)

    raw_allow_writes = arguments.get("allow_writes")
    allow_writes_requested = (
        _coerce_bool_argument(raw_allow_writes, default=False)
        if raw_allow_writes is not None
        else False
    )
    if allow_writes_requested:
        return [
            _json_text(
                {
                    "success": False,
                    "error_code": "von_chat_run_read_only",
                    "error": (
                        "von_chat_run is an adaptive read-only entry point. Use "
                        "an explicit authorised effect tool or workflow for writes."
                    ),
                    "dry_run": True,
                    "allow_writes": False,
                    "access_profile": access_profile_summary,
                }
            )
        ]
    dry_run = True
    effective_allow_writes = False

    try:
        max_string_chars = int(arguments.get("max_string_chars", 8000))
    except Exception:
        max_string_chars = 8000
    max_string_chars = max(256, min(20000, max_string_chars))

    try:
        timeout_seconds = float(
            arguments.get("advisory_seconds", arguments.get("timeout_seconds", 90))
        )
    except Exception:
        timeout_seconds = 90.0
    timeout_seconds = max(1.0, min(600.0, timeout_seconds))

    raw_context = arguments.get("context")
    context = (
        [dict(item) for item in raw_context if isinstance(item, dict)]
        if isinstance(raw_context, list)
        else []
    )
    preferred_language = get_preferred_language()
    if isinstance(preferred_language, str) and preferred_language.strip():
        context.append(
            {
                "role": "system",
                "content": (
                    "Trusted response-language preference: "
                    f"{preferred_language.strip()}."
                ),
            }
        )
    if auxiliary_system_prompt and auxiliary_system_prompt.strip():
        # This field is caller payload, not server authority. Preserve it as
        # ordinary context rather than escalating it to a system instruction.
        context.append(
            {
                "role": "user",
                "content": (
                    "Caller-provided supplementary context:\n"
                    f"{auxiliary_system_prompt.strip()}"
                ),
            }
        )

    llm_client = get_llm_client()
    catalogue = build_default_catalogue()
    transport = InternalMCPTransport()
    base_gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=transport,
        enabled=True,
        trusted_actor_payload_fallback=(
            actor_context_source == "trusted_operator_payload_fallback"
        ),
    )
    gateway = _RestrictedGateway(
        gateway=base_gateway,
        allow_writes=False,
    )

    try:

        def _run_adaptive_turn_sync():
            # ContextVars are not propagated by run_in_executor.  Bind actor
            # provenance inside the worker thread so each delegated read sees
            # the same trusted authority boundary.
            with bind_internal_mcp_actor_context_source(actor_context_source):
                return execute_adaptive_turn(
                    gateway=gateway,  # type: ignore[arg-type]
                    prompt=prompt.strip(),
                    context=context,
                    llm_client=llm_client,
                    model=model_name,
                    model_parameters=None,
                    user_namespace=user_namespace,
                    user_concept_id=user_concept_id,
                    org_concept_id=org_concept_id,
                    trusted_argument_values=(
                        {"gmail_profile": gmail_profile}
                        if gmail_profile
                        else None
                    ),
                    turn_id=(
                        arguments.get("turn_id").strip()
                        if isinstance(arguments.get("turn_id"), str)
                        and arguments.get("turn_id").strip()
                        else f"von-chat-run-{secrets.token_urlsafe(12)}"
                    ),
                    turn_budget_seconds=timeout_seconds,
                    final_synthesis_reserve_seconds=min(
                        30.0,
                        max(0.2, timeout_seconds * 0.2),
                    ),
                )

        outer_advisory_seconds = timeout_seconds + 1.0
        outer_started_at = time.perf_counter()
        adaptive_result = await _run_blocking_with_timeout(
            _run_adaptive_turn_sync,
            timeout_seconds=outer_advisory_seconds,
        )
        outer_elapsed_seconds = time.perf_counter() - outer_started_at
        completed = adaptive_result.terminal_status == "completed"
        payload = {
            "success": completed,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
            "delegation": "read_only",
            "access_profile": access_profile_summary,
            "timeout_seconds": timeout_seconds,
            "advisory_seconds_requested": timeout_seconds,
            "elapsed_time_enforcement": "advisory",
            "advisory_seconds": outer_advisory_seconds,
            "advisory_exceeded": outer_elapsed_seconds > outer_advisory_seconds,
            "hard_timeout_seconds": None,
            "terminal_status": adaptive_result.terminal_status,
            "response_text": _truncate_string(
                adaptive_result.response_text, max_chars=max_string_chars
            ),
            "tool_invocations": _redact_debug_value(
                list(adaptive_result.tool_invocations),
                max_string_chars=max_string_chars,
            ),
            "tool_messages": _redact_debug_value(
                list(adaptive_result.extra_messages),
                max_string_chars=max_string_chars,
            ),
            "evidence_index": _redact_debug_value(
                list(adaptive_result.evidence_index),
                max_string_chars=max_string_chars,
            ),
            "llm_usage": dict(adaptive_result.llm_usage or {}),
        }
        if not completed:
            payload["error_code"] = adaptive_result.terminal_status
            payload["error"] = adaptive_result.response_text
    except Exception as exc:
        capacity_unavailable = isinstance(exc, VonChatRunCapacityUnavailable)
        payload = {
            "success": False,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
            "delegation": "read_only",
            "access_profile": access_profile_summary,
            "timeout_seconds": timeout_seconds,
            "elapsed_time_enforcement": "advisory",
            "advisory_seconds_requested": timeout_seconds,
            "hard_timeout_seconds": None,
            "error_code": (
                "von_chat_run_capacity_exhausted"
                if capacity_unavailable
                else type(exc).__name__
            ),
            "error": str(exc),
        }
        if capacity_unavailable:
            payload.update(
                {
                    "retryable": True,
                    "capacity_boundary": "bounded_worker_admission",
                    "active_worker_limit": _VON_CHAT_RUN_MAX_WORKERS,
                }
            )

    return [_json_text(payload)]


async def _handle_upsert_text_relation(arguments: dict[str, Any]) -> list[TextContent]:
    """Use the same governed command as chat, workflow, and internal MCP."""

    return [
        _json_text(internal_mcp_catalogue_module._upsert_text_relation(**arguments))
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
        from src.backend.security.access_control import (
            force_access_control_enforcement,
        )

        with force_access_control_enforcement():
            relations = get_texts_for_concept(
                subject_concept_id=concept_id,
                predicate=predicate,
                lang=language,
                limit=limit,
                # Direct stdio calls have no authenticated actor scope. Keep
                # this surface on the globally visible base publication view.
                context_view="base_publication",
            )

        # Add text previews for long content
        for relation in relations:
            text = relation.get("text", "")
            if len(text) > 200:
                relation["text_preview"] = text[:200] + "..."

        payload = {
            "concept_id": concept_id,
            "context_view": "base_publication",
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
    """Use the shared canonical mutation command once catalogue-governed."""

    return [
        _json_text(internal_mcp_catalogue_module._update_text_relation(**arguments))
    ]


async def _handle_delete_text_relation(arguments: dict[str, Any]) -> list[TextContent]:
    """Use the same governed command as chat, workflow, and internal MCP."""

    return [
        _json_text(internal_mcp_catalogue_module._delete_text_relation(**arguments))
    ]


async def _handle_add_names(arguments: dict[str, Any]) -> list[TextContent]:
    return [
        _json_text(internal_mcp_catalogue_module._add_names_to_concept(**arguments))
    ]


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
        from src.backend.security.access_control import (
            force_access_control_enforcement,
        )

        with force_access_control_enforcement():
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
            include_relations_any_arg = bool(
                arguments.get("include_relations_any_arg")
            )
            include_text_relations_arg1 = arguments.get(
                "include_text_relations_arg1", False
            )
            predicate_filter = arguments.get("predicate_filter")
            limit = arguments.get("limit")
            offset = arguments.get("offset")
            include_concept_preview = arguments.get(
                "include_concept_preview", True
            )
            include_uncertain = bool(arguments.get("include_uncertain", False))
            uncertainty_mode = arguments.get("uncertainty_mode")
            uncertainty_statuses = arguments.get("uncertainty_statuses")

            if predicate_filter is not None and not isinstance(
                predicate_filter, list
            ):
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
        from src.backend.security.access_control import (
            force_access_control_enforcement,
        )

        with force_access_control_enforcement():
            payload = find_relations_with_argument(
                concept_id=str(concept_id),
                argument_index=arguments.get("argument_index"),
                predicate_filter=arguments.get("predicate_filter"),
                relation_kind=arguments.get("relation_kind"),
                scope=arguments.get("scope"),
                include_text_snippets=bool(
                    arguments.get("include_text_snippets", False)
                ),
                include_concept_preview=bool(
                    arguments.get("include_concept_preview", False)
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
                arguments.get("include_concept_preview", False)
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
        from src.backend.security.access_control import (
            force_access_control_enforcement,
        )

        with force_access_control_enforcement():
            payload = get_text_relations_summary(
                concept_id,
                predicates=arguments.get("predicates"),
                languages=arguments.get("languages"),
                max_relation_ids_per_group=arguments.get(
                    "max_relation_ids_per_group", 25
                ),
                # Direct stdio calls have no authenticated actor scope.
                context_view="base_publication",
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
    return [
        _json_text(
            internal_mcp_catalogue_module._upsert_singleton_text_relation(
                **arguments
            )
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
        result = internal_mcp_catalogue_module._gmail_list_messages(
            **arguments,
            _audit_source="mcp_stdio",
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
        # Catalogue projection strips raw MIME/base64 and applies the shared
        # profile invocation-authority boundary.  ``call_tool`` binds this
        # direct stdio surface as the narrow trusted-local operator route.
        result = internal_mcp_catalogue_module._gmail_get_message(
            **arguments,
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
        arguments.get("to") or arguments.get("recipient") or arguments.get("recipients")
    )
    subject = arguments.get("subject")
    body_text = (
        arguments.get("body_text") or arguments.get("body") or arguments.get("message")
    )
    profile_text = profile if isinstance(profile, str) and profile.strip() else None
    to_value = to if isinstance(to, (str, list)) and to else None
    subject_text = subject if isinstance(subject, str) and subject.strip() else None
    body_text_value = (
        body_text if isinstance(body_text, str) and body_text.strip() else None
    )
    request_id = arguments.get("request_id")
    request_id_value = (
        request_id if isinstance(request_id, str) and request_id.strip() else None
    )
    allow_send = bool(arguments.get("allow_send"))
    missing = [
        field
        for field, value in (
            ("profile", profile_text),
            ("to", to_value),
            ("subject", subject_text),
            ("body_text", body_text_value),
            ("request_id", request_id_value),
        )
        if value in (None, "")
    ]
    if missing:
        return [
            _json_error(
                "Missing required parameters for Gmail send",
                error_code="missing_parameter",
                suggestions=[
                    "Provide profile, to, subject, body_text, request_id, and allow_send=true",
                ],
            )
        ]
    assert profile_text is not None
    assert to_value is not None
    assert subject_text is not None
    assert body_text_value is not None
    assert request_id_value is not None
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
        # Reuse the canonical catalogue handler so the trusted-local operator
        # path receives the same represented-identity, quota, idempotency and
        # finality controls as ordinary Von conversations.
        result = internal_mcp_catalogue_module._gmail_send_message(
            **arguments,
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
        from src.backend.services.file_bytes_text_projection_service import (
            DEFAULT_MAX_TEXT_CHARS,
            MAX_TEXT_CHARS,
            extract_file_bytes_text_projection,
        )

        max_bytes, limit_error = (
            internal_mcp_catalogue_module._gmail_attachment_positive_int(
                arguments.get("max_bytes"),
                field_name="max_bytes",
                default=(
                    internal_mcp_catalogue_module._DEFAULT_GMAIL_ATTACHMENT_MAX_BYTES
                ),
                maximum=(
                    internal_mcp_catalogue_module._MAX_GMAIL_ATTACHMENT_MAX_BYTES
                ),
            )
        )
        if limit_error is not None:
            return [_json_text(limit_error)]
        max_text_chars, limit_error = (
            internal_mcp_catalogue_module._gmail_attachment_positive_int(
                arguments.get("max_text_chars"),
                field_name="max_text_chars",
                default=DEFAULT_MAX_TEXT_CHARS,
                maximum=MAX_TEXT_CHARS,
            )
        )
        if limit_error is not None:
            return [_json_text(limit_error)]
        assert max_bytes is not None
        assert max_text_chars is not None

        source = internal_mcp_catalogue_module._gmail_attachment_source_payload(
            profile=str(profile),
            message_id=str(message_id),
            attachment_id=str(attachment_id),
            namespace=str(arguments.get("namespace") or "trusted_local_operator"),
            tool_name="gmail_get_attachment",
            max_bytes=max_bytes,
            audit_source="mcp_stdio",
        )
        if source.get("success") is not True:
            return [_json_text(source)]
        attachment_bytes = source.get("_bytes")
        if not isinstance(attachment_bytes, bytes):
            return [
                _json_error(
                    "Gmail did not return decodable attachment bytes",
                    error_code="gmail_attachment_decode_failed",
                )
            ]
        result = internal_mcp_catalogue_module._gmail_attachment_public_source(
            source
        )
        result.update(
            extract_file_bytes_text_projection(
                data=attachment_bytes,
                content_type=(
                    source.get("content_type")
                    if isinstance(source.get("content_type"), str)
                    else None
                ),
                original_filename=(
                    source.get("filename")
                    if isinstance(source.get("filename"), str)
                    else None
                ),
                max_text_chars=max_text_chars,
            )
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
            exact_name=arguments.get("exact_name"),
            require_exact_match=arguments.get("require_exact_match") is True,
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


async def _handle_gmail_create_label(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    name = arguments.get("name") or arguments.get("label_name")
    allow_mutation = arguments.get("allow_mutation") is True
    if not profile or not (isinstance(name, str) and name.strip()):
        return [
            _json_error(
                "Missing required parameters: profile and name",
                error_code="missing_parameter",
                suggestions=[
                    "Provide both profile ID and label name",
                    "Set allow_mutation=true only when label creation is authorised",
                ],
            )
        ]
    if not allow_mutation:
        return [
            _json_error(
                "allow_mutation must be true to create Gmail labels",
                error_code="mutation_not_allowed",
                suggestions=["Set allow_mutation=true to confirm label creation"],
            )
        ]
    try:
        result = gmail_service.create_label(
            profile_id=profile,
            name=name,
            label_list_visibility=arguments.get("label_list_visibility"),
            message_list_visibility=arguments.get("message_list_visibility"),
            allow_mutation=allow_mutation,
            audit_context=_gmail_audit_context("gmail_create_label"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [
            _json_error(
                f"Gmail create label failed: {exc}",
                error_code="gmail_error",
                suggestions=[
                    "Verify the profile ID is correct",
                    "Ensure Gmail integration is configured with gmail.modify scope",
                    "Use gmail_list_labels if the label may already exist",
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
            verify_after=arguments.get("verify_after") is True,
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
    result = internal_mcp_catalogue_module._delete_concept(
        concept_id=concept_id,
        simulate=simulate,
    )
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
    result = internal_mcp_catalogue_module._merge_concepts(
        source_id=source_id,
        target_id=target_id,
        simulate=simulate,
    )
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
        result = internal_mcp_catalogue_module._update_concept(
            concept_id=concept_id,
            update_data=update_data,
        )
        return [_json_text(result)]
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
        from src.backend.security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        inherited_preexisting_actor = (
            internal_mcp_gateway_module.get_internal_mcp_preexisting_actor_context()
        )
        effective_user_id = get_effective_user_concept_id()
        effective_org_id = get_effective_organisation_concept_id()
        server_bound_actor = inherited_preexisting_actor or (
            (effective_user_id, effective_org_id)
            if effective_user_id or effective_org_id
            else None
        )
        inherited_source = (
            internal_mcp_gateway_module.get_internal_mcp_actor_context_source()
        )
        actor_context_source = (
            "preexisting_authenticated_or_workflow_context"
            if server_bound_actor is not None
            else "trusted_operator_payload_fallback"
            if inherited_source == "trusted_operator_payload_fallback"
            else "tool_payload_fallback"
        )
        with bind_internal_mcp_actor_context_source(
            actor_context_source,
            preexisting_actor_context=server_bound_actor,
        ):
            result = internal_mcp_catalogue_module._search_knowledge_base(
                **arguments
            )
        return [_json_text(result)]
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


def _run_diagnostic_read_proxy_handler(
    handler: Callable[..., Any],
    arguments: dict[str, Any],
    **kwargs: Any,
) -> list[TextContent]:
    """Preserve entry-point authority; a direct handler call cannot mint it."""

    source = internal_mcp_gateway_module.get_internal_mcp_actor_context_source()
    actor = internal_mcp_gateway_module.get_internal_mcp_preexisting_actor_context()
    with bind_internal_mcp_actor_context_source(
        source or "tool_payload_fallback", preexisting_actor_context=actor
    ):
        # Reuse the signed-reference transport pager for local operator reads.
        # Do not page errors or re-page an already bounded delegated response.
        def bounded_handler(**payload_args: Any) -> Any:
            result = handler(**payload_args)
            if (
                isinstance(result, dict)
                and result.get("success") is not False
                and "bounded_read" not in result
                and len(json.dumps(result, separators=(",", ":"), default=str))
                > _get_stdio_max_response_chars()
            ):
                return internal_mcp_catalogue_module._bounded_telemetry_payload(
                    result, arguments=arguments,
                    artifact_kind=handler.__name__.lstrip("_"),
                    preserve_inline_below_limit=True,
                )
            return result

        return _run_catalogue_proxy_handler(bounded_handler, arguments, **kwargs)


def _run_untrusted_workflow_proxy_handler(
    handler: Callable[..., Any],
    arguments: dict[str, Any],
) -> list[TextContent]:
    """Run a raw stdio workflow call without promoting payload actor claims."""

    with bind_internal_mcp_actor_context_source("tool_payload_fallback"):
        return _run_catalogue_proxy_handler(
            handler,
            arguments,
            tool_family_label="Workflow",
        )


async def _handle_jira_search(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _jira_search,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_comments(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _jira_get_comments,
        arguments,
        tool_family_label="Jira",
        suggestions=["Check Jira authentication and network connectivity"],
    )


async def _handle_jira_get_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
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
    return _run_untrusted_workflow_proxy_handler(
        _workflow_list_definitions,
        arguments,
    )


async def _handle_workflow_validate_candidate(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_validate_candidate,
        arguments,
    )


async def _handle_workflow_list_use_episodes(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_list_use_episodes,
        arguments,
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
    return _run_untrusted_workflow_proxy_handler(
        _workflow_bind_event,
        arguments,
    )


async def _handle_workflow_list_event_bindings(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_list_event_bindings,
        arguments,
    )


async def _handle_workflow_set_event_binding_enabled(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_set_event_binding_enabled,
        arguments,
    )


async def _handle_workflow_delete_event_binding(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_delete_event_binding,
        arguments,
    )


async def _handle_workflow_mcp_health_check(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_mcp_health_check,
        arguments,
    )


async def _handle_coding_agent_mcp_access_profile(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _coding_agent_mcp_access_profile,
        arguments,
        tool_family_label="Internal",
    )


async def _handle_mongo_query_diagnostics_report(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _mongo_query_diagnostics_report,
        arguments,
        tool_family_label="Internal",
    )


async def _handle_workflow_materialisation_diagnostics(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_materialisation_diagnostics,
        arguments,
    )


async def _handle_workflow_concept_parity_audit(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_concept_parity_audit,
        arguments,
    )


async def _handle_workflow_create_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_create_instance,
        arguments,
    )


async def _handle_workflow_execute(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_execute,
        arguments,
    )


async def _handle_workflow_list_instances(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _workflow_list_instances,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_workflow_list_execution_traces(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _workflow_list_execution_traces,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_workflow_build_prediction_envelope(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_build_prediction_envelope,
        arguments,
    )


async def _handle_workflow_get_instance(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _workflow_get_instance,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_workflow_get_execution_trace(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _workflow_get_execution_trace,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_workflow_cancel_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_cancel_instance,
        arguments,
    )


async def _handle_workflow_resume_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_resume_instance,
        arguments,
    )


async def _handle_workflow_retry_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_retry_instance,
        arguments,
    )


async def _handle_workflow_create_schedule(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_create_schedule,
        arguments,
    )


async def _handle_workflow_list_schedules(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_list_schedules,
        arguments,
    )


async def _handle_workflow_get_schedule(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_get_schedule,
        arguments,
    )


async def _handle_workflow_set_schedule_enabled(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_set_schedule_enabled,
        arguments,
    )


async def _handle_workflow_delete_schedule(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_delete_schedule,
        arguments,
    )


async def _handle_workflow_trigger_schedule(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _workflow_trigger_schedule,
        arguments,
    )


async def _handle_chat_history_get_segments(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _chat_history_get_segments,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_chat_history_get_debug_entry(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _chat_history_get_debug_entry,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_conversation_list(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _conversation_list,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_get(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _conversation_get,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_transcript_page(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _conversation_transcript_page,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_inspect_batch(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _conversation_inspect_batch,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_search(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _conversation_search,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_manage(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _conversation_manage,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_manage_batch(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _conversation_manage_batch,
        arguments,
        tool_family_label="Conversation",
    )


async def _handle_conversation_telemetry_get_locator(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _conversation_telemetry_get_locator,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_turn_execution_list(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _turn_execution_list,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_turn_execution_get(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _turn_execution_get,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_turn_execution_get_diagnostics(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _turn_execution_get_diagnostics,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_failure_case_intake_collect(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _failure_case_intake_collect,
        arguments,
        tool_family_label="FailureCase",
    )


async def _handle_failure_case_reference_resolve(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _failure_case_reference_resolve,
        arguments,
        tool_family_label="FailureCase",
    )


async def _handle_turn_execution_get_live_progress(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_diagnostic_read_proxy_handler(
        _turn_execution_get_live_progress,
        arguments,
        tool_family_label="Diagnostics",
    )


async def _handle_turn_execution_get_critic_bundle(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_get_critic_bundle,
        arguments,
    )


async def _handle_turn_execution_search_failures(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_search_failures,
        arguments,
    )


async def _handle_turn_execution_build_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_build_benchmark,
        arguments,
    )


async def _handle_turn_execution_build_selector_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_build_selector_benchmark,
        arguments,
    )


async def _handle_turn_execution_build_context_answering_benchmark(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_build_context_answering_benchmark,
        arguments,
    )


async def _handle_turn_execution_build_dashboard(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_build_dashboard,
        arguments,
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
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_backfill_from_chat_history,
        arguments,
    )


async def _handle_turn_execution_namespace_coverage_report(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _turn_execution_namespace_coverage_report,
        arguments,
    )


async def _handle_experiment_run_list(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_run_list,
        arguments,
    )


async def _handle_experiment_run_get(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_run_get,
        arguments,
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


async def _handle_learning_candidate_capture(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _learning_candidate_capture,
        arguments,
    )


async def _handle_learning_candidate_get(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _learning_candidate_get,
        arguments,
    )


async def _handle_learning_candidate_list(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _learning_candidate_list,
        arguments,
    )


async def _handle_learning_candidate_revise(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _learning_candidate_revise,
        arguments,
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
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_create_slice,
        arguments,
    )


async def _handle_testing_theory_import_canonical_context(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_import_canonical_context,
        arguments,
    )


async def _handle_testing_theory_assert_local_claims(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_assert_local_claims,
        arguments,
    )


async def _handle_testing_theory_compute_diff(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_compute_diff,
        arguments,
    )


async def _handle_testing_theory_rollback_local_writes(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_rollback_local_writes,
        arguments,
    )


async def _handle_testing_theory_promote_validated_claims(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_promote_validated_claims,
        arguments,
    )


async def _handle_testing_theory_gc_expired(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_theory_gc_expired,
        arguments,
    )


async def _handle_experiment_create_spec(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_create_spec,
        arguments,
    )


async def _handle_experiment_start_run(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_start_run,
        arguments,
    )


async def _handle_experiment_record_observation(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_record_observation,
        arguments,
    )


async def _handle_experiment_compute_verdict(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_compute_verdict,
        arguments,
    )


async def _handle_experiment_emit_learning_signal(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_emit_learning_signal,
        arguments,
    )


async def _handle_experiment_execute_target_workflow(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_execute_target_workflow,
        arguments,
    )


async def _handle_experiment_execute_regression_suite(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _experiment_execute_regression_suite,
        arguments,
    )


async def _handle_testing_prepare_meeting_invitation_spec(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_prepare_meeting_invitation_spec,
        arguments,
    )


async def _handle_testing_prepare_experiment_spec(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_prepare_experiment_spec,
        arguments,
    )


async def _handle_testing_prepare_arxiv_paper_ingestion_fixture(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_prepare_arxiv_paper_ingestion_fixture,
        arguments,
    )


async def _handle_testing_verify_arxiv_paper_ingestion_result(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_verify_arxiv_paper_ingestion_result,
        arguments,
    )


async def _handle_testing_cleanup_arxiv_paper_ingestion_artifacts(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_untrusted_workflow_proxy_handler(
        _testing_cleanup_arxiv_paper_ingestion_artifacts,
        arguments,
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
            project_concept_id=arguments.get("project_concept_id"),
            collection_concept_ids=arguments.get("collection_concept_ids"),
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


async def _handle_otter_collect_now(arguments: dict[str, Any]) -> list[TextContent]:
    from src.backend.integrations.internal_mcp.otter_collection_tools import call_collection
    return [_json_text(call_collection("enqueue", **arguments))]


async def _handle_otter_collection_status(arguments: dict[str, Any]) -> list[TextContent]:
    from src.backend.integrations.internal_mcp.otter_collection_tools import call_collection
    return [_json_text(call_collection("status", **arguments))]


def _native_task_handler(name):
    async def handle(arguments):
        from src.backend.integrations.internal_mcp import catalogue

        # Reuse the canonical task handlers. The stdio write profile is checked
        # by call_tool before reaching this adapter; actor-bound callers retain
        # their context and ordinary continuation checks.
        result = getattr(catalogue, f"_{name}")(**arguments)
        return [_json_text(result)]

    return handle


_NATIVE_TASK_TOOLS = (
    "task_get",
    "task_search",
    "task_update_fields",
    "task_get_transitions",
    "task_transition",
    "task_create_subtask",
    "task_set_parent",
    "task_link",
    "task_unlink",
    "task_add_comment",
    "task_list_comments",
    "task_add_attachment",
    "task_list_attachments",
    "task_add_worklog",
    "task_list_worklog",
    "task_get_history",
    "task_list_projects",
    "task_get_project",
    "task_get_collection",
    "task_get_source_archive",
)


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[list[TextContent]]]] = {
    **{name: _native_task_handler(name) for name in _NATIVE_TASK_TOOLS},
    "otter_collect_now": _handle_otter_collect_now,
    "otter_collection_status": _handle_otter_collection_status,
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
    "gmail_create_label": _handle_gmail_create_label,
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
    "learning_candidate_capture": _handle_learning_candidate_capture,
    "learning_candidate_get": _handle_learning_candidate_get,
    "learning_candidate_list": _handle_learning_candidate_list,
    "learning_candidate_revise": _handle_learning_candidate_revise,
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
    "conversation_list": _handle_conversation_list,
    "conversation_search": _handle_conversation_search,
    "conversation_get": _handle_conversation_get,
    "conversation_transcript_page": _handle_conversation_transcript_page,
    "conversation_inspect_batch": _handle_conversation_inspect_batch,
    "conversation_manage": _handle_conversation_manage,
    "conversation_manage_batch": _handle_conversation_manage_batch,
    "conversation_telemetry_get_locator": _handle_conversation_telemetry_get_locator,
    "turn_execution_list": _handle_turn_execution_list,
    "turn_execution_get": _handle_turn_execution_get,
    "turn_execution_get_diagnostics": _handle_turn_execution_get_diagnostics,
    "failure_case_intake_collect": _handle_failure_case_intake_collect,
    "failure_case_reference_resolve": _handle_failure_case_reference_resolve,
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
    "jira_get_comments": _handle_jira_get_comments,
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
    "mongo_query_diagnostics_report": _handle_mongo_query_diagnostics_report,
    "workflow_materialisation_diagnostics": _handle_workflow_materialisation_diagnostics,
    "workflow_create_instance": _handle_workflow_create_instance,
    "workflow_execute": _handle_workflow_execute,
    "workflow_list_instances": _handle_workflow_list_instances,
    "workflow_list_execution_traces": _handle_workflow_list_execution_traces,
    "workflow_build_prediction_envelope": _handle_workflow_build_prediction_envelope,
    "workflow_get_instance": _handle_workflow_get_instance,
    "workflow_get_execution_trace": _handle_workflow_get_execution_trace,
    "workflow_cancel_instance": _handle_workflow_cancel_instance,
    "workflow_resume_instance": _handle_workflow_resume_instance,
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
