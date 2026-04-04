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

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Cache TTL in seconds (5 minutes default)
_CACHE_TTL_SECONDS = 300

# Lock for thread-safe cache access
_cache_lock = threading.Lock()

# In-memory cache
_tool_metadata_cache: dict[str, "ToolMetadata"] = {}
_cache_timestamp: float = 0.0
_cache_loaded: bool = False


@dataclass
class ToolMetadata:
    """Metadata for an MCP tool loaded from Vontology."""

    tool_name: str
    concept_id: str | None = None
    salience: str = "medium"  # high, medium, low, none
    display_template: str | None = None
    description: str | None = None
    category: str | None = None  # vontology, arxiv, gmail, jira, task, search, etc.

    @property
    def is_high_salience(self) -> bool:
        return self.salience == "high"

    @property
    def is_visible(self) -> bool:
        return self.salience != "none"


# Default metadata for tools not yet in Vontology (backward compatibility)
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
    },
    "task_update_status": {
        "salience": "high",
        "category": "task",
        "display_template": "Status → {status}",
    },
    "task_assign": {
        "salience": "high",
        "category": "task",
        "display_template": "Assigned to: {assignee}",
    },
    "task_delete": {
        "salience": "high",
        "category": "task",
        "display_template": "Deleted task: {task_id}",
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
    },
    "download_paper": {
        "salience": "high",
        "category": "arxiv",
        "display_template": "Downloaded: {arxiv_id}",
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
    # LinkedIn Data Dump tools
    "linkedin_list_exports": {
        "salience": "high",
        "category": "linkedin",
        "display_template": "{total_exports} exports",
    },
    "linkedin_list_files": {
        "salience": "high",
        "category": "linkedin",
        "display_template": "Files: {export_name}",
    },
    "linkedin_get_profile": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "Profile: {export_name}",
    },
    "linkedin_get_csv_data": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "CSV: {file_name}",
    },
    "linkedin_get_company_stats": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "Company stats: {export_name}",
    },
    "linkedin_get_messages": {
        "salience": "medium",
        "category": "linkedin",
        "display_template": "Messages: {export_name}",
    },
    # Web/search tools (HIGH salience)
    "search_web": {
        "salience": "high",
        "category": "search",
        "display_template": "{count} web results",
    },
    "extract_url": {
        "salience": "high",
        "category": "search",
        "display_template": "Extracted: {url}",
    },
    "resilient_extract_url": {
        "salience": "high",
        "category": "search",
        "display_template": "Extracted: {url}",
    },
    # MEDIUM salience - show tool name with brief info
    "jira_search": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Found {count} issues",
    },
    "jira_get_issue": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Issue: {key}",
    },
    "jira_get_transitions": {
        "salience": "medium",
        "category": "jira",
        "display_template": "Transitions: {key}",
    },
    "gmail_list_messages": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "{count} messages",
    },
    "gmail_get_message": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Message: {subject}",
    },
    "gmail_get_attachment": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Attachment: {filename}",
    },
    "gmail_list_labels": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "{count} labels",
    },
    "gmail_modify_labels": {
        "salience": "medium",
        "category": "gmail",
        "display_template": "Labels modified",
    },
    "upsert_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{action}: {predicate}",
    },
    "upsert_singleton_text_relation": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "{action}: {predicate}",
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
    "find_concepts_by_name": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Found: {names}",
    },
    "resolve_concept_by_name": {
        "salience": "medium",
        "category": "vontology",
        "display_template": "Resolved: {name}",
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
    },
    "task_get": {
        "salience": "medium",
        "category": "task",
        "display_template": "Task: {title}",
    },
    "task_list": {
        "salience": "medium",
        "category": "task",
        "display_template": "{count} tasks",
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
    # LOW salience - aggregate count only
    "fetch_concept": {
        "salience": "low",
        "category": "vontology",
        "display_template": "Fetched: {name}",
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
    },
    "get_related_concepts": {
        "salience": "low",
        "category": "vontology",
        "display_template": "{count} related",
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
    "chat_introspect": {
        "salience": "none",
        "category": "internal",
        "display_template": None,
    },
}


def _load_from_vontology() -> dict[str, ToolMetadata]:
    """Load tool metadata from Vontology #V#mcp_tool instances.

    Returns a dict mapping tool_name to ToolMetadata.
    Falls back gracefully if Vontology is unavailable.
    """
    result: dict[str, ToolMetadata] = {}

    try:
        from ..db.repositories.concepts_repository import ConceptsRepository
        from .text_value_service import get_preferred_text_for_concept

        # Find all instances of #V#mcp_tool
        cursor = ConceptsRepository.find(
            {"relationships.is_an_instance_of": "#V#mcp_tool"},
            {
                "concept_id": 1,
                "attributes": 1,
            },
        )

        for doc in cursor:
            attrs = doc.get("attributes") or {}
            tool_name = attrs.get("mcp_tool_name")
            if not tool_name:
                continue

            salience = attrs.get("user_salience", "medium")
            display_template = attrs.get("display_template")
            description = None
            if isinstance(doc.get("concept_id"), str):
                best = get_preferred_text_for_concept(
                    doc["concept_id"],
                    predicate_precedence=(
                        ("hasDescription", "#V#hasDescription"),
                        ("hasContent", "#V#hasContent"),
                    ),
                    preferred_languages=("en-NZ", "en"),
                    limit=10,
                )
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
                category=defaults.get("category"),
            )

        # Override with Vontology data
        new_cache.update(vontology_metadata)

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
        return _tool_metadata_cache[tool_name]

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


def invalidate_cache() -> None:
    """Force cache refresh on next access."""
    global _cache_loaded, _cache_timestamp
    with _cache_lock:
        _cache_loaded = False
        _cache_timestamp = 0.0


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
