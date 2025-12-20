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
from src.backend.services.text_value_service import upsert_text_for_concept
from src.backend.services.annotation_extraction_service import extract_annotations
from src.backend.services.concept_merge_service import merge_concepts
from src.backend.services.settings_service import (
    get_active_llm_setting,
    get_preferred_language,
    get_setting,
)
from src.backend.integrations.internal_mcp.catalogue import _add_relationship
from src.backend.integrations.internal_mcp.catalogue import _remove_relationship
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
    return [
        Tool(
            name="get_context",
            description="Get current server-side context: active LLM model, language preference, and runtime settings. NOTE: User and organisation information is managed client-side (localStorage) per JVNAUTOSCI-628 and is not available through this endpoint.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="create_concepts",
            description="Creates one or more concepts (instances, types, or predicates). Each concept needs a name and kind. Use for bulk creation. Supports singleton arrays. After creation, use add_names for alternative names/translations. Unknown top-level fields are ignored to accommodate orchestrator-added context.",
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
                        "description": "Filter to instances of a specific type (e.g., '#V#researcher')",
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
                        "enum": ["exact", "substring", "similarity"],
                        "description": "Type of matching",
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
            name="vontology_concept_search",
            description="Namespaced alias for concept search used by the MCP orchestrator. Same behaviour as search_concepts (query required; use empty string when combining with instance_of filters).",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query text"},
                    "instance_of": {
                        "type": "string",
                        "description": "Filter to instances of a specific type",
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
                        "enum": ["exact", "substring", "similarity"],
                        "description": "Type of matching",
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
            description="Download PDF of an arXiv paper to local storage (data/arxiv_papers/). Returns file path where PDF was saved.",
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
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:  # type: ignore[misc]
    """Handle tool calls by delegating to specific handlers."""

    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return [_json_error(f"Unknown tool: {name}")]

    try:
        return await handler(arguments or {})
    except Exception as exc:  # Defensive: avoid crashing the stdio server
        return [_json_error(str(exc))]


def _json_text(payload: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, indent=2, default=str))


def _json_error(message: str) -> TextContent:
    return _json_text({"error": message})


async def _handle_get_context(arguments: dict[str, Any]) -> list[TextContent]:
    model_setting = get_active_llm_setting()
    context = {
        "llm_model": (
            model_setting.get("model")
            if isinstance(model_setting, dict)
            else model_setting
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
    parent_id = arguments.get("parent_id")
    concepts = arguments.get("concepts", [])
    if not parent_id or not concepts:
        return [
            _json_error("Missing required parameters: parent_id and concepts array")
        ]

    results = []
    for concept_data in concepts:
        name_val = concept_data.get("name")
        kind = concept_data.get("kind", "type")
        description = concept_data.get("description")
        notes = concept_data.get("notes")

        if not name_val:
            results.append(
                {"error": "Concept missing required 'name' field", "data": concept_data}
            )
            continue

        create_as_instance = kind == "instance"
        result = create_vontology_concept(
            parent_id=parent_id,
            new_concept_name=name_val,
            create_as_instance=create_as_instance,
            description=description,
            notes=notes,
        )
        results.append(result)

    payload = {
        "results": results,
        "total": len(concepts),
        "successful": sum(1 for r in results if r.get("success")),
    }
    return [_json_text(payload)]


async def _handle_find_subconcepts(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    if not concept_id:
        return [_json_error("Missing concept_id parameter")]

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
        return [_json_error("Missing name parameter")]

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
        gateway=gateway,
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


async def _handle_add_names(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    names = arguments.get("names")
    if not concept_id:
        return [_json_error("Missing concept_id parameter")]
    if not names or not isinstance(names, list):
        return [_json_error("Missing or invalid names array")]

    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not concept:
        return [_json_error(f"Concept '{concept_id}' not found")]

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
        return [_json_error("Missing concept_id parameter")]

    try:
        concept = get_concept_by_concept_id(concept_id)
        if not concept:
            return [_json_error(f"Concept '{concept_id}' not found")]

        concept = enrich_concept_with_text_relations(concept)
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
        return [_json_error(str(exc))]


async def _handle_search_concepts(arguments: dict[str, Any]) -> list[TextContent]:
    search_result = search_concepts(**arguments)
    return [_json_text(search_result)]


async def _handle_extract_annotations(arguments: dict[str, Any]) -> list[TextContent]:
    input_text = arguments.get("input_text")
    context_concept_id = arguments.get("context_concept_id")
    if not input_text:
        return [_json_error("Missing input_text parameter")]

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
        return [_json_error("Missing required parameter: arxiv_id")]

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
        return [_json_error("Missing required parameter: arxiv_id")]

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


async def _handle_search_web(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    if not query:
        return [_json_error("Missing query parameter")]
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
        return [_json_error(str(exc))]
    except Exception as exc:
        return [_json_error(f"Unexpected error: {exc}")]


async def _handle_context_search(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    context_value = arguments.get("context")
    if not query or not context_value:
        return [_json_error("Missing required parameters: query and context")]
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
        return [_json_error(str(exc))]
    except Exception as exc:
        return [_json_error(f"Unexpected error: {exc}")]


async def _handle_qna_search(arguments: dict[str, Any]) -> list[TextContent]:
    query = arguments.get("query")
    if not query:
        return [_json_error("Missing query parameter")]
    try:
        proxy = await get_search_proxy()
        result = await proxy.qna_search(
            query=query,
            max_results=arguments.get("max_results", 5),
            search_depth=arguments.get("search_depth", "advanced"),
        )
        return [_json_text(result)]
    except SearchProxyError as exc:
        return [_json_error(str(exc))]
    except Exception as exc:
        return [_json_error(f"Unexpected error: {exc}")]


async def _handle_extract_url(arguments: dict[str, Any]) -> list[TextContent]:
    url = arguments.get("url")
    if not url:
        return [_json_error("Missing url parameter")]
    try:
        proxy = await get_search_proxy()
        result = await proxy.extract(url=url)
        return [_json_text(result)]
    except SearchProxyError as exc:
        return [_json_error(str(exc))]
    except Exception as exc:
        return [_json_error(f"Unexpected error: {exc}")]


def _gmail_audit_context(tool: str) -> dict[str, str]:
    return {"source": "mcp_stdio", "tool": tool}


async def _handle_gmail_list_messages(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    if not profile:
        return [_json_error("Missing required parameter: profile")]
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
        return [_json_error(f"Gmail list failed: {exc}")]


async def _handle_gmail_get_message(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    message_id = arguments.get("message_id")
    if not profile or not message_id:
        return [_json_error("Missing required parameters: profile and message_id")]
    try:
        result = gmail_service.get_message(
            profile_id=profile,
            message_id=message_id,
            format=arguments.get("format", "metadata"),
            audit_context=_gmail_audit_context("gmail_get_message"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [_json_error(f"Gmail get message failed: {exc}")]


async def _handle_gmail_get_attachment(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    message_id = arguments.get("message_id")
    attachment_id = arguments.get("attachment_id")
    if not profile or not message_id or not attachment_id:
        return [
            _json_error(
                "Missing required parameters: profile, message_id, attachment_id"
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
        return [_json_error(f"Gmail get attachment failed: {exc}")]


async def _handle_gmail_list_labels(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    if not profile:
        return [_json_error("Missing required parameter: profile")]
    try:
        result = gmail_service.list_labels(
            profile_id=profile,
            audit_context=_gmail_audit_context("gmail_list_labels"),
        )
        return [_json_text(result)]
    except Exception as exc:
        return [_json_error(f"Gmail list labels failed: {exc}")]


async def _handle_gmail_modify_labels(arguments: dict[str, Any]) -> list[TextContent]:
    profile = arguments.get("profile") or arguments.get("profile_id")
    message_id = arguments.get("message_id")
    allow_mutation = bool(arguments.get("allow_mutation"))
    if not profile or not message_id:
        return [_json_error("Missing required parameters: profile and message_id")]
    if not allow_mutation:
        return [_json_error("allow_mutation must be true to modify labels")]
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
        return [_json_error(f"Gmail modify labels failed: {exc}")]


async def _handle_add_relationship(arguments: dict[str, Any]) -> list[TextContent]:
    source_id = arguments.get("source_id")
    predicate = arguments.get("predicate")
    target = arguments.get("target")
    if not source_id or not predicate or not target:
        return [
            _json_error("Missing required parameters: source_id, predicate, and target")
        ]
    try:
        result = _add_relationship(
            source_id=source_id, predicate=predicate, target=target
        )
        return [_json_text(result)]
    except Exception as exc:
        return [_json_error(f"Failed to add relationship: {str(exc)}")]


async def _handle_remove_relationship(arguments: dict[str, Any]) -> list[TextContent]:
    source_id = arguments.get("source_id")
    predicate = arguments.get("predicate")
    target = arguments.get("target")
    if not source_id or not predicate or not target:
        return [
            _json_error("Missing required parameters: source_id, predicate, and target")
        ]
    try:
        result = _remove_relationship(
            source_id=source_id, predicate=predicate, target=target
        )
        return [_json_text(result)]
    except Exception as exc:
        return [_json_error(f"Failed to remove relationship: {str(exc)}")]


async def _handle_delete_concept(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    simulate = arguments.get("simulate", True)
    if not concept_id:
        return [_json_error("Missing concept_id parameter")]
    result = simulate_or_delete_concept(concept_id, execute=not simulate)
    return [_json_text(result)]


async def _handle_merge_concepts(arguments: dict[str, Any]) -> list[TextContent]:
    source_id = arguments.get("source_id")
    target_id = arguments.get("target_id")
    simulate = arguments.get("simulate", True)
    if not source_id or not target_id:
        return [_json_error("Missing source_id or target_id parameter")]
    result = merge_concepts(source_id, target_id, simulate=simulate)
    return [_json_text(result)]


async def _handle_update_concept(arguments: dict[str, Any]) -> list[TextContent]:
    concept_id = arguments.get("concept_id")
    update_data = arguments.get("update_data")
    if not concept_id:
        return [_json_error("Missing concept_id parameter")]
    if not update_data or not isinstance(update_data, dict):
        return [_json_error("Missing or invalid update_data dictionary")]
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
        return [_json_error("Missing query parameter")]
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


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[list[TextContent]]]] = {
    "get_context": _handle_get_context,
    "create_concepts": _handle_create_concepts,
    "find_subconcepts": _handle_find_subconcepts,
    "find_concepts_by_name": _handle_find_concepts_by_name,
    "von_chat_run": _handle_von_chat_run,
    "add_names": _handle_add_names,
    "get_tree": _handle_get_tree,
    "fetch_concept": _handle_fetch_concept,
    "search_concepts": _handle_search_concepts,
    "vontology_concept_search": _handle_search_concepts,
    "extract_annotations": _handle_extract_annotations,
    "search_arxiv": _handle_search_arxiv,
    "get_paper_metadata": _handle_get_paper_metadata,
    "download_paper": _handle_download_paper,
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
}


async def main():
    """Run the MCP server over stdio."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
