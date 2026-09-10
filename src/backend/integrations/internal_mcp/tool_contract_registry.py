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
from typing import Any, Iterable

from .catalogue import build_default_catalogue
from .gateway import MethodDefinition
from .schemas import Schema, schema_to_json_schema
from ...services.tool_metadata_service import (
    get_tool_description,
    get_tool_family,
    get_tool_operation_category,
    get_tool_surface_exposure_metadata,
    is_usable_tool_capability_description,
)

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

# Opaque, server-issued execution data accepted only by direct stdio canonical
# ontology writes.  These are not identity, organisation, or role parameters:
# the mutation boundary validates a delegation against its exact intent and
# effect before invoking a write primitive.
_STDIO_GOVERNED_ONTOLOGY_MUTATION_TOOLS = frozenset(
    {
        "add_names",
        "add_relationship",
        "change_concept_publication_scope",
        "create_concepts",
        "delete_concept",
        "delete_text_relation",
        "merge_concepts",
        "preview_concept_publication_scope_change",
        "remove_relationship",
        "remove_relationships_bulk",
        "update_concept",
        "update_text_relation",
        "upsert_singleton_text_relation",
        "upsert_text_relation",
    }
)

_STDIO_UNTRUSTED_ONTOLOGY_AUTHORITY_CONTRACT_FIELDS = frozenset(
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


def _with_stdio_ontology_delegation_contract(
    tool_name: str,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    """Expose opaque delegation carriers without adding identity claims."""

    if tool_name not in _STDIO_GOVERNED_ONTOLOGY_MUTATION_TOOLS:
        return input_schema

    payload = dict(input_schema)
    properties = dict(payload.get("properties") or {})
    for field_name in _STDIO_UNTRUSTED_ONTOLOGY_AUTHORITY_CONTRACT_FIELDS:
        properties.pop(field_name, None)
    properties.update(
        {
            "ontology_delegation_id": {
                "type": "string",
                "description": (
                    "Opaque server-issued delegation for this exact canonical "
                    "ontology mutation. It does not identify or authenticate a user."
                ),
            },
            "ontology_effect_id": {
                "type": "string",
                "description": (
                    "Opaque server-issued effect identifier bound to the delegation. "
                    "It is used for exact authorisation and idempotent receipts."
                ),
            },
        }
    )
    payload["properties"] = properties
    return payload


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
    if tool_name.startswith("coding_agent_"):
        return "internal"
    if tool_name.startswith("conversation_") or tool_name.startswith(
        "shared_conversation_"
    ):
        return "conversation"
    if tool_name.startswith("chat_") or tool_name.startswith("settings_"):
        return "internal"
    if tool_name in {"list_recent_screenshots"}:
        return "internal"
    return "vontology"


def _requires_namespace(schema: Schema) -> bool:
    return "namespace" in schema.required


def _build_exposure(name: str, *, internal: bool) -> ToolSurfaceExposure:
    metadata = get_tool_surface_exposure_metadata(
        name,
        allow_registry_fallback=False,
    )
    return ToolSurfaceExposure(
        expose_in_internal_catalogue=internal,
        expose_in_vontology_stdio=metadata.expose_in_vontology_stdio,
        expose_in_vonrag_stdio=metadata.expose_in_vonrag_stdio,
        expose_in_manifest=metadata.expose_in_manifest,
        expose_in_jira_family_server=metadata.expose_in_jira_family_server,
    )


def _usable_contract_description(description: str | None) -> str | None:
    """Return a code-registered description only when it says something real.

    Guards against a bare placeholder in a MethodDefinition silently becoming
    the published contract text.
    """
    cleaned = str(description or "").strip()
    if not cleaned:
        return None
    return cleaned if is_usable_tool_capability_description(cleaned) else None


def _contract_from_method_definition(
    definition: MethodDefinition,
) -> CanonicalMCPToolContract:
    exposure = _build_exposure(definition.name, internal=True)
    return CanonicalMCPToolContract(
        name=definition.name,
        family=get_tool_family(
            definition.name,
            fallback_family=_infer_tool_family(definition.name),
            allow_registry_fallback=False,
        ),
        # The published contract is code-authoritative. Vontology metadata may
        # fill a gap the code leaves, but must not redefine what a registered
        # tool claims to do or whether it reads or writes. #V#mcp_tool concepts
        # record their description once at first bootstrap and are never
        # refreshed, so preferring them froze stale text over later code
        # improvements and made this surface vary with database state.
        category=(
            definition.category
            or get_tool_operation_category(
                definition.name,
                allow_registry_fallback=False,
            )
        ),
        description=(
            _usable_contract_description(definition.description)
            or get_tool_description(definition.name)
            or f"Execute {definition.name}."
        ),
        input_schema=_with_stdio_ontology_delegation_contract(
            definition.name,
            schema_to_json_schema(definition.input_schema),
        ),
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


_SUPPLEMENTAL_SURFACE_ONLY_SPECS: dict[str, dict[str, Any]] = {
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
        "category": "read",
        "description": (
            "Run one thin adaptive read-only Von turn and return its response, "
            "bounded evidence index, and redacted observational trace. The model "
            "may choose among delegated read capabilities; writes require an "
            "explicit authorised effect tool or workflow."
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
                "model": {
                    "type": "string",
                    "description": "Optional model override",
                },
                "user_namespace": {
                    "type": "string",
                    "description": "Optional namespace claim accepted only from a trusted operator context; it never authenticates itself",
                },
                "gmail_profile": {
                    "type": "string",
                    "description": "Optional trusted-operator Gmail profile binding",
                },
                "auxiliary_system_prompt": {
                    "type": "string",
                    "description": "Optional caller-supplied supplementary context; treated as untrusted user context, not a system authority",
                },
                "timeout_seconds": {
                    "type": "number",
                    "default": 90,
                    "description": "Legacy alias for advisory_seconds",
                },
                "advisory_seconds": {
                    "type": "number",
                    "default": 90,
                    "description": (
                        "Elapsed-time advisory for the adaptive turn; crossing "
                        "it does not discard the result"
                    ),
                },
                "max_string_chars": {
                    "type": "integer",
                    "default": 8000,
                    "description": "Max characters retained for any string in the trace",
                },
                "turn_id": {
                    "type": "string",
                    "description": "Optional caller correlation identifier for this ephemeral read-only turn",
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
                "project_concept_id": {
                    "type": "string",
                    "description": "Project selected from task_list_projects.",
                },
                "collection_concept_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Collections associated with the selected project.",
                },
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


def supplemental_tool_names() -> frozenset[str]:
    """Names of surface-only contracts, without constructing them.

    Constructing them resolves metadata and exposure, which reads Vontology
    and runs access control. Callers that only need membership must use this.
    """
    return frozenset(_SUPPLEMENTAL_SURFACE_ONLY_SPECS)


def _supplemental_surface_only_contracts() -> dict[str, CanonicalMCPToolContract]:
    supplemental_specs = _SUPPLEMENTAL_SURFACE_ONLY_SPECS

    contracts: dict[str, CanonicalMCPToolContract] = {}
    for name, spec in supplemental_specs.items():
        exposure = _build_exposure(name, internal=False)
        contracts[name] = CanonicalMCPToolContract(
            name=name,
            family=get_tool_family(
                name,
                fallback_family=str(spec["family"]),
                allow_registry_fallback=False,
            ),
            category=(
                get_tool_operation_category(
                    name,
                    fallback_operation_category=str(spec["category"]),
                    allow_registry_fallback=False,
                )
                or str(spec["category"])
            ),
            description=(
                get_tool_description(
                    name,
                    fallback_description=str(spec["description"]),
                )
                or str(spec["description"])
            ),
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

    return contracts


def invalidate_canonical_tool_registry() -> None:
    get_canonical_tool_registry.cache_clear()


def get_surface_contracts(surface: str) -> list[CanonicalMCPToolContract]:
    contracts = get_canonical_tool_registry()
    selected = [
        contract
        for contract in contracts.values()
        if contract.exposure.is_exposed_on(surface)
    ]
    return sorted(selected, key=lambda contract: contract.name)


def get_surface_tool_payloads(surface: str) -> list[dict[str, Any]]:
    return [
        contract.to_surface_payload() for contract in get_surface_contracts(surface)
    ]


def get_manifest_payload() -> dict[str, Any]:
    return {
        "mcpVersion": "1.0.0",
        "tools": get_surface_tool_payloads(SURFACE_MANIFEST),
    }


def iter_exposed_tool_names(surface: str) -> Iterable[str]:
    for contract in get_surface_contracts(surface):
        yield contract.name
