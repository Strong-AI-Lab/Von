#!/usr/bin/env python3
"""
MCP stdio server wrapper for Vontology operations.
This provides a Model Context Protocol interface over stdin/stdout
while reusing the existing Flask endpoint logic.
"""

import sys
import os
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

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

# Absolute imports (works when run as a script)
from datetime import datetime, timezone
from src.backend.vontology.utils_vontology import (
    create_vontology_concept,
    get_all_vontology_nodes_with_details,
    get_vontology_node_content,
    get_vontology_tree,
    simulate_or_delete_concept,
)
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.services.concept_service import (
    get_concept_display_name_with_names_fallback,
    get_concept_by_concept_id,
    enrich_concept_with_text_relations,
    update_concept,
)
from src.backend.services.concept_relation_service import (
    build_concept_relations_payload,
)
from src.backend.services.concept_search_service import search_concepts
from src.backend.services.concept_embedding_service import (
    get_concepts_needing_indexing,
    get_concept_embedding_stats,
)
from src.backend.services.text_value_service import (
    upsert_text_for_concept,
    get_texts_for_concept,
    get_text_relations_summary,
    upsert_singleton_text_relation,
    update_text_relation_text,
    delete_text_relation,
    delete_text_relation_by_predicate_and_text,
)
from src.backend.services.create_concepts_parent_resolution_service import (
    resolve_parent_for_create_concepts,
)
from src.backend.services.create_concepts_duplicate_guard_service import (
    build_duplicate_prevented_create_concepts_result,
    find_existing_concept_for_create_concepts,
)
from src.backend.services.rag_text_relation_change_hook_service import (
    maybe_delete_text_relation_doc_from_rag,
    maybe_sync_concept_text_relations_to_rag,
)
from src.backend.services.annotation_extraction_service import extract_annotations
from src.backend.services.concept_merge_service import merge_concepts
from src.backend.services.settings_service import (
    resolve_llm_setting,
    get_preferred_language,
    get_setting,
)
from src.backend.integrations.internal_mcp.catalogue import _add_relationship
from src.backend.integrations.internal_mcp.catalogue import _jira_add_comment
from src.backend.integrations.internal_mcp.catalogue import _jira_create_issue
from src.backend.integrations.internal_mcp.catalogue import _jira_get_auth_config
from src.backend.integrations.internal_mcp.catalogue import _jira_get_issue
from src.backend.integrations.internal_mcp.catalogue import _jira_get_myself
from src.backend.integrations.internal_mcp.catalogue import _jira_link_issue
from src.backend.integrations.internal_mcp.catalogue import _jira_search
from src.backend.integrations.internal_mcp.catalogue import _jira_transition_issue
from src.backend.integrations.internal_mcp.catalogue import _jira_update_issue
from src.backend.integrations.internal_mcp.catalogue import _remove_relationship
from src.backend.integrations.internal_mcp.catalogue import _workflow_cancel_instance
from src.backend.integrations.internal_mcp.catalogue import _workflow_create_instance
from src.backend.integrations.internal_mcp.catalogue import _workflow_create_schedule
from src.backend.integrations.internal_mcp.catalogue import _workflow_delete_schedule
from src.backend.integrations.internal_mcp.catalogue import _workflow_get_instance
from src.backend.integrations.internal_mcp.catalogue import _workflow_get_schedule
from src.backend.integrations.internal_mcp.catalogue import _workflow_list_definitions
from src.backend.integrations.internal_mcp.catalogue import _workflow_list_instances
from src.backend.integrations.internal_mcp.catalogue import _workflow_list_schedules
from src.backend.integrations.internal_mcp.catalogue import _workflow_mcp_health_check
from src.backend.integrations.internal_mcp.catalogue import _workflow_retry_instance
from src.backend.integrations.internal_mcp.catalogue import (
    _workflow_set_schedule_enabled,
)
from src.backend.integrations.internal_mcp.catalogue import _workflow_trigger_schedule
from src.backend.integrations.internal_mcp.schemas import make_error_response
from src.backend.integrations.internal_mcp.workflow_surface_capabilities import (
    classify_stdio_missing_tool,
)
from src.backend.integrations.internal_mcp.arxiv_proxy import (
    get_arxiv_proxy,
    ArxivProxyError,
)
from src.backend.integrations.internal_mcp.search_proxy_mcp import (
    get_search_proxy,
    SearchProxyError,
)
from src.backend.integrations.internal_mcp import (
    InternalMCPGateway,
    InternalMCPTransport,
    InternalMCPChatOrchestrator,
    ToolCallParsingError,
    build_default_catalogue,
)
from src.backend.integrations.google import gmail_service
from src.backend.services.rag_service import get_rag_service, RAGBackendUnavailable
from src.backend.languagemodels.llm_interface import (
    get_llm_client,
    get_active_model_name,
)

# Create MCP server instance
app = Server("vontology-mcp")


_LOG = logging.getLogger(__name__)

