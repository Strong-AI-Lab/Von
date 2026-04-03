"""Canonical MCP tool contract registry and surface adapters.

This module is the single source of truth for MCP tool contracts across
surfaces:
- Internal gateway catalogue metadata
- Vontology stdio `list_tools`
- VonRAG stdio `list_tools`
- Static manifest generation (`vontology_mcp.json`)
- Family-specific external server surfaces (for example Jira)

JVNAUTOSCI-1144: eliminate drift by deriving surface exposure from one
registry rather than hand-maintained per-surface lists.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping

from .catalogue import build_default_catalogue
from .gateway import MethodDefinition
from .schemas import Schema, schema_to_json_schema

SURFACE_INTERNAL_CATALOGUE = "internal_catalogue"
SURFACE_VONTOLOGY_STDIO = "vontology_stdio"
SURFACE_VONRAG_STDIO = "vonrag_stdio"
SURFACE_MANIFEST = "manifest"
SURFACE_JIRA_FAMILY_SERVER = "jira_family_server"

KNOWN_SURFACES: tuple[str, ...] = (
    SURFACE_INTERNAL_CATALOGUE,
    SURFACE_VONTOLOGY_STDIO,
    SURFACE_VONRAG_STDIO,
    SURFACE_MANIFEST,
    SURFACE_JIRA_FAMILY_SERVER,
)

# NOTE: Keep these names aligned with the stdio tool-call handlers in
# src/backend/mcp_server/mcp_stdio_server.py. This set defines what the
# Vontology stdio surface intentionally exposes.
VONTOLOGY_STDIO_EXPOSED_TOOL_NAMES: tuple[str, ...] = (
    "add_names",
    "add_relationship",
    "assign_task",
    "audit_concept_text_relations",
    "concept_exists",
    "context_search",
    "create_concepts",
    "create_task",
    "delete_concept",
    "delete_text_relation",
    "download_paper",
    "build_paper_recommendations",
    "extract_annotations",
    "extract_url",
    "fetch_concept",
    "fetch_concept_content",
    "finalise_cached_paper",
    "materialise_scholarly_representation_for_file_copy",
    "find_relations_with_argument",
    "find_concepts_by_name",
    "find_subconcepts",
    "get_concept_index_status",
    "get_context",
    "get_paper_metadata",
    "get_task",
    "get_text_relations",
    "get_text_relations_summary",
    "get_tree",
    "gmail_get_attachment",
    "gmail_get_message",
    "gmail_list_labels",
    "gmail_list_messages",
    "gmail_modify_labels",
    "jira_add_comment",
    "jira_add_attachment",
    "jira_create_issue",
    "jira_delete_issue_link",
    "jira_get_auth_config",
    "jira_get_issue",
    "jira_get_myself",
    "jira_get_transitions",
    "jira_link_issue",
    "jira_search",
    "jira_transition",
    "jira_update_issue",
    "list_recent_screenshots",
    "list_my_tasks",
    "merge_concepts",
    "qna_search",
    "renderer_resolve_applicability",
    "upsert_renderer_profile",
    "remove_relationship",
    "preview_remove_relationship",
    "remove_relationships_bulk",
    "undo_relationship_removal",
    "resolve_concept_by_name",
    "search_arxiv",
    "search_concepts",
    "search_knowledge_base",
    "search_web",
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
    "update_concept",
    "update_task_status",
    "update_text_relation",
    "upsert_singleton_text_relation",
    "upsert_text_relation",
    "von_chat_run",
    "vontology_concept_search",
    "workflow_bind_event",
    "workflow_cancel_instance",
    "workflow_create_instance",
    "workflow_execute",
    "workflow_create_schedule",
    "workflow_delete_event_binding",
    "workflow_delete_schedule",
    "workflow_get_execution_trace",
    "workflow_get_instance",
    "workflow_get_schedule",
    "workflow_list_definitions",
    "workflow_list_event_bindings",
    "workflow_list_execution_traces",
    "workflow_list_instances",
    "workflow_list_schedules",
    "workflow_mcp_health_check",
    "workflow_retry_instance",
    "workflow_set_event_binding_enabled",
    "workflow_set_schedule_enabled",
    "workflow_trigger_schedule",
)

# Vontology MCP manifest intentionally mirrors the Vontology stdio surface.
MANIFEST_EXPOSED_TOOL_NAMES: tuple[str, ...] = VONTOLOGY_STDIO_EXPOSED_TOOL_NAMES

# Focused read-oriented RAG surface.
VONRAG_STDIO_EXPOSED_TOOL_NAMES: tuple[str, ...] = (
    "get_related_concepts",
    "index_concept_text",
    "rag_get_item",
    "rag_get_status",
    "rag_list_collections",
    "rag_list_indexed",
    "rag_sync_text_relations",
    "search_concept_descriptions",
    "search_knowledge_base",
)

# Legacy external Jira MCP server surface currently supports this subset.
JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES: tuple[str, ...] = (
    "jira_add_comment",
    "jira_get_issue",
    "jira_get_transitions",
    "jira_search",
    "jira_transition",
)


@dataclass(frozen=True)
class ToolSurfaceExposure:
    expose_in_internal_catalogue: bool = False
    expose_in_vontology_stdio: bool = False
    expose_in_vonrag_stdio: bool = False
    expose_in_manifest: bool = False
    expose_in_jira_family_server: bool = False

    def is_exposed_on(self, surface: str) -> bool:
        if surface == SURFACE_INTERNAL_CATALOGUE:
            return self.expose_in_internal_catalogue
        if surface == SURFACE_VONTOLOGY_STDIO:
            return self.expose_in_vontology_stdio
        if surface == SURFACE_VONRAG_STDIO:
            return self.expose_in_vonrag_stdio
        if surface == SURFACE_MANIFEST:
            return self.expose_in_manifest
        if surface == SURFACE_JIRA_FAMILY_SERVER:
            return self.expose_in_jira_family_server
        raise ValueError(
            f"Unknown surface '{surface}'. Supported surfaces: {', '.join(KNOWN_SURFACES)}"
        )


@dataclass(frozen=True)
class CanonicalMCPToolContract:
    name: str
    family: str
    category: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    exposure: ToolSurfaceExposure
    internal_method_name: str | None = None
    internal_only: bool = False
    debug_only: bool = False
    requires_namespace: bool = False

    def to_surface_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

def _infer_tool_family(tool_name: str) -> str:
    if tool_name.startswith("jira_"):
        return "jira"
    if tool_name.startswith("github_"):
        return "github"
    if tool_name.startswith("workflow_"):
        return "workflow"
    if tool_name.startswith("gmail_"):
        return "gmail"
    if tool_name.startswith("rag_") or tool_name in {
        "get_related_concepts",
        "index_concept_text",
        "search_concept_descriptions",
        "search_knowledge_base",
    }:
        return "rag"
    if tool_name in {
        "search_web",
        "context_search",
        "qna_search",
        "extract_url",
        "resilient_extract_url",
        "search_proxy_diagnostics",
    }:
        return "search"
    if tool_name in {
        "search_arxiv",
        "get_paper_metadata",
        "download_paper",
        "finalise_cached_paper",
        "materialise_scholarly_representation_for_file_copy",
        "list_papers",
        "read_paper",
        "read_file_copy",
        "interpret_file_copy",
        "import_local_file_copy",
    }:
        return "arxiv"
    if tool_name in {"index_file_copy"}:
        return "rag"
    if tool_name.startswith("linkedin_"):
        return "linkedin"
    if tool_name.startswith("task_") or tool_name in {
        "create_task",
        "get_task",
        "list_my_tasks",
        "update_task_status",
        "assign_task",
    }:
        return "task"
    if tool_name.startswith("chat_") or tool_name.startswith("settings_"):
        return "internal"
    if tool_name in {"list_recent_screenshots"}:
        return "internal"
    return "vontology"


def _requires_namespace(schema: Schema) -> bool:
    return "namespace" in schema.required


def _build_exposure(name: str, *, internal: bool) -> ToolSurfaceExposure:
    return ToolSurfaceExposure(
        expose_in_internal_catalogue=internal,
        expose_in_vontology_stdio=name in VONTOLOGY_STDIO_EXPOSED_TOOL_NAMES,
        expose_in_vonrag_stdio=name in VONRAG_STDIO_EXPOSED_TOOL_NAMES,
        expose_in_manifest=name in MANIFEST_EXPOSED_TOOL_NAMES,
        expose_in_jira_family_server=name in JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES,
    )


def _contract_from_method_definition(
    definition: MethodDefinition,
) -> CanonicalMCPToolContract:
    exposure = _build_exposure(definition.name, internal=True)
    return CanonicalMCPToolContract(
        name=definition.name,
        family=_infer_tool_family(definition.name),
        category=definition.category,
        description=definition.description or f"Execute {definition.name}.",
        input_schema=schema_to_json_schema(definition.input_schema),
        output_schema=(
            schema_to_json_schema(definition.output_schema)
            if definition.output_schema is not None
            else None
        ),
        exposure=exposure,
        internal_method_name=definition.name,
        internal_only=not any(
            (
                exposure.expose_in_vontology_stdio,
                exposure.expose_in_vonrag_stdio,
                exposure.expose_in_manifest,
                exposure.expose_in_jira_family_server,
            )
        ),
        requires_namespace=_requires_namespace(definition.input_schema),
    )


def _supplemental_surface_only_contracts() -> dict[str, CanonicalMCPToolContract]:
    supplemental_specs: dict[str, dict[str, Any]] = {
        "find_subconcepts": {
            "family": "vontology",
            "category": "read",
            "description": "Finds all direct subconcepts (children) of a given concept in the Vontology hierarchy",
            "input_schema": {
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "The concept ID to find children of",
                    }
                },
                "required": ["concept_id"],
            },
        },
        "find_concepts_by_name": {
            "family": "vontology",
            "category": "read",
            "description": "Searches for concepts in the Vontology by name substring",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name substring to search for",
                    }
                },
                "required": ["name"],
            },
        },
        "audit_concept_text_relations": {
            "family": "vontology",
            "category": "read",
            "description": (
                "Audit all text relations attached to a concept. Reports accessibility status for each relation "
                "and whether the concept can be safely renamed. Use before rename operations to identify and "
                "resolve blocking inaccessible relations."
            ),
            "input_schema": {
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
        },
        "get_concept_index_status": {
            "family": "vontology",
            "category": "read",
            "description": (
                "Get statistics about the concept embedding index. Shows counts by status (pending, indexed, "
                "stale, failed), concepts needing indexing, and index namespace."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "concept_id": {
                        "type": "string",
                        "description": "Optional: get status for a specific concept instead of aggregate stats",
                    }
                },
                "required": [],
            },
        },
        "von_chat_run": {
            "family": "internal",
            "category": "write",
            "description": (
                "Run the Von chat orchestrator (LLM + internal MCP tools) and return a redacted trace. "
                "By default, this runs in dry-run mode (read-only tools only). To allow write tools, set "
                "VON_INTERNAL_MCP_ENABLE=1 and VON_MCP_ALLOW_WRITES=1 and pass allow_writes=true."
            ),
            "input_schema": {
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
                    "model": {"type": "string", "description": "Optional model override"},
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
                        "default": 30,
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
        },
        "create_task": {
            "family": "task",
            "category": "write",
            "description": (
                "Create a task from a conversation. Tasks are stored as Vontology concepts with rich metadata. "
                "Optionally links to the originating conversation."
            ),
            "input_schema": {
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
        },
        "get_task": {
            "family": "task",
            "category": "read",
            "description": "Get details of a specific task by its concept ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_concept_id": {
                        "type": "string",
                        "description": "The task's concept ID (e.g., '#V#task_abc123')",
                    }
                },
                "required": ["task_concept_id"],
            },
        },
        "list_my_tasks": {
            "family": "task",
            "category": "read",
            "description": "List tasks assigned to a user, optionally filtered by status.",
            "input_schema": {
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
        },
        "update_task_status": {
            "family": "task",
            "category": "write",
            "description": "Update the status of a task.",
            "input_schema": {
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
        },
        "assign_task": {
            "family": "task",
            "category": "write",
            "description": "Assign or reassign a task to a user.",
            "input_schema": {
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
        },
    }

    contracts: dict[str, CanonicalMCPToolContract] = {}
    for name, spec in supplemental_specs.items():
        exposure = _build_exposure(name, internal=False)
        contracts[name] = CanonicalMCPToolContract(
            name=name,
            family=str(spec["family"]),
            category=str(spec["category"]),
            description=str(spec["description"]),
            input_schema=dict(spec["input_schema"]),
            output_schema=None,
            exposure=exposure,
            internal_method_name=None,
            internal_only=not any(
                (
                    exposure.expose_in_vontology_stdio,
                    exposure.expose_in_vonrag_stdio,
                    exposure.expose_in_manifest,
                    exposure.expose_in_jira_family_server,
                )
            ),
            debug_only=(name == "von_chat_run"),
            requires_namespace=False,
        )
    return contracts


def _validate_surface_coverage(contracts: Mapping[str, CanonicalMCPToolContract]) -> None:
    contract_names = set(contracts.keys())
    declared: dict[str, set[str]] = {
        SURFACE_VONTOLOGY_STDIO: set(VONTOLOGY_STDIO_EXPOSED_TOOL_NAMES),
        SURFACE_MANIFEST: set(MANIFEST_EXPOSED_TOOL_NAMES),
        SURFACE_VONRAG_STDIO: set(VONRAG_STDIO_EXPOSED_TOOL_NAMES),
        SURFACE_JIRA_FAMILY_SERVER: set(JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES),
    }

    for surface, names in declared.items():
        missing = sorted(names - contract_names)
        if missing:
            raise RuntimeError(
                f"Canonical MCP registry missing tools required for surface '{surface}': {missing}"
            )


@lru_cache(maxsize=1)
def get_canonical_tool_registry() -> dict[str, CanonicalMCPToolContract]:
    catalogue = build_default_catalogue()
    contracts: dict[str, CanonicalMCPToolContract] = {}

    for method_name in catalogue.list_methods():
        definition = catalogue.get(method_name)
        contracts[method_name] = _contract_from_method_definition(definition)

    for supplemental_name, contract in _supplemental_surface_only_contracts().items():
        if supplemental_name in contracts:
            raise RuntimeError(
                f"Duplicate canonical MCP contract name: '{supplemental_name}'"
            )
        contracts[supplemental_name] = contract

    _validate_surface_coverage(contracts)
    return contracts


def get_surface_contracts(surface: str) -> list[CanonicalMCPToolContract]:
    contracts = get_canonical_tool_registry()
    selected = [
        contract
        for contract in contracts.values()
        if contract.exposure.is_exposed_on(surface)
    ]
    return sorted(selected, key=lambda contract: contract.name)


def get_surface_tool_payloads(surface: str) -> list[dict[str, Any]]:
    return [contract.to_surface_payload() for contract in get_surface_contracts(surface)]


def get_manifest_payload() -> dict[str, Any]:
    return {
        "mcpVersion": "1.0.0",
        "tools": get_surface_tool_payloads(SURFACE_MANIFEST),
    }


def iter_exposed_tool_names(surface: str) -> Iterable[str]:
    for contract in get_surface_contracts(surface):
        yield contract.name

