"""Service for loading MCP tool metadata from Vontology with caching.

This service provides tool salience levels and display templates loaded from
Vontology concepts (#V#mcp_tool instances), with in-memory caching for performance.

Tool salience levels:
- high: Always show with detailed summary (create, search, relationship, task ops)
- medium: Show tool name with brief result info (search, gmail, text relations)
- low: Aggregate count only (internal lookups, audits, status checks)
- none: Internal tools, never show to users

The cache is refreshed periodically (default 5 minutes) to support eventual consistency.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from src.backend.services.required_tool_identity_service import (
    preferred_tool_metadata_surface_name,
)

logger = logging.getLogger(__name__)

# Cache TTL in seconds (5 minutes default)
_CACHE_TTL_SECONDS = 300

# Lock for thread-safe cache access
_cache_lock = threading.Lock()

# In-memory cache
_tool_metadata_cache: dict[str, "ToolMetadata"] = {}
_cache_timestamp: float = 0.0
_cache_loaded: bool = False

_NON_FAMILY_TOOL_CATEGORIES = frozenset({"read", "write"})


@dataclass
class ToolMetadata:
    """Metadata for an MCP tool loaded from Vontology."""

    tool_name: str
    concept_id: str | None = None
    salience: str = "medium"  # high, medium, low, none
    display_template: str | None = None
    description: str | None = None
    category: str | None = None  # vontology, arxiv, gmail, jira, task, search, etc.
    planner_hint: str | None = None
    dispatch_surface_family: str | None = None
    evidence_surface_family: str | None = None
    external_surface: bool | None = None
    operation_category: str | None = None
    evidence_role: str | None = None
    evidence_kind: str | None = None
    required_tool_operation_class: str | None = None
    required_tool_target_closure: bool | None = None
    required_tool_target_argument_names: tuple[str, ...] = field(default_factory=tuple)
    required_tool_target_payload_field_names: tuple[str, ...] = field(
        default_factory=tuple
    )
    target_concept_argument_name: str | None = None
    target_concept_source: str | None = None
    target_concept_max_count: int | None = None
    default_payload: dict[str, Any] | None = None
    identifier_argument_name: str | None = None
    identifier_pattern: str | None = None
    identifier_source: str | None = None
    identifier_max_count: int | None = None
    identifier_normalise: str | None = None
    identifier_default_payload: dict[str, Any] | None = None
    expose_in_vontology_stdio: bool | None = None
    expose_in_vonrag_stdio: bool | None = None
    expose_in_manifest: bool | None = None
    expose_in_jira_family_server: bool | None = None

    @property
    def is_high_salience(self) -> bool:
        return self.salience == "high"

    @property
    def is_visible(self) -> bool:
        return self.salience != "none"


@dataclass(frozen=True)
class ToolDispatchSurfaceMetadata:
    """Dispatch-preflight surface metadata derived from authoritative tool metadata."""

    surface_family: str
    evidence_surface_family: str
    external_surface: bool = False


@dataclass(frozen=True)
class ToolSurfaceExposureMetadata:
    """Per-surface exposure metadata derived from authoritative tool metadata."""

    expose_in_vontology_stdio: bool = False
    expose_in_vonrag_stdio: bool = False
    expose_in_manifest: bool = False
    expose_in_jira_family_server: bool = False


@dataclass(frozen=True)
class ToolTargetConceptBindingMetadata:
    """Intrinsic focal-concept argument binding metadata for an MCP tool."""

    target_concept_argument_name: str
    target_concept_source: str = "focal_concept"
    target_concept_max_count: int | None = None
    default_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolIdentifierBindingMetadata:
    """Exact identifier binding metadata for a required-tool retry."""

    identifier_argument_name: str
    identifier_pattern: str
    identifier_source: str = "user_text"
    identifier_max_count: int | None = None
    identifier_normalise: str | None = None
    default_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRequiredObligationMetadata:
    """Tool semantics needed by required-tool completion-gate accounting."""

    operation_class: str | None = None
    target_closure_required: bool | None = None
    target_argument_names: tuple[str, ...] = field(default_factory=tuple)
    target_payload_field_names: tuple[str, ...] = field(default_factory=tuple)


_REQUIRED_TOOL_OPERATION_CLASSES = frozenset(
    {
        "search_or_resolution_read",
        "verification_read",
        "mutation_write",
        "external_side_effect",
        "workflow_execute",
    }
)


_DEFAULT_DISPATCH_SURFACE_METADATA: dict[str, ToolDispatchSurfaceMetadata] = {
    "vontology": ToolDispatchSurfaceMetadata(
        surface_family="knowledge_base",
        evidence_surface_family="knowledge_base",
        external_surface=False,
    ),
    "rag": ToolDispatchSurfaceMetadata(
        surface_family="knowledge_base",
        evidence_surface_family="knowledge_base",
        external_surface=False,
    ),
    "search": ToolDispatchSurfaceMetadata(
        surface_family="web",
        evidence_surface_family="web",
        external_surface=True,
    ),
    "arxiv": ToolDispatchSurfaceMetadata(
        surface_family="arxiv",
        evidence_surface_family="arxiv",
        external_surface=True,
    ),
    "jira": ToolDispatchSurfaceMetadata(
        surface_family="jira",
        evidence_surface_family="jira",
        external_surface=True,
    ),
    "gmail": ToolDispatchSurfaceMetadata(
        surface_family="gmail",
        evidence_surface_family="gmail",
        external_surface=True,
    ),
    "task": ToolDispatchSurfaceMetadata(
        surface_family="task",
        evidence_surface_family="task",
        external_surface=False,
    ),
    "conversation": ToolDispatchSurfaceMetadata(
        surface_family="message",
        evidence_surface_family="message",
        external_surface=False,
    ),
    "message": ToolDispatchSurfaceMetadata(
        surface_family="message",
        evidence_surface_family="message",
        external_surface=False,
    ),
}


# Authority-boundary warning: this is a backward-compatibility support table for
# generic tool exposure metadata. Do not add domain modelling policy here just
# because a nearby failed turn named a domain object. Durable tool policy belongs
# in Vontology tool metadata, workflows, prompt concepts, or represented
# predicates; Python defaults should stay wiring/validation-oriented.
_DEFAULT_TOOL_METADATA: dict[str, dict[str, Any]] = {
    # HIGH salience - always show with details
    "create_concepts": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Created {count} concept(s): {names}",
    },
    "search_concepts": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Found {count} concepts for {query}",
        "description": (
            "Search Vontology names and descriptions for possible entities, "
            "predicates, and types. Start with exact or substring search and "
            "enrich only when needed. This discovers possible schema, not the "
            "predicates actually used around an anchor or enumeration coverage."
        ),
        "planner_hint": (
            "Use when the entity, predicate, type, inverse, or reified shape is "
            "unknown, then make a relation-bearing read. For a broad category "
            "around a known anchor, use predicate incidence instead: schema search "
            "shows what is possible, not what is actually used there."
        ),
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "read",
        "evidence_role": "search",
    },
    "list_uncertain_relationship_assertions": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} uncertain relationship assertion(s)",
        "planner_hint": (
            "Use with source_id set to the focal concept when a request asks "
            "for uncertain, inferred, or fact-vs-inference relationship evidence. "
            "This is the review and read-back half of the lifecycle; when authorised "
            "intent is to mark or update a candidate for later review, inspect "
            "upsert_uncertain_relationship_assertion and reuse the represented "
            "predicate rather than minting one merely to encode uncertainty."
        ),
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "read",
        "evidence_role": "verification",
        "evidence_kind": "relation_bearing",
        "target_concept_argument_name": "source_id",
        "target_concept_source": "focal_concept",
        "target_concept_max_count": 2,
        "default_payload": {"include_legacy": True},
    },
    "upsert_uncertain_relationship_assertion": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Recorded uncertain relationship for review",
        "planner_hint": (
            "Use when a candidate relationship should be marked as possible or "
            "proposed for later review rather than asserted as fact. Reuse an "
            "existing represented predicate, including one returned by "
            "list_uncertain_relationship_assertions or the relevant represented "
            "lifecycle; do not mint a predicate merely to express uncertainty. "
            "Read back with list_uncertain_relationship_assertions."
        ),
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "write",
    },
    "add_relationship": {
        "salience": "high",
        "category": "vontology",
        "display_template": "{predicate} → {target}",
    },
    "remove_relationship": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Removed: {predicate} → {target}",
    },
    "preview_remove_relationship": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Preview remove: {predicate} → {target}",
    },
    "remove_relationships_bulk": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Bulk removed relationships: {removed_count}",
    },
    "undo_relationship_removal": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Undo restored: {restored_count}",
    },
    "add_names": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Added names: {names}",
    },
    "merge_concepts": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Merged {source} → {target}",
    },
    "delete_concept": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Deleted: {concept_id}",
    },
    "rename_concept": {
        "salience": "high",
        "category": "vontology",
        "display_template": "Renamed: {old_name} → {new_name}",
    },
    # Task tools (HIGH salience)
    "task_create": {
        "salience": "high",
        "category": "task",
        "display_template": "Created task: {title}",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "task_update_status": {
        "salience": "high",
        "category": "task",
        "display_template": "Status → {status}",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "task_assign": {
        "salience": "high",
        "category": "task",
        "display_template": "Assigned to: {assignee}",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "task_delete": {
        "salience": "high",
        "category": "task",
        "display_template": "Deleted task: {task_id}",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "shared_conversation_create_session": {
        "salience": "high",
        "category": "conversation",
        "display_template": "Session ready: {session_id}",
    },
    "shared_conversation_join_session": {
        "salience": "high",
        "category": "conversation",
        "display_template": "Joined session: {session_id}",
    },
    "shared_conversation_invite_create": {
        "salience": "high",
        "category": "conversation",
        "display_template": "Invite created: {invite_id}",
    },
    "shared_conversation_list_invites": {
        "salience": "medium",
        "category": "conversation",
        "display_template": "{count} invites",
    },
    "shared_conversation_respond_invite": {
        "salience": "high",
        "category": "conversation",
        "display_template": "Invite {action}: {invite_id}",
    },
    "conversation_list": {
        "salience": "medium",
        "category": "conversation",
        "display_template": "{count} conversations",
    },
    "conversation_get": {
        "salience": "medium",
        "category": "conversation",
        "display_template": "Read conversation: {session_id}",
    },
    "conversation_manage": {
        "salience": "high",
        "category": "conversation",
        "display_template": "Conversation {action}: {session_id}",
    },
    # Jira tools (HIGH salience)
    "jira_create_issue": {
        "salience": "high",
        "category": "jira",
        "display_template": "Created: {key}",
    },
    "jira_update_issue": {
        "salience": "high",
        "category": "jira",
        "display_template": "Updated: {key}",
    },
    "jira_move_issue": {
        "salience": "high",
        "category": "jira",
        "display_template": "Moved: {issue_key}",
    },
    "jira_add_comment": {
        "salience": "high",
        "category": "jira",
        "display_template": "Commented on: {key}",
    },
    "jira_add_attachment": {
        "salience": "high",
        "category": "jira",
        "display_template": "Attached: {filename}",
    },
    "jira_transition": {
        "salience": "high",
        "category": "jira",
        "display_template": "Transitioned: {key} → {status}",
    },
    "jira_link_issue": {
        "salience": "high",
        "category": "jira",
        "display_template": "Linked: {inward} ↔ {outward}",
    },
    # ArXiv tools (HIGH salience)
    "search_arxiv": {
        "salience": "high",
        "category": "arxiv",
        "display_template": "Found {count} papers",
        "dispatch_surface_family": "arxiv",
        "evidence_surface_family": "arxiv",
        "external_surface": True,
        "planner_hint": (
            "Use for recent or current arXiv literature retrieval. Query with the "
            "topic, person, or research thread rather than a generic placeholder."
        ),
    },
    "download_paper": {
        "salience": "high",
        "category": "arxiv",
        "display_template": "Downloaded: {arxiv_id}",
        "dispatch_surface_family": "arxiv",
        "evidence_surface_family": "arxiv",
        "external_surface": True,
    },
    "finalise_cached_paper": {
        "salience": "high",
        "category": "arxiv",
        "display_template": "Finalised: {arxiv_id}",
    },
    "materialise_scholarly_representation_for_file_copy": {
        "salience": "high",
        "category": "arxiv",
        "display_template": "Materialised paper: {paper_concept_id}",
    },
    # Owner-scoped LinkedIn export tools
    "linkedin_index_status": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "LinkedIn index: {records} records",
    },
    "linkedin_list_datasets": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "LinkedIn datasets: {count}",
    },
    "linkedin_search_export": {
        "salience": "high",
        "category": "linkedin",
        "display_template": "LinkedIn export matches: {count}",
    },
    "linkedin_search_connections": {
        "salience": "high",
        "category": "linkedin",
        "display_template": "LinkedIn connections: {count}",
    },
    "linkedin_search_messages": {
        "salience": "high",
        "category": "linkedin",
        "display_template": "LinkedIn messages: {count}",
    },
    "linkedin_list_connection_organisations": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "LinkedIn organisations: {count}",
    },
    "linkedin_get_record": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "LinkedIn record: {record_id}",
    },
    # Web/search tools (HIGH salience)
    "search_web": {
        "salience": "high",
        "category": "search",
        "display_template": "{count} web results",
        "dispatch_surface_family": "web",
        "evidence_surface_family": "web",
        "external_surface": True,
        "planner_hint": (
            "Use for current public-web information or recent external developments. "
            "Query with the concrete topic or entity, not a vague placeholder."
        ),
    },
    "extract_url": {
        "salience": "high",
        "category": "search",
        "display_template": "Extracted: {url}",
        "dispatch_surface_family": "web",
        "evidence_surface_family": "web",
        "external_surface": True,
    },
    "resilient_extract_url": {
        "salience": "high",
        "category": "search",
        "display_template": "Extracted: {url}",
        "dispatch_surface_family": "web",
        "evidence_surface_family": "web",
        "external_surface": True,
    },
    # MEDIUM salience - show tool name with brief info
    "jira_search": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Found {count} issues",
        "dispatch_surface_family": "jira",
        "evidence_surface_family": "jira",
        "external_surface": True,
        "planner_hint": (
            "Use for Jira issue retrieval. Prefer this over general task tools when "
            "the user asks about Jira issues or linked Jira tasks."
        ),
    },
    "jira_get_issue": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Issue: {key}",
        "dispatch_surface_family": "jira",
        "evidence_surface_family": "jira",
        "external_surface": True,
        "identifier_argument_name": "issue_key",
        "identifier_pattern": r"\b[A-Z][A-Z0-9]+-\d+\b",
        "identifier_source": "user_text",
        "identifier_max_count": 1,
        "identifier_normalise": "upper",
    },
    "jira_get_comments": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Comments: {issue_key}",
        "dispatch_surface_family": "jira",
        "evidence_surface_family": "jira",
        "external_surface": True,
        "identifier_argument_name": "issue_key",
        "identifier_pattern": r"\b[A-Z][A-Z0-9]+-\d+\b",
        "identifier_source": "user_text",
        "identifier_max_count": 1,
        "identifier_normalise": "upper",
    },
    "jira_get_project_issue_types": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Issue types: {project_key}",
    },
    "jira_get_bulk_operation_progress": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Bulk task: {task_id}",
    },
    "jira_get_transitions": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Transitions: {key}",
        "description": (
            "List available Jira workflow transitions/status changes for an "
            "issue key. Use this to get the transition list and transition IDs "
            "before calling jira_transition."
        ),
        "planner_hint": (
            "Use when the user asks for a Jira transition list, available "
            "status changes, or the transition IDs needed before calling "
            "jira_transition."
        ),
        "dispatch_surface_family": "jira",
        "evidence_surface_family": "jira",
        "external_surface": True,
    },
    "gmail_list_messages": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "{count} messages",
        "dispatch_surface_family": "gmail",
        "evidence_surface_family": "gmail",
        "external_surface": True,
    },
    "gmail_list_profiles": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "{count} profiles",
        "dispatch_surface_family": "gmail",
        "evidence_surface_family": "gmail",
        "external_surface": True,
        "operation_category": "read",
        "evidence_role": "search",
        "evidence_kind": "inventory_only",
    },
    "gmail_get_message": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Message: {subject}",
        "dispatch_surface_family": "gmail",
        "evidence_surface_family": "gmail",
        "external_surface": True,
    },
    "gmail_send_message": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Sent message: {subject}",
        "planner_hint": (
            "Use only when the user explicitly asks Von to send outbound email. "
            "Required payload fields are profile, to, subject, body_text, and "
            "allow_send=true; the result is Gmail send metadata, not the body."
        ),
    },
    "gmail_get_attachment": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Attachment: {filename}",
        "dispatch_surface_family": "gmail",
        "evidence_surface_family": "gmail",
        "external_surface": True,
        "operation_category": "read",
        "planner_hint": (
            "Call after gmail_get_message when an attachment is relevant to the "
            "user's request. It returns bounded text extracted in memory and creates "
            "no durable state; do not expect or attempt to decode raw base64. Use "
            "gmail_import_attachment only when the user asks to retain the file."
        ),
    },
    "gmail_import_attachment": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Imported attachment: {filename}",
        "dispatch_surface_family": "gmail",
        "evidence_surface_family": "gmail",
        "external_surface": True,
        "operation_category": "write",
        "planner_hint": (
            "Use only when the user or represented workflow asks to save, import, "
            "or represent a Gmail attachment. Requires profile, message_id, "
            "attachment_id, and allow_import=true; returns an actor-scoped durable "
            "computer_file_copy handle with canonical read-back."
        ),
    },
    "gmail_list_labels": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "{count} labels",
        "planner_hint": (
            "Use exact_name with require_exact_match=true when a workflow needs "
            "one existing mailbox-specific label ID before mutation."
        ),
    },
    "gmail_create_label": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Created label: {name}",
        "planner_hint": (
            "Use only when the user or represented workflow explicitly asks to "
            "create a Gmail label. Required payload fields are profile, name, "
            "and allow_mutation=true; use gmail_list_labels first when you need "
            "to check whether a label already exists."
        ),
    },
    "gmail_modify_labels": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Labels modified",
        "planner_hint": (
            "Use one message-level call for atomic add/remove. Set verify_after=true "
            "when the outcome requires independent canonical label read-back and "
            "lost-response reconciliation."
        ),
    },
    "upsert_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{action}: {predicate}",
        "planner_hint": (
            "Use only after the target concept is known and the predicate is a "
            "core text predicate or an existing #V# predicate concept. Do not "
            "invent ad-hoc #V# field-name predicates."
        ),
    },
    "upsert_singleton_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{action}: {predicate}",
        "planner_hint": (
            "Use for singleton text fields only with a core text predicate or an "
            "existing #V# predicate concept. Resolve or create durable predicates "
            "before writing; missing #V# predicates are rejected."
        ),
    },
    "record_source_processing_marker": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Recorded source-processing marker",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "write",
        "planner_hint": (
            "Use only after a represented workflow has verified that a source "
            "item was processed into durable Vontology artefacts. This records "
            "additive Vontology evidence and must not mutate the source system."
        ),
    },
    "get_source_processing_marker": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Read source-processing marker",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "read",
        "evidence_role": "verification",
    },
    "delete_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Deleted relation",
    },
    "update_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Updated: {predicate}",
    },
    "get_text_relations": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} relations",
    },
    "update_concept": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Updated: {concept_id}",
    },
    "find_subconcepts": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} children",
    },
    "fetch_concept": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Loaded: {name}",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "target_concept_argument_name": "concept_id",
        "target_concept_source": "required_fetch_or_focal_concept",
        "target_concept_max_count": 5,
        "planner_hint": (
            "Use when you already know the concept ID and need grounded represented "
            "facts for that specific concept. For relationship exploration, prefer "
            "a small predicate-filtered relation read; if relation enrichment is "
            "needed here, start with a small page without inline previews and hydrate "
            "only selected related concepts."
        ),
    },
    "find_relations_with_argument": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} relation(s)",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "operation_category": "read",
        "evidence_role": "verification",
        "evidence_kind": "relation_bearing",
        "external_surface": False,
        "target_concept_argument_name": "concept_id",
        "target_concept_source": "focal_concept",
        "target_concept_max_count": 2,
        "default_payload": {"include_concept_preview": False, "limit": 20},
        "planner_hint": (
            "Use for content-bearing relationships after resolving the anchor. "
            "Start predicate-filtered around limit 20 with previews off; hydrate "
            "selected concepts only when needed. A hit proves existence, not list "
            "or count completeness. If predicate, direction, or represented-form "
            "coverage is not established, inspect small any-direction incidence "
            "first, then pass every fitting exact predicate together in one "
            "predicate_filter read; do not select only the most obvious label. "
            "When a hit reaches a "
            "reified, event, claim, or role node, inspect represented role predicates "
            "before treating another filler as the requested entity; co-participation "
            "alone does not establish that role. A numeric object index selects one "
            "stored slot, not the whole object side."
        ),
    },
    "find_concepts_by_name": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Found: {names}",
    },
    "resolve_concept_by_name": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Resolved: {name}",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "read",
        "evidence_role": "search",
        "required_tool_operation_class": "search_or_resolution_read",
        "planner_hint": (
            "Use to resolve a named person, project, organisation, or other entity "
            "to a represented concept before relation lookup."
        ),
    },
    "resolve_concept_by_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Resolved: {text}",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "operation_category": "read",
        "evidence_role": "search",
        "required_tool_operation_class": "search_or_resolution_read",
        "planner_hint": (
            "Use when an exact represented text predicate and value, such as an "
            "email identifier, should identify a concept. Treat ambiguous or "
            "incomplete results as unresolved; never choose a candidate "
            "arbitrarily."
        ),
    },
    "vontology_concept_search": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} concepts for {query}",
    },
    "search_concept_descriptions": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} concepts for {query}",
    },
    "get_paper_metadata": {
        "salience": "medium",
        "category": "arxiv",
        "display_template": "Paper: {title}",
    },
    "list_papers": {
        "salience": "medium",
        "category": "arxiv",
        "display_template": "{count} papers",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "evidence_kind": "inventory_only",
        "external_surface": False,
        "planner_hint": (
            "Inventory only. Use for cached/stored paper listings, not as evidence "
            "of authorship, ownership, provenance, affiliation, or any entity "
            "relationship."
        ),
    },
    "get_predicate_incidence": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} predicate rows",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "evidence_kind": "relation_bearing",
        "external_surface": False,
        "required_tool_target_argument_names": ("concept_id", "instance_of"),
        "required_tool_target_payload_field_names": ("concept_id", "instance_of"),
        "target_concept_argument_name": "concept_id",
        "target_concept_source": "focal_concept",
        "target_concept_max_count": 2,
        "default_payload": {
            "argument_index": "subject",
            "relation_kind": "binary",
            "include_argument_type_counts": False,
            "include_concept_preview": False,
            "limit": 12,
        },
        "planner_hint": (
            "Use before a filtered relation read when coverage is not established. "
            "For a list, count, broad category, or negative, start one small binary "
            "argument_index='any' page with limit=20, "
            "include_concept_preview=false, include_text_snippets=false, and "
            "include_argument_type_counts=false. "
            "Treat every semantically fitting row, including inverse forms, as a "
            "coverage candidate; pass all returned predicate IDs together in one "
            "exact predicate_filter read, do not select only the most obvious label, "
            "deduplicate overlaps, "
            "and inspect role fillers when a matching row grounds a reified node. "
            "Incidence identifies coverage candidates, not the answer entities. A "
            "filler qualifies only when its represented role establishes the requested "
            "relationship; co-participation alone does not. A numeric object index "
            "selects only one stored slot."
        ),
    },
    "get_concept_usage_profile": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Usage profile: {concept_id}",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "evidence_kind": "relation_bearing",
        "external_surface": False,
        "target_concept_argument_name": "concept_id",
        "target_concept_source": "focal_concept",
        "target_concept_max_count": 3,
        "planner_hint": (
            "Use to gather grounded relation/text assertion counts before "
            "workflow-governed quality, description, or translation rumination. "
            "The workflow/profile supplies any non-trivial-use threshold."
        ),
    },
    "read_paper": {
        "salience": "medium",
        "category": "arxiv",
        "display_template": "Read: {arxiv_id}",
    },
    "context_search": {
        "salience": "medium",
        "category": "search",
        "display_template": "{count} results",
    },
    "qna_search": {
        "salience": "medium",
        "category": "search",
        "display_template": "Answer: {answer}",
    },
    "search_knowledge_base": {
        "salience": "medium",
        "category": "search",
        "display_template": "{count} KB matches",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "planner_hint": (
            "Use for represented-knowledge lookup over indexed content in the user's "
            "namespace, especially when answering questions about an entity and its "
            "related facts, artefacts, or relationships."
        ),
    },
    "task_get": {
        "salience": "medium",
        "category": "task",
        "display_template": "Task: {title}",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "task_list": {
        "salience": "medium",
        "category": "task",
        "display_template": "{count} tasks",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
        "planner_hint": (
            "Use for Von internal tasks and to-dos. This is not Jira issue search "
            "and should not be used for Jira issues or linked Jira tasks."
        ),
    },
    "task_search": {
        "salience": "medium",
        "category": "task",
        "display_template": "{count} tasks",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "task_create_subtask": {
        "salience": "medium",
        "category": "task",
        "display_template": "Created subtask: {title}",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "list_my_tasks": {
        "salience": "medium",
        "category": "task",
        "display_template": "{count} tasks",
        "dispatch_surface_family": "task",
        "evidence_surface_family": "task",
        "external_surface": False,
    },
    "extract_annotations": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{count} annotations",
    },
    "read_file_copy": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Read: {filename}",
    },
    "index_file_copy": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Indexed file copy",
    },
    "import_local_file_copy": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Imported local file",
    },
    "import_url_file_copy": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Imported URL file",
    },
    "generate_concept_description": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Generated description",
    },
    "rag_sync_text_relations": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Synced {count} to RAG",
    },
    "fetch_concept_content": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Loaded: {name}",
    },
    "concept_exists": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Exists: {exists}",
    },
    "get_tree": {
        "salience": "low",
        "category": "vontology",
        "display_template": "{count} nodes",
    },
    "get_text_relations_summary": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Summary",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "target_concept_argument_name": "concept_id",
        "target_concept_source": "focal_concept",
        "target_concept_max_count": 2,
    },
    "get_related_concepts": {
        "salience": "low",
        "category": "vontology",
        "display_template": "{count} related",
        "dispatch_surface_family": "knowledge_base",
        "evidence_surface_family": "knowledge_base",
        "external_surface": False,
        "target_concept_argument_name": "concept_id",
        "target_concept_source": "focal_concept",
        "target_concept_max_count": 2,
    },
    "get_predicate_extent": {
        "salience": "low",
        "category": "vontology",
        "display_template": "{count} extent",
    },
    "index_concept_text": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Indexed",
    },
    "jira_get_myself": {
        "salience": "low",
        "category": "jira",
        "display_template": "User: {name}",
    },
    "jira_get_auth_config": {
        "salience": "low",
        "category": "jira",
        "display_template": "Auth config",
    },
    "rag_get_item": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Item: {id}",
    },
    "rag_get_status": {
        "salience": "low",
        "category": "vontology",
        "display_template": "RAG status",
    },
    "rag_list_collections": {
        "salience": "low",
        "category": "vontology",
        "display_template": "{count} collections",
    },
    "rag_list_indexed": {
        "salience": "low",
        "category": "vontology",
        "display_template": "{count} indexed",
    },
    "check_placeholder_description": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Placeholder: {is_placeholder}",
    },
    "audit_concept_text_relations": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Audit complete",
    },
    "failure_case_intake_collect": {
        "salience": "low",
        "category": "workflow",
        "display_template": "Failure-case intake: {request_id}",
        "operation_category": "read",
        "evidence_role": "verification",
    },
    "failure_case_reference_resolve": {
        "salience": "low",
        "category": "workflow",
        "display_template": "Failure-case reference: {resolved_request_id}",
        "operation_category": "read",
        "evidence_role": "verification",
    },
    # NONE salience - internal, never show
    "get_context": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
    "get_client_capabilities": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
    "settings_get_public": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
    "chat_get_prompt_context": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
    "chat_get_applied_prompt_context": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
    "chat_introspect": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
}

_DEFAULT_VONTOLOGY_STDIO_EXPOSED_TOOL_NAMES = {
    "add_names",
    "add_relationship",
    "assign_task",
    "audit_concept_text_relations",
    "coding_agent_mcp_access_profile",
    "concept_exists",
    "context_search",
    "create_concepts",
    "create_task",
    "delete_concept",
    "delete_text_relation",
    "download_paper",
    "build_paper_recommendations",
    "record_paper_recommendation_feedback",
    "record_source_processing_marker",
    "extract_annotations",
    "extract_url",
    "fetch_concept",
    "fetch_concept_content",
    "finalise_cached_paper",
    "materialise_scholarly_representation_for_file_copy",
    "find_relations_with_argument",
    "get_concept_usage_profile",
    "get_predicate_incidence",
    "get_source_processing_marker",
    "find_concepts_by_name",
    "find_subconcepts",
    "get_concept_index_status",
    "get_context",
    "get_paper_metadata",
    "get_task",
    "get_text_relations",
    "get_text_relations_summary",
    "get_tree",
    "gmail_create_label",
    "gmail_get_attachment",
    "gmail_get_message",
    "gmail_send_message",
    "gmail_list_labels",
    "gmail_list_messages",
    "gmail_modify_labels",
    "jira_add_comment",
    "jira_add_attachment",
    "jira_create_issue",
    "jira_delete_issue_link",
    "jira_get_auth_config",
    "jira_get_bulk_operation_progress",
    "jira_get_comments",
    "jira_get_issue",
    "jira_get_project_issue_types",
    "jira_get_myself",
    "jira_get_transitions",
    "jira_link_issue",
    "jira_search",
    "jira_transition",
    "jira_move_issue",
    "jira_update_issue",
    "list_recent_screenshots",
    "list_my_tasks",
    "merge_concepts",
    "mongo_query_diagnostics_report",
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
    "skill_catalogue_list",
    "skill_catalogue_sync",
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
    "failure_case_intake_collect",
    "failure_case_reference_resolve",
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
    "chat_history_get_segments",
    "chat_history_get_debug_entry",
    "conversation_telemetry_get_locator",
    "conversation_list",
    "conversation_get",
    "conversation_manage",
    "testing_prepare_experiment_spec",
    "testing_prepare_meeting_invitation_spec",
    "testing_prepare_arxiv_paper_ingestion_fixture",
    "testing_verify_arxiv_paper_ingestion_result",
    "testing_cleanup_arxiv_paper_ingestion_artifacts",
    "turn_execution_list",
    "turn_execution_get",
    "turn_execution_get_diagnostics",
    "turn_execution_get_critic_bundle",
    "turn_execution_get_live_progress",
    "turn_execution_search_failures",
    "turn_execution_build_benchmark",
    "turn_execution_build_context_answering_benchmark",
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
    "workflow_list_use_episodes",
    "workflow_validate_candidate",
    "workflow_list_event_bindings",
    "workflow_list_execution_traces",
    "workflow_build_prediction_envelope",
    "workflow_list_instances",
    "workflow_list_schedules",
    "workflow_mcp_health_check",
    "workflow_concept_parity_audit",
    "workflow_materialisation_diagnostics",
    "workflow_resume_instance",
    "workflow_retry_instance",
    "workflow_set_event_binding_enabled",
    "workflow_set_schedule_enabled",
    "workflow_trigger_schedule",
}

_DEFAULT_VONRAG_STDIO_EXPOSED_TOOL_NAMES = {
    "get_related_concepts",
    "index_concept_text",
    "rag_get_item",
    "rag_get_status",
    "rag_list_collections",
    "rag_list_indexed",
    "rag_sync_text_relations",
    "search_concept_descriptions",
    "search_knowledge_base",
}

_DEFAULT_JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES = {
    "jira_add_comment",
    "jira_get_comments",
    "jira_get_issue",
    "jira_get_transitions",
    "jira_search",
    "jira_transition",
}

_DEFAULT_WRITE_TOOL_NAMES = {
    "add_issue_comment",
    "add_names",
    "add_relationship",
    "assign_copilot_to_issue",
    "assign_task",
    "build_paper_recommendations",
    "create_branch",
    "create_concepts",
    "create_or_update_file",
    "create_pull_request",
    "create_repository",
    "create_task",
    "conversation_manage",
    "delete_concept",
    "delete_file",
    "delete_text_relation",
    "download_paper",
    "episode_critique_build_benchmark",
    "finalise_cached_paper",
    "materialise_scholarly_representation_for_file_copy",
    "import_url_file_copy",
    "gmail_create_label",
    "gmail_import_attachment",
    "gmail_modify_labels",
    "gmail_send_message",
    "issue_write",
    "jira_add_attachment",
    "jira_add_comment",
    "jira_create_issue",
    "jira_move_issue",
    "jira_link_issue",
    "jira_transition",
    "jira_update_issue",
    "merge_concepts",
    "merge_pull_request",
    "mcp__github__add_issue_comment",
    "mcp__github__create_pull_request",
    "mcp__github__update_pull_request",
    "pull_request_review_write",
    "push_files",
    "record_source_processing_marker",
    "remove_relationship",
    "remove_relationships_bulk",
    "undo_relationship_removal",
    "sub_issue_write",
    "task_add_attachment",
    "task_add_comment",
    "task_add_worklog",
    "task_assign",
    "task_bulk_update",
    "task_create",
    "task_create_subtask",
    "task_delete",
    "task_import_jira_issues",
    "task_link",
    "task_set_parent",
    "task_transition",
    "task_unassign",
    "task_unlink",
    "task_update_fields",
    "task_update_status",
    "upsert_renderer_profile",
    "upsert_singleton_text_relation",
    "upsert_text_relation",
    "update_concept",
    "update_pull_request",
    "update_pull_request_branch",
    "update_task_status",
    "update_text_relation",
    "workflow_bind_event",
    "workflow_create_instance",
    "workflow_create_schedule",
    "workflow_delete_event_binding",
    "workflow_delete_schedule",
    "workflow_set_event_binding_enabled",
    "workflow_set_schedule_enabled",
}

_DEFAULT_VERIFICATION_READ_TOOL_NAMES = {
    "count",
    "fetch_concept",
    "fetch_concept_content",
    "find_concepts_by_name",
    "find_relations_with_argument",
    "find_subconcepts",
    "get_context",
    "get_file_contents",
    "get_paper_metadata",
    "get_task",
    "get_team_members",
    "get_teams",
    "get_text_relations",
    "get_text_relations_summary",
    "get_tree",
    "issue_read",
    "jira_get_comments",
    "jira_get_issue",
    "jira_get_bulk_operation_progress",
    "jira_get_project_issue_types",
    "jira_get_myself",
    "jira_get_transitions",
    "jira_search",
    "list_uncertain_relationship_assertions",
    "list_my_tasks",
    "list_pull_requests",
    "resolve_concept_by_name",
    "search_code",
    "search_concepts",
    "search_issues",
    "search_pull_requests",
    "search_repositories",
    "search_users",
    "search_knowledge_base",
    "task_get",
    "task_get_history",
    "task_get_transitions",
    "task_list",
    "task_list_attachments",
    "task_list_comments",
    "task_list_worklog",
    "task_search",
    "vontology_concept_search",
    "workflow_get_instance",
    "workflow_get_schedule",
    "workflow_list_definitions",
    "workflow_list_event_bindings",
    "workflow_list_instances",
    "workflow_list_schedules",
}

_DEFAULT_SEARCH_EVIDENCE_TOOL_NAMES = {
    "context_search",
    "find_concepts_by_name",
    "find_relations_with_argument",
    "get_concept_usage_profile",
    "get_predicate_incidence",
    "get_related_concepts",
    "get_text_relations_summary",
    "jira_search",
    "qna_search",
    "search_arxiv",
    "search_concept_descriptions",
    "search_concepts",
    "search_knowledge_base",
    "search_web",
    "vontology_concept_search",
}


def _set_default_tool_metadata_fields(tool_name: str, **fields: Any) -> None:
    entry = _DEFAULT_TOOL_METADATA.setdefault(tool_name, {})
    for key, value in fields.items():
        entry.setdefault(key, value)


for _tool_name in _DEFAULT_VONTOLOGY_STDIO_EXPOSED_TOOL_NAMES:
    _set_default_tool_metadata_fields(
        _tool_name,
        expose_in_vontology_stdio=True,
        expose_in_manifest=True,
    )

for _tool_name in _DEFAULT_VONRAG_STDIO_EXPOSED_TOOL_NAMES:
    _set_default_tool_metadata_fields(
        _tool_name,
        expose_in_vonrag_stdio=True,
    )

for _tool_name in _DEFAULT_JIRA_FAMILY_SERVER_EXPOSED_TOOL_NAMES:
    _set_default_tool_metadata_fields(
        _tool_name,
        expose_in_jira_family_server=True,
    )

for _tool_name in _DEFAULT_WRITE_TOOL_NAMES:
    _set_default_tool_metadata_fields(
        _tool_name,
        operation_category="write",
    )

for _tool_name in _DEFAULT_VERIFICATION_READ_TOOL_NAMES:
    _set_default_tool_metadata_fields(
        _tool_name,
        operation_category="read",
        evidence_role="verification",
    )

for _tool_name in _DEFAULT_SEARCH_EVIDENCE_TOOL_NAMES:
    _set_default_tool_metadata_fields(
        _tool_name,
        operation_category="read",
        evidence_role="search",
    )


def _load_from_vontology() -> dict[str, ToolMetadata]:
    """Load tool metadata from Vontology #V#mcp_tool instances.

    Returns a dict mapping tool_name to ToolMetadata.
    Falls back gracefully if Vontology is unavailable.
    """
    result: dict[str, ToolMetadata] = {}

    try:
        from ..db.repositories.concepts_repository import ConceptsRepository
        from .text_value_service import get_preferred_texts_for_concepts

        # Find all instances of #V#mcp_tool
        cursor = ConceptsRepository.find(
            {"relationships.is_an_instance_of": "#V#mcp_tool"},
            {
                "concept_id": 1,
                "attributes": 1,
            },
        )
        docs = [doc for doc in cursor if isinstance(doc, dict)]
        concept_ids = [
            doc["concept_id"]
            for doc in docs
            if isinstance(doc.get("concept_id"), str) and doc.get("concept_id")
        ]
        preferred_text_by_concept = get_preferred_texts_for_concepts(
            concept_ids,
            predicate_precedence=(
                ("hasDescription", "#V#hasDescription"),
                ("hasContent", "#V#hasContent"),
            ),
            preferred_languages=("en-NZ", "en"),
            limit_per_concept=10,
        )

        for doc in docs:
            attrs = doc.get("attributes") or {}
            tool_name = attrs.get("mcp_tool_name")
            if not tool_name:
                continue

            salience = attrs.get("user_salience", "medium")
            display_template = attrs.get("display_template")
            description = None
            if isinstance(doc.get("concept_id"), str):
                best = preferred_text_by_concept.get(doc["concept_id"])
                if isinstance(best, dict):
                    text_value = best.get("text")
                    if isinstance(text_value, str) and text_value.strip():
                        description = text_value.strip()
            category = attrs.get("category")

            result[tool_name] = ToolMetadata(
                tool_name=tool_name,
                concept_id=doc.get("concept_id"),
                salience=salience,
                display_template=display_template,
                description=description,
                category=category,
                planner_hint=attrs.get("planner_hint"),
                dispatch_surface_family=attrs.get("dispatch_surface_family"),
                evidence_surface_family=attrs.get("evidence_surface_family"),
                external_surface=attrs.get("external_surface"),
                operation_category=attrs.get("operation_category"),
                evidence_role=attrs.get("evidence_role"),
                evidence_kind=attrs.get("evidence_kind"),
                required_tool_operation_class=attrs.get(
                    "required_tool_operation_class"
                ),
                required_tool_target_closure=_coerce_optional_bool(
                    attrs.get("required_tool_target_closure")
                    if attrs.get("required_tool_target_closure") is not None
                    else attrs.get("target_closure_required")
                ),
                required_tool_target_argument_names=_coerce_string_tuple(
                    attrs.get("required_tool_target_argument_names")
                    or attrs.get("required_tool_target_argument_name")
                ),
                required_tool_target_payload_field_names=_coerce_string_tuple(
                    attrs.get("required_tool_target_payload_field_names")
                    or attrs.get("required_tool_target_payload_field_name")
                ),
                target_concept_argument_name=attrs.get("target_concept_argument_name"),
                target_concept_source=attrs.get("target_concept_source"),
                target_concept_max_count=_coerce_optional_positive_int(
                    attrs.get("target_concept_max_count")
                ),
                default_payload=_coerce_optional_mapping(attrs.get("default_payload")),
                identifier_argument_name=attrs.get("identifier_argument_name"),
                identifier_pattern=attrs.get("identifier_pattern"),
                identifier_source=attrs.get("identifier_source"),
                identifier_max_count=_coerce_optional_positive_int(
                    attrs.get("identifier_max_count")
                ),
                identifier_normalise=attrs.get("identifier_normalise"),
                identifier_default_payload=_coerce_optional_mapping(
                    attrs.get("identifier_default_payload")
                ),
                expose_in_vontology_stdio=attrs.get("expose_in_vontology_stdio"),
                expose_in_vonrag_stdio=attrs.get("expose_in_vonrag_stdio"),
                expose_in_manifest=attrs.get("expose_in_manifest"),
                expose_in_jira_family_server=attrs.get("expose_in_jira_family_server"),
            )

        logger.debug(f"Loaded {len(result)} tool metadata entries from Vontology")

    except Exception as e:
        logger.warning(f"Failed to load tool metadata from Vontology: {e}")

    return result


def _refresh_cache_if_needed() -> None:
    """Refresh the cache if TTL has expired."""
    global _tool_metadata_cache, _cache_timestamp, _cache_loaded

    current_time = time.time()
    if _cache_loaded and (current_time - _cache_timestamp) < _CACHE_TTL_SECONDS:
        return

    with _cache_lock:
        # Double-check after acquiring lock
        if _cache_loaded and (time.time() - _cache_timestamp) < _CACHE_TTL_SECONDS:
            return

        # Load from Vontology
        vontology_metadata = _load_from_vontology()

        # Merge with defaults (Vontology takes precedence)
        new_cache: dict[str, ToolMetadata] = {}

        # Start with defaults
        for tool_name, defaults in _DEFAULT_TOOL_METADATA.items():
            new_cache[tool_name] = ToolMetadata(
                tool_name=tool_name,
                salience=defaults.get("salience", "medium"),
                display_template=defaults.get("display_template"),
                description=defaults.get("description"),
                category=defaults.get("category"),
                planner_hint=defaults.get("planner_hint"),
                dispatch_surface_family=defaults.get("dispatch_surface_family"),
                evidence_surface_family=defaults.get("evidence_surface_family"),
                external_surface=defaults.get("external_surface"),
                operation_category=defaults.get("operation_category"),
                evidence_role=defaults.get("evidence_role"),
                evidence_kind=defaults.get("evidence_kind"),
                required_tool_operation_class=defaults.get(
                    "required_tool_operation_class"
                ),
                required_tool_target_closure=_coerce_optional_bool(
                    defaults.get("required_tool_target_closure")
                ),
                required_tool_target_argument_names=_coerce_string_tuple(
                    defaults.get("required_tool_target_argument_names")
                    or defaults.get("required_tool_target_argument_name")
                ),
                required_tool_target_payload_field_names=_coerce_string_tuple(
                    defaults.get("required_tool_target_payload_field_names")
                    or defaults.get("required_tool_target_payload_field_name")
                ),
                target_concept_argument_name=defaults.get(
                    "target_concept_argument_name"
                ),
                target_concept_source=defaults.get("target_concept_source"),
                target_concept_max_count=_coerce_optional_positive_int(
                    defaults.get("target_concept_max_count")
                ),
                default_payload=_coerce_optional_mapping(
                    defaults.get("default_payload")
                ),
                identifier_argument_name=defaults.get("identifier_argument_name"),
                identifier_pattern=defaults.get("identifier_pattern"),
                identifier_source=defaults.get("identifier_source"),
                identifier_max_count=_coerce_optional_positive_int(
                    defaults.get("identifier_max_count")
                ),
                identifier_normalise=defaults.get("identifier_normalise"),
                identifier_default_payload=_coerce_optional_mapping(
                    defaults.get("identifier_default_payload")
                ),
                expose_in_vontology_stdio=defaults.get("expose_in_vontology_stdio"),
                expose_in_vonrag_stdio=defaults.get("expose_in_vonrag_stdio"),
                expose_in_manifest=defaults.get("expose_in_manifest"),
                expose_in_jira_family_server=defaults.get(
                    "expose_in_jira_family_server"
                ),
            )

        # Override with Vontology data while preserving useful default hints when
        # the Vontology representation has not been extended yet.
        for tool_name, metadata in vontology_metadata.items():
            default_metadata = new_cache.get(tool_name)
            if isinstance(default_metadata, ToolMetadata):
                new_cache[tool_name] = ToolMetadata(
                    tool_name=tool_name,
                    concept_id=metadata.concept_id or default_metadata.concept_id,
                    salience=metadata.salience or default_metadata.salience,
                    display_template=(
                        metadata.display_template or default_metadata.display_template
                    ),
                    description=metadata.description or default_metadata.description,
                    category=metadata.category or default_metadata.category,
                    planner_hint=metadata.planner_hint or default_metadata.planner_hint,
                    dispatch_surface_family=(
                        metadata.dispatch_surface_family
                        or default_metadata.dispatch_surface_family
                    ),
                    evidence_surface_family=(
                        metadata.evidence_surface_family
                        or default_metadata.evidence_surface_family
                    ),
                    external_surface=(
                        metadata.external_surface
                        if metadata.external_surface is not None
                        else default_metadata.external_surface
                    ),
                    operation_category=(
                        metadata.operation_category
                        or default_metadata.operation_category
                    ),
                    evidence_role=metadata.evidence_role
                    or default_metadata.evidence_role,
                    evidence_kind=metadata.evidence_kind
                    or default_metadata.evidence_kind,
                    required_tool_operation_class=(
                        metadata.required_tool_operation_class
                        or default_metadata.required_tool_operation_class
                    ),
                    required_tool_target_closure=(
                        metadata.required_tool_target_closure
                        if metadata.required_tool_target_closure is not None
                        else default_metadata.required_tool_target_closure
                    ),
                    required_tool_target_argument_names=(
                        metadata.required_tool_target_argument_names
                        or default_metadata.required_tool_target_argument_names
                    ),
                    required_tool_target_payload_field_names=(
                        metadata.required_tool_target_payload_field_names
                        or default_metadata.required_tool_target_payload_field_names
                    ),
                    target_concept_argument_name=(
                        metadata.target_concept_argument_name
                        or default_metadata.target_concept_argument_name
                    ),
                    target_concept_source=(
                        metadata.target_concept_source
                        or default_metadata.target_concept_source
                    ),
                    target_concept_max_count=(
                        metadata.target_concept_max_count
                        if metadata.target_concept_max_count is not None
                        else default_metadata.target_concept_max_count
                    ),
                    default_payload=(
                        _coerce_optional_mapping(metadata.default_payload)
                        or _coerce_optional_mapping(default_metadata.default_payload)
                    ),
                    identifier_argument_name=(
                        metadata.identifier_argument_name
                        or default_metadata.identifier_argument_name
                    ),
                    identifier_pattern=(
                        metadata.identifier_pattern
                        or default_metadata.identifier_pattern
                    ),
                    identifier_source=(
                        metadata.identifier_source or default_metadata.identifier_source
                    ),
                    identifier_max_count=(
                        metadata.identifier_max_count
                        if metadata.identifier_max_count is not None
                        else default_metadata.identifier_max_count
                    ),
                    identifier_normalise=(
                        metadata.identifier_normalise
                        or default_metadata.identifier_normalise
                    ),
                    identifier_default_payload=(
                        _coerce_optional_mapping(metadata.identifier_default_payload)
                        or _coerce_optional_mapping(
                            default_metadata.identifier_default_payload
                        )
                    ),
                    expose_in_vontology_stdio=(
                        metadata.expose_in_vontology_stdio
                        if metadata.expose_in_vontology_stdio is not None
                        else default_metadata.expose_in_vontology_stdio
                    ),
                    expose_in_vonrag_stdio=(
                        metadata.expose_in_vonrag_stdio
                        if metadata.expose_in_vonrag_stdio is not None
                        else default_metadata.expose_in_vonrag_stdio
                    ),
                    expose_in_manifest=(
                        metadata.expose_in_manifest
                        if metadata.expose_in_manifest is not None
                        else default_metadata.expose_in_manifest
                    ),
                    expose_in_jira_family_server=(
                        metadata.expose_in_jira_family_server
                        if metadata.expose_in_jira_family_server is not None
                        else default_metadata.expose_in_jira_family_server
                    ),
                )
            else:
                new_cache[tool_name] = metadata

        _tool_metadata_cache = new_cache
        _cache_timestamp = time.time()
        _cache_loaded = True


def get_tool_metadata(tool_name: str) -> ToolMetadata:
    """Get metadata for a tool, with caching and Vontology override.

    Args:
        tool_name: The MCP tool name (e.g., "create_concepts", "jira_search")

    Returns:
        ToolMetadata with salience and display template info
    """
    _refresh_cache_if_needed()

    if tool_name in _tool_metadata_cache:
        metadata = _tool_metadata_cache[tool_name]
        metadata_surface_name = preferred_tool_metadata_surface_name(tool_name)
        canonical_metadata = _tool_metadata_cache.get(metadata_surface_name)
        if (
            metadata_surface_name != tool_name
            and isinstance(canonical_metadata, ToolMetadata)
        ):
            inherited_fields = (
                "concept_id",
                "description",
                "planner_hint",
                "dispatch_surface_family",
                "evidence_surface_family",
                "external_surface",
                "operation_category",
                "evidence_role",
                "evidence_kind",
                "required_tool_operation_class",
                "required_tool_target_closure",
                "required_tool_target_argument_names",
                "required_tool_target_payload_field_names",
                "target_concept_argument_name",
                "target_concept_source",
                "target_concept_max_count",
                "default_payload",
                "identifier_argument_name",
                "identifier_pattern",
                "identifier_source",
                "identifier_max_count",
                "identifier_normalise",
                "identifier_default_payload",
            )
            inherited_values: dict[str, Any] = {}
            for field_name in inherited_fields:
                canonical_value = getattr(canonical_metadata, field_name)
                if canonical_value not in (None, (), {}):
                    inherited_values[field_name] = canonical_value
            if inherited_values:
                metadata = replace(metadata, **inherited_values)
        return metadata

    # Return default for unknown tools
    return ToolMetadata(
        tool_name=tool_name,
        salience="medium",
        display_template=None,
    )


def get_tool_salience(tool_name: str) -> str:
    """Get the salience level for a tool.

    Returns: "high", "medium", "low", or "none"
    """
    return get_tool_metadata(tool_name).salience


def is_tool_visible(tool_name: str) -> bool:
    """Check if a tool should be visible in progress displays."""
    return get_tool_metadata(tool_name).is_visible


def get_display_template(tool_name: str) -> str | None:
    """Get the display template for a tool result summary."""
    return get_tool_metadata(tool_name).display_template


def get_tool_description(
    tool_name: str, *, fallback_description: str | None = None
) -> str | None:
    """Get the best model-visible description for a registered tool.

    Represented descriptions remain authoritative when they carry useful
    capability semantics.  Legacy materialisation placeholders must not
    override the registered interface contract merely because they are
    non-empty.
    """
    metadata = get_tool_metadata(tool_name)
    description = str(metadata.description or "").strip()
    if is_usable_tool_capability_description(description, tool_name=tool_name):
        return description
    cleaned_fallback = str(fallback_description or "").strip()
    if is_usable_tool_capability_description(
        cleaned_fallback,
        tool_name=tool_name,
    ):
        return cleaned_fallback
    return None


def is_usable_tool_capability_description(
    description: str | None,
    *,
    tool_name: str | None = None,
) -> bool:
    """Return whether text tells a model something substantive about a tool.

    This deliberately recognises only structural failure modes.  It is not a
    semantic router and does not try to score prose style or prescribe when a
    capability must be selected.
    """

    cleaned = str(description or "").strip()
    if not cleaned:
        return False
    normalised = " ".join(cleaned.lower().split())
    if re.fullmatch(
        r"(?:built-in )?internal mcp metadata concept for [a-z0-9_.-]+\.?",
        normalised,
    ):
        return False
    if normalised.startswith(("tool metadata for ", "metadata concept for ")):
        return False
    cleaned_name = str(tool_name or "").strip().lower()
    if cleaned_name:
        generic_forms = {
            f"use {cleaned_name}.",
            f"execute {cleaned_name}.",
            f"run {cleaned_name}.",
        }
        if normalised in generic_forms:
            return False
        if normalised.startswith(
            (
                f"{cleaned_name} input:",
                f"{cleaned_name} output:",
            )
        ):
            return False
    return bool(re.search(r"[a-z]{3}", normalised))


def get_tool_planner_hint(tool_name: str) -> str | None:
    """Get a short planner-facing hint for tool selection."""
    return get_tool_metadata(tool_name).planner_hint


def _normalise_operation_category(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"read", "write"}:
        return lowered
    return None


def _normalise_evidence_role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"search", "verification"}:
        return lowered
    return None


def _normalise_evidence_kind(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().replace("-", "_")
    if lowered in {"relation_bearing", "inventory_only"}:
        return lowered
    return None


def _normalise_required_tool_operation_class(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower().replace("-", "_")
    aliases = {
        "search": "search_or_resolution_read",
        "search_read": "search_or_resolution_read",
        "resolution_read": "search_or_resolution_read",
        "read_search": "search_or_resolution_read",
        "verification": "verification_read",
        "read_verification": "verification_read",
        "write": "mutation_write",
        "mutation": "mutation_write",
        "workflow": "workflow_execute",
        "workflow_execution": "workflow_execute",
        "external": "external_side_effect",
    }
    canonical = aliases.get(lowered, lowered)
    if canonical in _REQUIRED_TOOL_OPERATION_CLASSES:
        return canonical
    return None


def get_tool_family(
    tool_name: str,
    *,
    fallback_family: str | None = None,
    allow_registry_fallback: bool = True,
) -> str:
    """Resolve the canonical tool family for planner/listing support.

    Authority order:
    1. Vontology-backed tool metadata category when it names an actual family
    2. Explicit caller-provided fallback family
    3. Canonical tool-contract registry family
    4. ``unknown`` when neither surface can classify the tool
    """

    metadata = get_tool_metadata(tool_name)
    category = str(metadata.category or "").strip().lower()
    if category and category not in _NON_FAMILY_TOOL_CATEGORIES:
        return category

    fallback = str(fallback_family or "").strip().lower()
    if fallback and fallback not in _NON_FAMILY_TOOL_CATEGORIES:
        return fallback

    contract = (
        _resolve_registry_contract(tool_name) if allow_registry_fallback else None
    )

    family = str(getattr(contract, "family", "") or "").strip().lower()
    if family:
        return family

    return "unknown"


def _normalise_dispatch_surface_family(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    family = value.strip().lower()
    return family or None


def _coerce_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _coerce_optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else None
    return None


def _coerce_string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values: Sequence[Any]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, Sequence) and not isinstance(
                parsed, (str, bytes, bytearray)
            ):
                values = parsed
            else:
                values = [item.strip() for item in stripped.split(",")]
        else:
            values = [item.strip() for item in stripped.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return ()

    normalised: list[str] = []
    seen: set[str] = set()
    for raw_item in values:
        item = str(raw_item or "").strip()
        if not item or any(character.isspace() for character in item):
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalised.append(item)
    return tuple(normalised)


def _coerce_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items() if str(key)}
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, Mapping):
            return {str(key): item for key, item in parsed.items() if str(key)}
    return None


def _normalise_target_concept_argument_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or any(character.isspace() for character in stripped):
        return None
    return stripped


def _normalise_target_concept_source(value: Any) -> str:
    if not isinstance(value, str):
        return "focal_concept"
    source = value.strip().lower()
    return source or "focal_concept"


def _normalise_identifier_pattern(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    pattern = value.strip()
    if not pattern or len(pattern) > 500:
        return None
    return pattern


def _normalise_identifier_source(value: Any) -> str:
    if not isinstance(value, str):
        return "user_text"
    source = value.strip().lower()
    return source or "user_text"


def _normalise_identifier_normalise(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalise = value.strip().lower()
    if normalise in {"upper", "lower"}:
        return normalise
    return None


def get_tool_target_concept_binding_metadata(
    tool_name: str,
) -> ToolTargetConceptBindingMetadata | None:
    """Resolve intrinsic focal-concept binding metadata for a tool."""

    metadata = get_tool_metadata(tool_name)
    argument_name = _normalise_target_concept_argument_name(
        metadata.target_concept_argument_name
    )
    if argument_name is None:
        return None
    return ToolTargetConceptBindingMetadata(
        target_concept_argument_name=argument_name,
        target_concept_source=_normalise_target_concept_source(
            metadata.target_concept_source
        ),
        target_concept_max_count=metadata.target_concept_max_count,
        default_payload=_coerce_optional_mapping(metadata.default_payload) or {},
    )


def get_tool_identifier_binding_metadata(
    tool_name: str,
) -> ToolIdentifierBindingMetadata | None:
    """Resolve exact identifier binding metadata for a required-tool retry."""

    metadata = get_tool_metadata(tool_name)
    argument_name = _normalise_target_concept_argument_name(
        metadata.identifier_argument_name
    )
    pattern = _normalise_identifier_pattern(metadata.identifier_pattern)
    if argument_name is None or pattern is None:
        return None
    return ToolIdentifierBindingMetadata(
        identifier_argument_name=argument_name,
        identifier_pattern=pattern,
        identifier_source=_normalise_identifier_source(metadata.identifier_source),
        identifier_max_count=metadata.identifier_max_count,
        identifier_normalise=_normalise_identifier_normalise(
            metadata.identifier_normalise
        ),
        default_payload=(
            _coerce_optional_mapping(metadata.identifier_default_payload) or {}
        ),
    )


def get_tool_required_obligation_metadata(
    tool_name: str,
    *,
    allow_registry_fallback: bool = True,
) -> ToolRequiredObligationMetadata:
    """Resolve represented semantics used by required-tool closure checks."""

    metadata = get_tool_metadata(tool_name)
    operation_class = _normalise_required_tool_operation_class(
        metadata.required_tool_operation_class
    )
    action_spec = (
        _resolve_workflow_action_spec(tool_name) if allow_registry_fallback else None
    )
    if operation_class is None and action_spec is not None:
        operation_class = _normalise_required_tool_operation_class(
            getattr(action_spec, "required_tool_operation_class", None)
            or getattr(action_spec, "operation_class", None)
            or getattr(action_spec, "obligation_operation_class", None)
        )
    if operation_class is None:
        operation_category = get_tool_operation_category(
            tool_name,
            allow_registry_fallback=allow_registry_fallback,
        )
        evidence_role = get_tool_evidence_role(
            tool_name,
            allow_registry_fallback=allow_registry_fallback,
        )
        if operation_category == "write":
            operation_class = "mutation_write"
        elif evidence_role == "search":
            operation_class = "search_or_resolution_read"
        elif evidence_role == "verification":
            operation_class = "verification_read"
        elif str(tool_name or "").strip().lower() == "workflow_execute":
            operation_class = "workflow_execute"

    target_argument_names = list(metadata.required_tool_target_argument_names)
    for argument_name in _coerce_string_tuple(
        getattr(action_spec, "required_tool_target_argument_names", None)
        if action_spec is not None
        else None
    ):
        if argument_name.lower() not in {
            item.lower() for item in target_argument_names
        }:
            target_argument_names.append(argument_name)
    target_payload_field_names = list(metadata.required_tool_target_payload_field_names)
    for field_name in _coerce_string_tuple(
        getattr(action_spec, "required_tool_target_payload_field_names", None)
        if action_spec is not None
        else None
    ):
        if field_name.lower() not in {
            item.lower() for item in target_payload_field_names
        }:
            target_payload_field_names.append(field_name)
    binding = get_tool_target_concept_binding_metadata(tool_name)
    if binding is not None:
        binding_name = binding.target_concept_argument_name
        if binding_name.lower() not in {item.lower() for item in target_argument_names}:
            target_argument_names.append(binding_name)
    target_closure_required = _coerce_optional_bool(
        metadata.required_tool_target_closure
    )
    if target_closure_required is None and action_spec is not None:
        target_closure_required = _coerce_optional_bool(
            getattr(action_spec, "required_tool_target_closure", None)
        )

    return ToolRequiredObligationMetadata(
        operation_class=operation_class,
        target_closure_required=target_closure_required,
        target_argument_names=tuple(target_argument_names),
        target_payload_field_names=tuple(target_payload_field_names),
    )


def get_tool_operation_category(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> str | None:
    """Resolve the canonical read/write category for a tool."""

    metadata = get_tool_metadata(tool_name)
    explicit_category = _normalise_operation_category(metadata.operation_category)
    if explicit_category is not None:
        return explicit_category

    fallback = _normalise_operation_category(fallback_operation_category)
    if fallback is not None:
        return fallback

    action_spec = (
        _resolve_workflow_action_spec(tool_name) if allow_registry_fallback else None
    )
    action_category = _normalise_operation_category(
        getattr(action_spec, "operation_category", None)
    )
    if action_category is not None:
        return action_category

    contract = (
        _resolve_registry_contract(tool_name) if allow_registry_fallback else None
    )
    return _normalise_operation_category(getattr(contract, "category", None))


def get_tool_evidence_role(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> str | None:
    """Resolve whether a tool is an evidence/search or verification surface."""

    metadata = get_tool_metadata(tool_name)
    explicit_role = _normalise_evidence_role(metadata.evidence_role)
    if explicit_role is not None:
        return explicit_role

    action_spec = (
        _resolve_workflow_action_spec(tool_name) if allow_registry_fallback else None
    )
    action_role = _normalise_evidence_role(getattr(action_spec, "evidence_role", None))
    if action_role is not None:
        return action_role

    operation_category = get_tool_operation_category(
        tool_name,
        fallback_operation_category=fallback_operation_category,
        allow_registry_fallback=allow_registry_fallback,
    )
    if operation_category == "write":
        return None
    return None


def get_tool_evidence_kind(tool_name: str) -> str | None:
    """Resolve the represented evidence-kind affordance for a tool."""

    return _normalise_evidence_kind(get_tool_metadata(tool_name).evidence_kind)


def is_tool_relation_bearing_evidence(tool_name: str) -> bool:
    """Return true when tool metadata marks the tool as relation-bearing evidence."""

    return get_tool_evidence_kind(tool_name) == "relation_bearing"


def is_tool_inventory_only_evidence(tool_name: str) -> bool:
    """Return true when tool metadata marks the tool as inventory-only evidence."""

    return get_tool_evidence_kind(tool_name) == "inventory_only"


def is_tool_write(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> bool:
    return (
        get_tool_operation_category(
            tool_name,
            fallback_operation_category=fallback_operation_category,
            allow_registry_fallback=allow_registry_fallback,
        )
        == "write"
    )


def is_tool_verification_read(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> bool:
    return (
        get_tool_evidence_role(
            tool_name,
            fallback_operation_category=fallback_operation_category,
            allow_registry_fallback=allow_registry_fallback,
        )
        == "verification"
    )


def is_tool_search_evidence(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> bool:
    return (
        get_tool_evidence_role(
            tool_name,
            fallback_operation_category=fallback_operation_category,
            allow_registry_fallback=allow_registry_fallback,
        )
        == "search"
    )


def is_tool_prompt_required_evidence(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> bool:
    return get_tool_evidence_role(
        tool_name,
        fallback_operation_category=fallback_operation_category,
        allow_registry_fallback=allow_registry_fallback,
    ) in {"search", "verification"}


def is_tool_prompt_required_mutation(
    tool_name: str,
    *,
    fallback_operation_category: str | None = None,
    allow_registry_fallback: bool = True,
) -> bool:
    return is_tool_write(
        tool_name,
        fallback_operation_category=fallback_operation_category,
        allow_registry_fallback=allow_registry_fallback,
    )


def _resolve_registry_contract(tool_name: str) -> Any | None:
    try:
        from src.backend.integrations.internal_mcp.tool_contract_registry import (
            get_canonical_tool_registry,
        )

        return get_canonical_tool_registry().get(tool_name)
    except Exception:
        return None


def _resolve_workflow_action_spec(tool_name: str) -> Any | None:
    try:
        from src.backend.workflows.durable.registry_factory import (
            get_shared_durable_action_registry,
        )

        return get_shared_durable_action_registry().resolve_action_spec(tool_name)
    except Exception:
        return None


def get_tool_surface_exposure_metadata(
    tool_name: str,
    *,
    allow_registry_fallback: bool = True,
) -> ToolSurfaceExposureMetadata:
    """Resolve the canonical per-surface exposure metadata for a tool."""

    metadata = get_tool_metadata(tool_name)
    explicit_values = (
        _coerce_optional_bool(metadata.expose_in_vontology_stdio),
        _coerce_optional_bool(metadata.expose_in_vonrag_stdio),
        _coerce_optional_bool(metadata.expose_in_manifest),
        _coerce_optional_bool(metadata.expose_in_jira_family_server),
    )
    if any(value is not None for value in explicit_values):
        return ToolSurfaceExposureMetadata(
            expose_in_vontology_stdio=bool(explicit_values[0]),
            expose_in_vonrag_stdio=bool(explicit_values[1]),
            expose_in_manifest=bool(explicit_values[2]),
            expose_in_jira_family_server=bool(explicit_values[3]),
        )

    contract = (
        _resolve_registry_contract(tool_name) if allow_registry_fallback else None
    )
    exposure = getattr(contract, "exposure", None)
    if exposure is not None:
        return ToolSurfaceExposureMetadata(
            expose_in_vontology_stdio=bool(
                getattr(exposure, "expose_in_vontology_stdio", False)
            ),
            expose_in_vonrag_stdio=bool(
                getattr(exposure, "expose_in_vonrag_stdio", False)
            ),
            expose_in_manifest=bool(getattr(exposure, "expose_in_manifest", False)),
            expose_in_jira_family_server=bool(
                getattr(exposure, "expose_in_jira_family_server", False)
            ),
        )

    return ToolSurfaceExposureMetadata()


def get_tool_dispatch_surface_metadata(
    tool_name: str,
) -> ToolDispatchSurfaceMetadata | None:
    """Resolve dispatch-preflight surface metadata for a tool.

    Authority order:
    1. Explicit Vontology/default tool metadata fields on the tool entry
    2. Canonical contract registry family
    3. Tool metadata category / contract category aliases
    """

    metadata = get_tool_metadata(tool_name)
    explicit_surface = _normalise_dispatch_surface_family(
        metadata.dispatch_surface_family
    )
    explicit_evidence = _normalise_dispatch_surface_family(
        metadata.evidence_surface_family
    )
    explicit_external = _coerce_optional_bool(metadata.external_surface)

    if explicit_surface:
        default_surface_metadata = _DEFAULT_DISPATCH_SURFACE_METADATA.get(
            explicit_surface
        )
        return ToolDispatchSurfaceMetadata(
            surface_family=explicit_surface,
            evidence_surface_family=(
                explicit_evidence
                or (
                    default_surface_metadata.evidence_surface_family
                    if isinstance(default_surface_metadata, ToolDispatchSurfaceMetadata)
                    else explicit_surface
                )
            ),
            external_surface=(
                explicit_external
                if explicit_external is not None
                else (
                    default_surface_metadata.external_surface
                    if isinstance(default_surface_metadata, ToolDispatchSurfaceMetadata)
                    else False
                )
            ),
        )

    contract = _resolve_registry_contract(tool_name)
    candidate_labels = (
        _normalise_dispatch_surface_family(getattr(contract, "family", None)),
        _normalise_dispatch_surface_family(metadata.category),
        _normalise_dispatch_surface_family(getattr(contract, "category", None)),
    )
    for label in candidate_labels:
        if label is None or label in _NON_FAMILY_TOOL_CATEGORIES:
            continue
        surface_metadata = _DEFAULT_DISPATCH_SURFACE_METADATA.get(label)
        if isinstance(surface_metadata, ToolDispatchSurfaceMetadata):
            return surface_metadata

    return None


def invalidate_cache() -> None:
    """Force cache refresh on next access."""
    global _cache_loaded, _cache_timestamp
    with _cache_lock:
        _cache_loaded = False
        _cache_timestamp = 0.0
    try:
        from src.backend.integrations.internal_mcp.tool_contract_registry import (
            invalidate_canonical_tool_registry,
        )

        invalidate_canonical_tool_registry()
    except Exception:
        pass


def get_all_tool_metadata() -> dict[str, ToolMetadata]:
    """Get all cached tool metadata (for debugging/admin)."""
    _refresh_cache_if_needed()
    return dict(_tool_metadata_cache)


def get_high_salience_tools() -> list[str]:
    """Get list of high-salience tool names."""
    _refresh_cache_if_needed()
    return [
        name for name, meta in _tool_metadata_cache.items() if meta.salience == "high"
    ]


def get_tools_by_category(category: str) -> list[str]:
    """Get tool names belonging to a specific category."""
    _refresh_cache_if_needed()
    return [
        name for name, meta in _tool_metadata_cache.items() if meta.category == category
    ]