_TOOL_LIST_CACHE: list[Tool] | None = None
_TOOL_LIST_CACHE_PATH = (
    Path(project_root) / "data" / "mcp_tool_cache" / "vontology_tools_runtime.json"
)


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

        source_mtime_ns = payload.get("source_file_mtime_ns")
        current_mtime_ns = Path(__file__).resolve().stat().st_mtime_ns
        if not isinstance(source_mtime_ns, int) or source_mtime_ns != current_mtime_ns:
            return None

        raw_tools = payload.get("tools")
        if not isinstance(raw_tools, list):
            return None

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
        if tools:
            return tools
    except Exception as exc:  # pragma: no cover - best-effort diagnostics cache
        _LOG.debug("Could not load MCP tool cache: %s", exc)
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

    def __init__(self, *, gateway: InternalMCPGateway, allow_writes: bool) -> None:
        self._gateway = gateway
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

    tools = [
        Tool(
            name="get_context",
            description="Get current server-side context: active LLM model, language preference, and runtime settings. NOTE: User and organisation information is managed client-side (localStorage) per JVNAUTOSCI-628 and is not available through this endpoint.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="create_concepts",
            description="Creates one or more concepts (instances, types, or predicates). Each concept needs a name and kind. Use for bulk creation. Supports singleton arrays. By default, deterministic pre-create lookup blocks duplicate instances/types/predicates; set allow_duplicate_instances=true to opt into legacy instance suffixing. After creation, use add_names for alternative names/translations. Unknown top-level fields are ignored to accommodate orchestrator-added context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "parent_id": {
                        "type": "string",
                        "description": "The concept ID of the parent concept",
                    },
                    "concepts": {
                        "type": "array",
                        "description": "Array of concepts to create",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Primary name (required)",
                                },
                                "kind": {
                                    "type": "string",
                                    "enum": ["instance", "type", "predicate"],
                                    "default": "type",
                                    "description": "'instance' for individuals, 'type' for subtypes, 'predicate' for relationships",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Optional description",
                                },
                                "notes": {
                                    "type": "string",
                                    "description": "Optional notes",
                                },
                            },
                            "required": ["name"],
                        },
                        "minItems": 1,
                    },
                    "allow_duplicate_instances": {
                        "type": "boolean",
                        "default": False,
                        "description": "When true, bypass duplicate guard for instance concepts and allow legacy suffix-based instance IDs",
                    },
                },
                "required": ["parent_id", "concepts"],
            },
        ),
        Tool(
            name="find_subconcepts",
            description="Finds all direct subconcepts (children) of a given concept in the Vontology hierarchy",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to find children of",
                    }
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="find_concepts_by_name",
            description="Searches for concepts in the Vontology by name substring",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name substring to search for",
                    }
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="upsert_text_relation",
            description="Add or update ANY text relation (hasContent, hasDescription, hasNote, custom predicates, etc.). Use for attaching text content to concepts with flexible predicate types.",
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g., #V#user@org). When provided, triggers a best-effort RAG reindex for the concept; otherwise no automatic indexing occurs.",
                    },
                    "concept_id": {
                        "type": "string",
                        "description": "The concept to attach text to (e.g., '#V#my_concept')",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "The text relation predicate (e.g., 'hasContent', 'hasDescription', 'hasNote')",
                    },
                    "text": {
                        "type": "string",
                        "description": "The text content to attach",
                    },
                    "language": {
                        "type": "string",
                        "default": "en-NZ",
                        "description": "Language code (ISO 639-1/BCP 47): en-NZ (default), en-US, fr, de, mi, zh, etc.",
                    },
                    "context": {
                        "type": ["object", "null"],
                        "description": "Optional metadata (e.g., {'text_type': 'NL', 'author': 'system'})",
                    },
                },
                "required": ["concept_id", "predicate", "text"],
            },
        ),
        Tool(
            name="get_text_relations",
            description="Retrieve text relations for a concept, optionally filtered by predicate/language. Returns all text attachments (hasContent, hasDescription, hasName, etc.).",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept to query text relations for",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Optional: filter by predicate (e.g., 'hasContent', 'hasName')",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional: filter by language code (e.g., 'en-NZ', 'fr')",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum number of results to return",
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="update_text_relation",
            description="Modify the text content of an existing text relation by relation ID. Updates the text value while preserving the relation structure.",
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g., #V#user@org). When provided, triggers a best-effort RAG reindex for the concept; otherwise no automatic indexing occurs.",
                    },
                    "concept_id": {
                        "type": "string",
                        "description": "The concept owning the text relation",
                    },
                    "relation_id": {
                        "type": "string",
                        "description": "The ID of the text relation to update",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The new text content",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional: update the language code",
                    },
                },
                "required": ["concept_id", "relation_id", "new_text"],
            },
        ),
        Tool(
            name="delete_text_relation",
            description="Delete a specific text relation by relation ID or by predicate+text match. Optionally garbage-collects orphaned text values.",
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g., #V#user@org). When provided, triggers a best-effort RAG delete/reindex; otherwise no automatic indexing occurs.",
                    },
                    "concept_id": {
                        "type": "string",
                        "description": "The concept owning the text relation",
                    },
                    "relation_id": {
                        "type": "string",
                        "description": "Optional: delete by relation ID (preferred method)",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Optional: delete by predicate + text match",
                    },
                    "text": {
                        "type": "string",
                        "description": "Optional: exact text to match (used with predicate)",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional: language code for predicate+text deletion",
                    },
                    "garbage_collect": {
                        "type": "boolean",
                        "description": "Optional: when true, delete orphaned text_values after relation removal",
                        "default": False,
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="get_text_relations_summary",
            description="Return a lightweight summary of text relations for a concept: counts + relation IDs grouped by predicate/language (no full text bodies).",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept to summarise text relations for",
                    },
                    "predicates": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: filter by predicate allow-list",
                    },
                    "languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: filter by language allow-list (derived from text_values.lang)",
                    },
                    "max_relation_ids_per_group": {
                        "type": "integer",
                        "default": 25,
                        "description": "Cap for relation IDs returned per predicate/language bucket",
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="upsert_singleton_text_relation",
            description="Upsert a text relation and enforce singleton semantics for (concept, predicate, language) by replacing any other relations in the same group.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept to attach text to",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Text predicate (e.g., 'hasDescription')",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to attach",
                    },
                    "language": {
                        "type": "string",
                        "default": "en-NZ",
                        "description": "Language code for the singleton group",
                    },
                    "policy": {
                        "type": "string",
                        "default": "replace_others",
                        "description": "Singleton policy (currently only replace_others)",
                    },
                    "garbage_collect": {
                        "type": "boolean",
                        "default": True,
                        "description": "When true, garbage-collect orphaned text_values for replaced relations",
                    },
                    "provenance": {
                        "type": "object",
                        "description": "Optional provenance metadata",
                    },
                    "context": {
                        "type": "object",
                        "description": "Optional relation context metadata",
                    },
                },
                "required": ["concept_id", "predicate", "text"],
            },
        ),
        Tool(
            name="audit_concept_text_relations",
            description="Audit all text relations attached to a concept. Reports accessibility status for each relation and whether the concept can be safely renamed. Use before rename operations to identify and resolve blocking inaccessible relations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to audit",
                    },
                    "include_text_preview": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include truncated text preview in results",
                    },
                    "max_preview_length": {
                        "type": "integer",
                        "default": 100,
                        "description": "Maximum length for text previews",
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="concept_exists",
            description="Minimal existence/accessibility check for a concept_id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to check",
                    }
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="fetch_concept_content",
            description="Fetch rendered markdown content for a concept (content_html + md_content + raw_doc). Use reconstruct_md=false to avoid masking missing md_content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to fetch",
                    },
                    "reconstruct_md": {
                        "type": "boolean",
                        "default": True,
                        "description": "When false, do not reconstruct md_content if it is missing",
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="add_names",
            description="Adds one or more names (aliases/synonyms) to an existing concept using text relations. Supports multilingual names and different types: NL (Natural Language - default, for standard names/translations), ABBR (Abbreviation - for short forms like 'EU', 'NATO'), CODE (technical identifiers/URIs). Language codes use ISO 639-1/BCP 47 format (e.g., 'en-NZ', 'fr', 'de', 'mi', 'zh'). Default: en-NZ, NL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to add names to (format: #V#concept_name)",
                    },
                    "names": {
                        "type": "array",
                        "description": "Array of names. Each can be: (1) a string like 'EU member' (defaults: en-NZ, NL), or (2) an object {name, language?, name_type?} for control. Examples: ['EU member'], [{name:'État membre', language:'fr'}], [{name:'EU MS', name_type:'ABBR'}]",
                        "items": {
                            "oneOf": [
                                {
                                    "type": "string",
                                    "description": "Simple name (defaults: en-NZ, NL)",
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "name": {
                                            "type": "string",
                                            "description": "The name text",
                                        },
                                        "language": {
                                            "type": "string",
                                            "default": "en-NZ",
                                            "description": "Language code: en-NZ (default), en-US, fr, de, es, it, mi, zh, ja, etc.",
                                        },
                                        "name_type": {
                                            "type": "string",
                                            "default": "NL",
                                            "description": "Type: NL (Natural Language), ABBR (Abbreviation), CODE (technical ID)",
                                            "enum": ["NL", "ABBR", "CODE"],
                                        },
                                    },
                                    "required": ["name"],
                                },
                            ]
                        },
                        "minItems": 1,
                    },
                },
                "required": ["concept_id", "names"],
            },
        ),
        Tool(
            name="get_tree",
            description="Get complete ontology tree structure starting from the root 'thing' concept. Returns nested hierarchy showing all concepts and their relationships.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="fetch_concept",
            description="Fetch full details of ONE specific concept by its ID. Returns complete information including names (from text relations), description, metadata, relationships, salient predicates, and all other concept properties.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to fetch (format: #V#concept_name, e.g., '#V#person')",
                    },
                    "include_relations_arg1": {
                        "type": "boolean",
                        "description": "When true, include structural relations where the concept is the subject (argument 1).",
                    },
                    "include_relations_any_arg": {
                        "type": "boolean",
                        "description": "When true, include structural relations where the concept appears in any argument position (incoming references).",
                    },
                    "include_text_relations_arg1": {
                        "oneOf": [
                            {"type": "boolean"},
                            {
                                "type": "string",
                                "enum": ["snippets"],
                                "description": "Use 'snippets' to request short text snippets in addition to raw text.",
                            },
                        ],
                        "description": "Include text relations (e.g., hasName, hasDescription) where the concept is the subject.",
                    },
                    "predicate_filter": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of predicate identifiers to include.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of relation entries to return (defaults to 200, capped at 500).",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Offset applied before collecting relation entries.",
                    },
                    "include_concept_preview": {
                        "type": "boolean",
                        "description": "Toggle inclusion of light-weight previews for related concepts.",
                        "default": True,
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="search_concepts",
            description="Advanced concept search with multiple filter options. Supports filtering by kind (individual/type/predicate), instance_of, hierarchy paths, and match types.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "instance_of": {
                        "type": "string",
                        "description": "Filter to instances of a specific type (e.g., '#V#researcher'). By default includes instances of subtypes.",
                    },
                    "direct_instances_only": {
                        "type": "boolean",
                        "description": "If true, only return direct instances of instance_of (not instances of subtypes). Default: false.",
                        "default": False,
                    },
                    "filter_kind": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["individual", "type", "predicate"],
                        },
                        "description": "Filter by concept kind",
                    },
                    "include_hierarchy_path": {
                        "type": "boolean",
                        "description": "Include full hierarchy path",
                    },
                    "match_type": {
                        "type": "string",
                        "enum": ["exact", "substring", "similarity", "semantic"],
                        "description": "Type of matching. 'semantic' uses embedding-based vector search for conceptual similarity.",
                    },
                    "namespace": {
                        "type": ["string", "null"],
                        "description": "Optional namespace for future isolation; currently accepted but not required",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_concept_index_status",
            description="Get statistics about the concept embedding index. Shows counts by status (pending, indexed, stale, failed), concepts needing indexing, and index namespace.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "Optional: get status for a specific concept instead of aggregate stats",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="resolve_concept_by_name",
            description="Resolve a Vontology concept deterministically from a user-provided surface form. Read-only (no mutation). Returns resolved/ambiguous/not_found with an audit trail.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The surface form / name to resolve",
                    },
                    "preferred_languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Preferred languages (ordered) used as a tie-breaker",
                    },
                    "allowed_languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict candidate name matching to these languages",
                    },
                    "instance_of": {
                        "type": "string",
                        "description": "Restrict matches to concepts that are instances of this type (includes descendants)",
                    },
                    "match_code_strings": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true, allow resolving '#V#...' and code-style identifiers",
                    },
                    "normalisation_level": {
                        "type": "string",
                        "default": "default",
                        "description": "Normalisation aggressiveness (currently informational)",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum candidates returned when ambiguous",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="vontology_concept_search",
            description="Namespaced alias for concept search used by the MCP orchestrator. Same behaviour as search_concepts (query required; use empty string when combining with instance_of filters).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "instance_of": {
                        "type": "string",
                        "description": "Filter to instances of a specific type. By default includes instances of subtypes.",
                    },
                    "direct_instances_only": {
                        "type": "boolean",
                        "description": "If true, only return direct instances of instance_of (not instances of subtypes). Default: false.",
                        "default": False,
                    },
                    "filter_kind": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["individual", "type", "predicate"],
                        },
                        "description": "Filter by concept kind",
                    },
                    "include_hierarchy_path": {
                        "type": "boolean",
                        "description": "Include full hierarchy path",
                    },
                    "match_type": {
                        "type": "string",
                        "enum": ["exact", "substring", "similarity", "semantic"],
                        "description": "Type of matching. 'semantic' uses embedding-based vector search for conceptual similarity.",
                    },
                    "namespace": {
                        "type": ["string", "null"],
                        "description": "Optional namespace for future isolation; currently accepted but not required",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="extract_annotations",
            description="Extract structured annotations from text using LLM analysis. Identifies concept mentions and suggests ontology links. Returns array of annotation objects with concept IDs, matched text, and confidence scores.",
            inputSchema={
                "type": "object",
                "properties": {
                    "input_text": {
                        "type": "string",
                        "description": "The text content to analyze",
                    },
                    "context_concept_id": {
                        "type": "string",
                        "description": "Optional context concept ID",
                    },
                },
                "required": ["input_text"],
            },
        ),
        Tool(
            name="search_arxiv",
            description="Search arXiv.org for scholarly articles. Returns list of papers with id, title, authors, summary, publication date. Supports boolean operators in query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (supports AND, OR, NOT operators)",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum results to return",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["relevance", "lastUpdatedDate", "submittedDate"],
                        "default": "relevance",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["ascending", "descending"],
                        "default": "descending",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_paper_metadata",
            description="Get detailed metadata for a specific arXiv paper by its ID. Returns title, authors, abstract, publication dates, categories, DOI, PDF URL, comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "arXiv identifier (e.g., '2506.16596' or 'arXiv:2506.16596')",
                    }
                },
                "required": ["arxiv_id"],
            },
        ),
        Tool(
            name="download_paper",
            description="Download PDF of an arXiv paper and store it in the configured blob store (local or OpenStack Swift). Returns both a local cache file_path and a durable storage URI.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {"type": "string", "description": "arXiv identifier"},
                    "filename": {
                        "type": "string",
                        "description": "Optional custom filename (defaults to arxiv_id.pdf)",
                    },
                },
                "required": ["arxiv_id"],
            },
        ),
        Tool(
            name="finalise_cached_paper",
            description="Upload an already-cached arXiv PDF to the configured blob store and register a Computer File Copy record. Requires authentication (user-scoped write).",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {"type": "string", "description": "arXiv identifier"},
                    "filename": {
                        "type": "string",
                        "description": "Optional display name to record as original filename",
                    },
                },
                "required": ["arxiv_id"],
            },
        ),
        Tool(
            name="search_web",
            description="Search the web for current information using Tavily MCP proxy. Supports advanced search depth, domain filters, direct answers, raw content, and images.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results",
                    },
                    "search_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "default": "basic",
                        "description": "Search depth",
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional whitelist of domains",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional blacklist of domains",
                    },
                    "include_answer": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include AI-generated direct answer",
                    },
                    "include_raw_content": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include raw page content",
                    },
                    "include_images": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include image URLs",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="context_search",
            description="Context-aware web search that uses provided background information to refine Tavily results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "context": {
                        "type": "string",
                        "description": "Context to focus the search",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 10,
                        "description": "Maximum number of results",
                    },
                    "search_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "default": "basic",
                        "description": "Search depth",
                    },
                    "include_answer": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include AI-generated direct answer",
                    },
                },
                "required": ["query", "context"],
            },
        ),
        Tool(
            name="qna_search",
            description="Question-answering optimised search that returns concise answers with supporting sources.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Question to answer"},
                    "max_results": {
                        "type": "integer",
                        "default": 5,
                        "description": "Maximum number of results",
                    },
                    "search_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "default": "advanced",
                        "description": "Search depth",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="extract_url",
            description="Extract the main content and title from a specific URL using Tavily.",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to extract content from",
                    }
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="gmail_list_messages",
            description="List Gmail messages for a profile with optional label and query filters (read-only).",
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Profile ID configured via env",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional Gmail search query",
                    },
                    "label_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional label IDs to filter",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return",
                    },
                },
                "required": ["profile"],
            },
        ),
        Tool(
            name="gmail_get_message",
            description="Fetch a Gmail message by ID for a profile (formats: metadata|full|raw).",
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Profile ID configured via env",
                    },
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "format": {
                        "type": "string",
                        "enum": ["metadata", "full", "raw", "minimal"],
                        "default": "metadata",
                        "description": "Gmail API format",
                    },
                },
                "required": ["profile", "message_id"],
            },
        ),
        Tool(
            name="gmail_get_attachment",
            description="Fetch a Gmail attachment by message and attachment ID for a profile (base64 payload).",
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Profile ID configured via env",
                    },
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "attachment_id": {
                        "type": "string",
                        "description": "Gmail attachment ID",
                    },
                },
                "required": ["profile", "message_id", "attachment_id"],
            },
        ),
        Tool(
            name="gmail_list_labels",
            description="List Gmail labels for a profile (read-only).",
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Profile ID configured via env",
                    }
                },
                "required": ["profile"],
            },
        ),
        Tool(
            name="gmail_modify_labels",
            description="Add/remove labels on a Gmail message for a profile. Requires allow_mutation=true and gmail.modify scope.",
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Profile ID configured via env",
                    },
                    "message_id": {"type": "string", "description": "Gmail message ID"},
                    "add_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to add",
                    },
                    "remove_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to remove",
                    },
                    "allow_mutation": {
                        "type": "boolean",
                        "description": "Must be true to permit mutation",
                    },
                },
                "required": ["profile", "message_id", "allow_mutation"],
            },
        ),
        Tool(
            name="add_relationship",
            description="Add a relationship between two concepts or from a concept to a text value. Use to add instance_of/typeOf relationships (e.g., add '#V#professor' as instance_of for a person), custom predicates (e.g., '#V#hasAffiliation' → 'Auckland University'), or any binary relationship. Supports both concept-to-concept relations (target is concept ID) and text predicates (target is text value). Common predicates: 'instance_of'/'instanceOf' (maps to is_an_instance_of), 'typeOf' (maps to is_a_type_of), or custom predicates like '#V#hasAffiliation', '#V#founderOf', '#V#hasResearchInterest'.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": "Source concept ID (e.g., '#V#nikola_k._kasabov')",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Relationship type: 'instance_of', 'typeOf', or custom predicate like '#V#hasAffiliation'",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target concept ID (e.g., '#V#professor') or text value for text predicates",
                    },
                },
                "required": ["source_id", "predicate", "target"],
            },
        ),
        Tool(
            name="remove_relationship",
            description="Remove a relationship between two concepts (concept-to-concept only). Use to clean incorrect type/instance links or other structural predicates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": "Source concept ID (e.g., '#V#mjw_work_diary_2025-11-24')",
                    },
                    "predicate": {
                        "type": "string",
                        "description": "Relationship alias or stored field name (e.g., 'instance_of', 'typeOf')",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target concept ID (e.g., '#V#diary_entry_about_michael_witbrocks_work')",
                    },
                },
                "required": ["source_id", "predicate", "target"],
            },
        ),
        Tool(
            name="delete_concept",
            description="Deletes a concept and handles its relationships. Can simulate the deletion first to see impact.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to delete",
                    },
                    "simulate": {
                        "type": "boolean",
                        "description": "If true (default), only simulates the deletion and returns a report. If false, executes the deletion.",
                        "default": True,
                    },
                },
                "required": ["concept_id"],
            },
        ),
        Tool(
            name="merge_concepts",
            description="Merges a source concept into a target concept. Moves relationships, names, and text values, then deletes the source. Can simulate first.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": "The concept ID to merge FROM (will be deleted)",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "The concept ID to merge TO (will receive data)",
                    },
                    "simulate": {
                        "type": "boolean",
                        "description": "If true (default), only simulates the merge and returns a report. If false, executes the merge.",
                        "default": True,
                    },
                },
                "required": ["source_id", "target_id"],
            },
        ),
        Tool(
            name="update_concept",
            description="Update specific fields of a concept. Use when you need to modify properties or relationships directly (e.g. fixing ontology errors, changing 'kind' by updating relationships). Supports dot notation in update_data keys for partial updates of nested objects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to update",
                    },
                    "update_data": {
                        "type": "object",
                        "description": "Dictionary of fields to update. Use dot notation for nested fields (e.g. {'relationships.is_an_instance_of': [...]}).",
                    },
                },
                "required": ["concept_id", "update_data"],
            },
        ),
        Tool(
            name="search_knowledge_base",
            description="Search the internal knowledge base (RAG) for documents and indexed content. Use when user asks about internal documents, policies, or specific indexed knowledge that is not in the ontology or on the public web. Returns semantically relevant text chunks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "top_k": {
                        "type": "integer",
                        "default": 5,
                        "description": "Number of results to return",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace filter",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="jira_search",
            description="Run a JQL query against Jira. Use when you need to find issues by status, assignee, project, or other fields.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jql": {
                        "type": "string",
                        "description": "JQL query string (required)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Optional Jira page size",
                    },
                    "next_page_token": {
                        "type": "string",
                        "description": "Optional Jira cursor token",
                    },
                    "start_at": {
                        "type": "integer",
                        "description": "Deprecated offset parameter",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of field names to return",
                    },
                },
                "required": ["jql"],
            },
        ),
        Tool(
            name="jira_get_issue",
            description="Fetch full details for a Jira issue by key (for example JVNAUTOSCI-123).",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Issue key (required)",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of field names to return",
                    },
                },
                "required": ["issue_key"],
            },
        ),
        Tool(
            name="jira_add_comment",
            description="Add a comment to a Jira issue.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key"},
                    "comment": {"type": "string", "description": "Comment body text"},
                },
                "required": ["issue_key", "comment"],
            },
        ),
        Tool(
            name="jira_transition",
            description="Transition a Jira issue using a transition ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key"},
                    "transition_id": {
                        "type": "string",
                        "description": "Transition ID",
                    },
                },
                "required": ["issue_key", "transition_id"],
            },
        ),
        Tool(
            name="jira_create_issue",
            description="Create a Jira issue with write guardrails. Default dry_run=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Project key"},
                    "issue_type": {"type": "string", "description": "Issue type name"},
                    "summary": {"type": "string", "description": "Issue summary"},
                    "description": {
                        "type": "string",
                        "description": "Optional issue description",
                    },
                    "parent": {
                        "type": "string",
                        "description": "Optional parent issue key",
                    },
                    "assignee_account_id": {
                        "type": "string",
                        "description": "Optional Jira accountId to assign",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional labels",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "When true, preview only",
                    },
                    "approved": {
                        "type": "boolean",
                        "default": False,
                        "description": "Per-write approval flag for execution",
                    },
                    "execute": {
                        "type": "boolean",
                        "default": False,
                        "description": "Execution override when execute mode is enabled",
                    },
                    "request_id": {
                        "type": "string",
                        "description": "Optional idempotency key",
                    },
                },
                "required": ["project_key", "issue_type", "summary"],
            },
        ),
        Tool(
            name="jira_update_issue",
            description="Update a Jira issue with write guardrails. Default dry_run=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Issue key"},
                    "update_fields": {
                        "type": "object",
                        "description": "Jira fields dictionary to update",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "When true, preview only",
                    },
                    "approved": {
                        "type": "boolean",
                        "default": False,
                        "description": "Per-write approval flag for execution",
                    },
                    "execute": {
                        "type": "boolean",
                        "default": False,
                        "description": "Execution override when execute mode is enabled",
                    },
                    "request_id": {
                        "type": "string",
                        "description": "Optional idempotency key",
                    },
                },
                "required": ["issue_key", "update_fields"],
            },
        ),
        Tool(
            name="jira_link_issue",
            description="Link two Jira issues with write guardrails. Default dry_run=true.",
            inputSchema={
                "type": "object",
                "properties": {
                    "inward_issue_key": {
                        "type": "string",
                        "description": "Inward issue key",
                    },
                    "outward_issue_key": {
                        "type": "string",
                        "description": "Outward issue key",
                    },
                    "link_type": {"type": "string", "description": "Jira link type name"},
                    "comment": {
                        "type": "string",
                        "description": "Optional link comment",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "When true, preview only",
                    },
                    "approved": {
                        "type": "boolean",
                        "default": False,
                        "description": "Per-write approval flag for execution",
                    },
                    "execute": {
                        "type": "boolean",
                        "default": False,
                        "description": "Execution override when execute mode is enabled",
                    },
                    "request_id": {
                        "type": "string",
                        "description": "Optional idempotency key",
                    },
                },
                "required": ["inward_issue_key", "outward_issue_key", "link_type"],
            },
        ),
        Tool(
            name="jira_get_myself",
            description="Return the Jira user profile for the currently configured Atlassian credentials.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="jira_get_auth_config",
            description="Inspect Jira auth configuration (base URL/email/token presence) from the process environment.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="workflow_list_definitions",
            description="List available workflow definitions (IDs, descriptions) that can be instantiated.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Optional cap on number of definitions returned",
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="workflow_mcp_health_check",
            description="Run lightweight workflow/introspection MCP health checks through InternalMCPGateway.invoke() and return actionable diagnostics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace used for check payloads",
                    },
                    "include_introspection": {
                        "type": "boolean",
                        "description": "When true, include chat introspection checks",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="workflow_create_instance",
            description="Create a new durable workflow instance. The workflow will be queued for execution by a background worker. Use workflow_get_instance to check status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow definition concept ID",
                    },
                    "user_id": {"type": "string", "description": "Optional user concept ID"},
                    "org_id": {"type": "string", "description": "Optional organisation concept ID"},
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace for routing/scoping",
                    },
                    "inputs": {
                        "type": "object",
                        "description": "Optional workflow input payload",
                    },
                    "max_retries": {
                        "type": "integer",
                        "description": "Optional retry override for this instance",
                    },
                },
                "required": ["workflow_id"],
            },
        ),
        Tool(
            name="workflow_list_instances",
            description="List durable workflow instances. Filter by user, org, namespace, status, or workflow_id. Supports source_event_type/source_event_id filters for event-to-workflow traceability.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "org_id": {"type": "string"},
                    "namespace": {"type": "string"},
                    "status": {
                        "type": "string",
                        "description": "pending|running|completed|failed|cancelled|paused",
                    },
                    "workflow_id": {"type": "string"},
                    "source_event_type": {"type": "string"},
                    "source_event_id": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        ),
        Tool(
            name="workflow_get_instance",
            description="Get detailed status of a durable workflow instance including current state, inputs, outputs, and any errors.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Workflow instance ID"}
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="workflow_cancel_instance",
            description="Cancel a running or pending workflow instance. The worker will stop execution at the next checkpoint.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Workflow instance ID"}
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="workflow_retry_instance",
            description="Reset a failed workflow instance for retry if it has not exceeded its max_retries limit.",
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_id": {"type": "string", "description": "Workflow instance ID"}
                },
                "required": ["instance_id"],
            },
        ),
        Tool(
            name="workflow_create_schedule",
            description="Create a scheduled trigger for a workflow. Supports schedule types: interval, cron, or once.",
            inputSchema={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "string",
                        "description": "Workflow definition concept ID",
                    },
                    "schedule_type": {
                        "type": "string",
                        "description": "interval|cron|once",
                    },
                    "user_id": {"type": "string"},
                    "org_id": {"type": "string"},
                    "namespace": {"type": "string"},
                    "interval_seconds": {"type": "integer"},
                    "cron_expression": {"type": "string"},
                    "run_at": {
                        "type": "string",
                        "description": "ISO datetime used for once schedules",
                    },
                    "default_inputs": {
                        "type": "object",
                        "description": "Optional default inputs for triggered instances",
                    },
                    "description": {"type": "string"},
                },
                "required": ["workflow_id", "schedule_type"],
            },
        ),
        Tool(
            name="workflow_list_schedules",
            description="List workflow schedules. Filter by user or enabled status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "enabled_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
        ),
        Tool(
            name="workflow_get_schedule",
            description="Get detailed information about a workflow schedule.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "Schedule ID"}
                },
                "required": ["schedule_id"],
            },
        ),
        Tool(
            name="workflow_set_schedule_enabled",
            description="Enable or disable a workflow schedule.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "Schedule ID"},
                    "enabled": {"type": "boolean", "description": "Desired enabled state"},
                },
                "required": ["schedule_id", "enabled"],
            },
        ),
        Tool(
            name="workflow_delete_schedule",
            description="Delete a workflow schedule permanently.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "Schedule ID"}
                },
                "required": ["schedule_id"],
            },
        ),
        Tool(
            name="workflow_trigger_schedule",
            description="Manually trigger a workflow schedule immediately, creating a new workflow instance.",
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string", "description": "Schedule ID"}
                },
                "required": ["schedule_id"],
            },
        ),
        Tool(
            name="von_chat_run",
            description=(
                "Run the Von chat orchestrator (LLM + internal MCP tools) and return a redacted trace. "
                "By default, this runs in dry-run mode (read-only tools only). "
                "To allow write tools, set VON_INTERNAL_MCP_ENABLE=1 and VON_MCP_ALLOW_WRITES=1 and pass allow_writes=true."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "User prompt text"},
                    "context": {
                        "type": "array",
                        "description": "Optional prior messages [{role, content}]",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {
                                    "type": "string",
                                    "description": "system|user|assistant|tool",
                                },
                                "content": {"type": "string"},
                            },
                            "required": ["role", "content"],
                        },
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override",
                    },
                    "user_namespace": {
                        "type": "string",
                        "description": "Optional namespace (e.g., #V#michael_witbrock) injected into tool payloads",
                    },
                    "gmail_profile": {
                        "type": "string",
                        "description": "Optional Gmail profile name",
                    },
                    "auxiliary_system_prompt": {
                        "type": "string",
                        "description": "Optional user-specific system prompt",
                    },
                    "max_tool_invocations": {
                        "type": "integer",
                        "default": 8,
                        "description": "Maximum number of tool calls in one run",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true, block write-category tools",
                    },
                    "allow_writes": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, allow write-category tools (requires VON_MCP_ALLOW_WRITES=1)",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "default": 90,
                        "description": "Overall wall-clock timeout for the orchestrator run. If exceeded, returns an error rather than hanging.",
                    },
                    "max_string_chars": {
                        "type": "integer",
                        "default": 8000,
                        "description": "Max characters retained for any string in the trace",
                    },
                    "max_context_chars": {
                        "type": "integer",
                        "default": 120000,
                        "description": "Maximum total characters of chat context forwarded to the LLM (approximate char budget; system prompt is always retained).",
                    },
                    "max_tool_result_chars": {
                        "type": "integer",
                        "default": 20000,
                        "description": "Maximum characters allowed for a single tool-result message added back into LLM context.",
                    },
                    "max_tool_result_field_chars": {
                        "type": "integer",
                        "default": 8000,
                        "description": "Maximum characters for any single string field inside a tool result forwarded to the LLM.",
                    },
                },
                "required": ["prompt"],
            },
        ),
        # Task management tools (JVNAUTOSCI-1040)
        Tool(
            name="create_task",
            description="Create a task from a conversation. Tasks are stored as Vontology concepts with rich metadata. Optionally links to the originating conversation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Brief task title (required)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Detailed task description",
                    },
                    "assignee_concept_id": {
                        "type": "string",
                        "description": "Assignee's concept ID (e.g., '#V#user_abc')",
                    },
                    "creator_concept_id": {
                        "type": "string",
                        "description": "Creator's concept ID (e.g., '#V#user_abc')",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Chat session_id to link task to conversation",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "ISO 8601 date string for due date",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "default": "medium",
                        "description": "Task priority level",
                    },
                    "organisation_concept_id": {
                        "type": "string",
                        "description": "Organisation context for the task",
                    },
                },
                "required": ["title", "description"],
            },
        ),
        Tool(
            name="get_task",
            description="Get details of a specific task by its concept ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_concept_id": {
                        "type": "string",
                        "description": "The task's concept ID (e.g., '#V#task_abc123')",
                    },
                },
                "required": ["task_concept_id"],
            },
        ),
        Tool(
            name="list_my_tasks",
            description="List tasks assigned to a user, optionally filtered by status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_concept_id": {
                        "type": "string",
                        "description": "User's concept ID to get tasks for",
                    },
                    "status_filter": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "in_progress",
                            "completed",
                            "cancelled",
                            "blocked",
                        ],
                        "description": "Optional status filter",
                    },
                    "include_created": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, also include tasks created by the user (not just assigned)",
                    },
                },
                "required": ["user_concept_id"],
            },
        ),
        Tool(
            name="update_task_status",
            description="Update the status of a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_concept_id": {
                        "type": "string",
                        "description": "The task's concept ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "in_progress",
                            "completed",
                            "cancelled",
                            "blocked",
                        ],
                        "description": "New task status",
                    },
                },
                "required": ["task_concept_id", "status"],
            },
        ),
        Tool(
            name="assign_task",
            description="Assign or reassign a task to a user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_concept_id": {
                        "type": "string",
                        "description": "The task's concept ID",
                    },
                    "assignee_concept_id": {
                        "type": "string",
                        "description": "The assignee's concept ID",
                    },
                },
                "required": ["task_concept_id", "assignee_concept_id"],
            },
        ),
    ]

    _TOOL_LIST_CACHE = list(tools)
    _persist_tool_list_cache(_TOOL_LIST_CACHE)
    return list(_TOOL_LIST_CACHE)


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:  # type: ignore[misc]
    """Handle tool calls by delegating to specific handlers."""

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

    try:
        return await handler(arguments or {})
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
    from src.backend.vontology.code_concepts_registry import PREDICATE_TYPE_ID

    parent_id = arguments.get("parent_id")
    concepts = arguments.get("concepts", [])
    allow_duplicate_instances_raw = arguments.get("allow_duplicate_instances", False)
    allow_duplicate_instances = (
        allow_duplicate_instances_raw
        if isinstance(allow_duplicate_instances_raw, bool)
        else str(allow_duplicate_instances_raw).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if not parent_id or not concepts:
        return [
            _json_error(
                "Missing required parameters: parent_id and concepts array",
                error_code="missing_parameter",
                details={
                    "missing": [
                        p for p in ["parent_id", "concepts"] if not arguments.get(p)
                    ]
                },
                suggestions=[
                    "Specify parent_id (e.g., '#V#person' or '#V#abstract_object')",
                    "Provide concepts array with at least one {name: '...'} object",
                    "Use search_concepts to find existing parent concepts",
                ],
            )
        ]

    parent_resolution = resolve_parent_for_create_concepts(str(parent_id))
    if not parent_resolution.success:
        canonical_parent = parent_resolution.canonical_parent_id
        related_ids = [
            cid
            for cid in [
                canonical_parent,
                *parent_resolution.fallback_candidates_checked,
            ]
            if cid
        ]
        suggestions = [
            "Create the parent concept first",
            "Search for similar concepts using search_concepts",
        ]
        if parent_resolution.fallback_candidates_checked:
            suggestions.append(
                "For workflow concepts, prefer an existing workflow supertype "
                f"({', '.join(parent_resolution.fallback_candidates_checked)})"
            )
        return [
            _json_error(
                f"Parent concept '{canonical_parent}' not found. Create it first or check the ID.",
                error_code="parent_not_found",
                details={
                    "canonical_parent_id": canonical_parent,
                    "original_parent_id": parent_id,
                    "fallback_candidates_checked": list(
                        parent_resolution.fallback_candidates_checked
                    ),
                },
                suggestions=suggestions,
                related_concept_ids=related_ids,
            )
        ]

    assert parent_resolution.resolved_parent_id is not None
    resolved_parent_id = parent_resolution.resolved_parent_id

    results = []
    for concept_data in concepts:
        name_val = concept_data.get("name")
        kind_raw = concept_data.get("kind", "type")
        kind = str(kind_raw or "type").strip().lower()
        if kind == "individual":
            kind = "instance"
        description = concept_data.get("description")
        notes = concept_data.get("notes")
        instance_of_type = concept_data.get("instance_of_type")

        if not name_val:
            results.append(
                {"error": "Concept missing required 'name' field", "data": concept_data}
            )
            continue

        if kind == "predicate":
            create_as_instance = True
            parent_id_for_concept = PREDICATE_TYPE_ID
        else:
            create_as_instance = kind == "instance"
            parent_id_for_concept = resolved_parent_id

        duplicate_match = find_existing_concept_for_create_concepts(
            concept_name=str(name_val),
            kind=kind,
            parent_id_for_concept=parent_id_for_concept,
            preferred_language="en-NZ",
            allow_duplicate_instances=allow_duplicate_instances,
        )
        if duplicate_match is not None:
            result = build_duplicate_prevented_create_concepts_result(
                requested_name=str(name_val),
                requested_kind=kind,
                existing_concept_id=duplicate_match.existing_concept_id,
                guard_scope=duplicate_match.guard_scope,
                match_source=duplicate_match.match_source,
            )
            result["concept_id"] = duplicate_match.existing_concept_id
            results.append(result)
            continue

        result = create_vontology_concept(
            parent_id=parent_id_for_concept,
            new_concept_name=name_val,
            create_as_instance=create_as_instance,
            description=description,
            notes=notes,
            instance_of_type=instance_of_type,
        )
        if isinstance(result, dict):
            result["requested_name"] = str(name_val)
            result["requested_kind"] = str(kind)
            concept_id_value = result.get("concept_id")
            if not isinstance(concept_id_value, str) or not concept_id_value.strip():
                nested_concept = result.get("concept")
                if isinstance(nested_concept, dict):
                    nested_id = nested_concept.get("concept_id")
                    if isinstance(nested_id, str) and nested_id.strip():
                        concept_id_value = nested_id
            if not isinstance(concept_id_value, str) or not concept_id_value.strip():
                canonical_id = result.get("canonical_concept_id")
                if isinstance(canonical_id, str) and canonical_id.strip():
                    concept_id_value = canonical_id
            if not isinstance(concept_id_value, str) or not concept_id_value.strip():
                existing_id = result.get("existing_concept_id")
                if isinstance(existing_id, str) and existing_id.strip():
                    concept_id_value = existing_id
            if isinstance(concept_id_value, str) and concept_id_value.strip():
                result["concept_id"] = concept_id_value.strip()
        results.append(result)

    created_concept_ids = [
        str(r.get("concept_id")).strip()
        for r in results
        if isinstance(r, dict)
        and r.get("success")
        and isinstance(r.get("concept_id"), str)
        and str(r.get("concept_id")).strip()
    ]
    successful = sum(
        1 for r in results if isinstance(r, dict) and bool(r.get("success"))
    )
    already_exists = sum(
        1
        for r in results
        if isinstance(r, dict) and r.get("error_code") == "already_exists"
    )
    payload = {
        "results": results,
        "total": len(concepts),
        "successful": successful,
        "already_existed": already_exists,
        "failed": len(concepts) - successful - already_exists,
        "created_concept_ids": created_concept_ids,
        "parent_id_used": resolved_parent_id,
        "parent_resolution": parent_resolution.to_dict(),
    }
    return [_json_text(payload)]


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
        max_tool_invocations = int(arguments.get("max_tool_invocations", 8))
    except Exception:
        max_tool_invocations = 8
    max_tool_invocations = max(0, min(16, max_tool_invocations))

    dry_run = bool(arguments.get("dry_run", True))
    allow_writes = bool(arguments.get("allow_writes", False))
    if allow_writes and not _truthy_env("VON_MCP_ALLOW_WRITES"):
        return [
            _json_text(
                {
                    "success": False,
                    "error": "Write tools are not enabled. Set VON_MCP_ALLOW_WRITES=1 (and ensure VON_INTERNAL_MCP_ENABLE=1) to use allow_writes=true.",
                }
            )
        ]
    effective_allow_writes = allow_writes and not dry_run

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
            "timeout_seconds": timeout_seconds,
            "error": f"Tool call parsing error: {exc}",
        }
    except Exception as exc:
        payload = {
            "success": False,
            "model": model_name,
            "dry_run": dry_run,
            "allow_writes": effective_allow_writes,
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
        result = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang=language,
            context=context,
        )

        maybe_sync_concept_text_relations_to_rag(
            namespace=namespace,
            concept_id=concept_id,
            predicate=predicate,
        )

        text_preview = text[:100] + "..." if len(text) > 100 else text
        payload = {
            "success": True,
            "text_value_id": str(result.get("text_value_id")),
            "relation_id": str(result.get("relation_id")),
            "relation_created": result.get("relation_created"),
            "predicate": predicate,
            "text_preview": text_preview,
            "language": language,
        }
        return [_json_text(payload)]
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

        # Detect vacuous typing (soft warning for agents to repair)
        from ..services.relationship_write_service import detect_vacuous_typing

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
        payload = upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=predicate,
            text=text,
            lang=arguments.get("language", "en-NZ"),
            policy=arguments.get("policy", "replace_others"),
            provenance=arguments.get("provenance"),
            context=arguments.get("context"),
            garbage_collect=arguments.get("garbage_collect", True),
        )
        return [_json_text(payload)]
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
        proxy = get_arxiv_proxy()
        result = proxy.download_paper(
            arxiv_id=arxiv_id, filename=arguments.get("filename")
        )
        return [_json_text(result)]
    except ArxivProxyError as exc:
        return [_json_text({"error": str(exc), "success": False})]
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
        result = _remove_relationship(
            source_id=source_id, predicate=predicate, target=target
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


async def _handle_jira_add_comment(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_add_comment,
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


async def _handle_jira_link_issue(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _jira_link_issue,
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


async def _handle_workflow_list_definitions(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_list_definitions,
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


async def _handle_workflow_create_instance(
    arguments: dict[str, Any],
) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_create_instance,
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


async def _handle_workflow_get_instance(arguments: dict[str, Any]) -> list[TextContent]:
    return _run_catalogue_proxy_handler(
        _workflow_get_instance,
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
            due_date=arguments.get("due_date"),
            priority=arguments.get("priority", "medium"),
            organisation_concept_id=arguments.get("organisation_concept_id"),
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
    "finalise_cached_paper": _handle_finalise_cached_paper,
    "search_web": _handle_search_web,
    "context_search": _handle_context_search,
    "qna_search": _handle_qna_search,
    "extract_url": _handle_extract_url,
    "gmail_list_messages": _handle_gmail_list_messages,
    "gmail_get_message": _handle_gmail_get_message,
    "gmail_get_attachment": _handle_gmail_get_attachment,
    "gmail_list_labels": _handle_gmail_list_labels,
    "gmail_modify_labels": _handle_gmail_modify_labels,
    "add_relationship": _handle_add_relationship,
    "remove_relationship": _handle_remove_relationship,
    "delete_concept": _handle_delete_concept,
    "merge_concepts": _handle_merge_concepts,
    "update_concept": _handle_update_concept,
    "search_knowledge_base": _handle_search_knowledge_base,
    "jira_search": _handle_jira_search,
    "jira_get_issue": _handle_jira_get_issue,
    "jira_add_comment": _handle_jira_add_comment,
    "jira_transition": _handle_jira_transition,
    "jira_create_issue": _handle_jira_create_issue,
    "jira_update_issue": _handle_jira_update_issue,
    "jira_link_issue": _handle_jira_link_issue,
    "jira_get_myself": _handle_jira_get_myself,
    "jira_get_auth_config": _handle_jira_get_auth_config,
    "workflow_list_definitions": _handle_workflow_list_definitions,
    "workflow_mcp_health_check": _handle_workflow_mcp_health_check,
    "workflow_create_instance": _handle_workflow_create_instance,
    "workflow_list_instances": _handle_workflow_list_instances,
    "workflow_get_instance": _handle_workflow_get_instance,
    "workflow_cancel_instance": _handle_workflow_cancel_instance,
    "workflow_retry_instance": _handle_workflow_retry_instance,
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
