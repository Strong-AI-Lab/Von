"""Vontology KR materialisation for Jira read-tool evidence contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import concept_service
from .relationship_write_service import add_relationship
from .tool_evidence_contract_vontology_service import (
    TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
    bootstrap_tool_evidence_contract_vocabulary,
    validate_tool_evidence_contract_vocabulary,
)
from .workflow_vontology_materialisation_helpers import (
    load_concept,
    normalise_relationship_targets,
    suspend_event_workflow_integration,
)

JIRA_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION = "jira_tool_evidence_contract.v1"
JIRA_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG = "JVNAUTOSCI-2535"
JIRA_TOOL_EVIDENCE_CONTRACT_MANAGED_BY = "jira_tool_evidence_contract_vontology_service"

JIRA_TOOL_EVIDENCE_CONTRACT_ID = "#V#jira_tool_evidence_contract_v1"
JIRA_ISSUE_ENTITY_TYPE_ID = "#V#jira_issue_result_entity_type"
JIRA_SEARCH_TOOL_ID = "#V#jira_search_tool"
JIRA_GET_ISSUE_TOOL_ID = "#V#jira_get_issue_tool"
JIRA_FINAL_ANSWER_VIEW_ID = "#V#jira_issue_final_answer_evidence_view"
JIRA_SEARCH_FINAL_ANSWER_VIEW_ID = "#V#jira_search_final_answer_evidence_view"

JIRA_ISSUES_COLLECTION_FIELD_ID = "#V#jira_issues_collection_field"
JIRA_KEY_FIELD_ID = "#V#jira_issue_key_field"
JIRA_SUMMARY_FIELD_ID = "#V#jira_issue_summary_field"
JIRA_STATUS_FIELD_ID = "#V#jira_issue_status_field"
JIRA_ASSIGNEE_FIELD_ID = "#V#jira_issue_assignee_field"
JIRA_ISSUE_TYPE_FIELD_ID = "#V#jira_issue_type_field"
JIRA_PARENT_FIELD_ID = "#V#jira_issue_parent_field"
JIRA_CREATED_FIELD_ID = "#V#jira_issue_created_field"
JIRA_UPDATED_FIELD_ID = "#V#jira_issue_updated_field"
JIRA_DESCRIPTION_FIELD_ID = "#V#jira_issue_description_field"
JIRA_TOTAL_FIELD_ID = "#V#jira_search_total_field"
JIRA_JQL_FIELD_ID = "#V#jira_search_jql_field"
JIRA_NEXT_PAGE_TOKEN_FIELD_ID = "#V#jira_search_next_page_token_field"
JIRA_IS_LAST_FIELD_ID = "#V#jira_search_is_last_field"

JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS: tuple[str, ...] = (
    JIRA_KEY_FIELD_ID,
    JIRA_SUMMARY_FIELD_ID,
    JIRA_STATUS_FIELD_ID,
)


@dataclass(frozen=True)
class JiraConceptSpec:
    concept_id: str
    name: str
    description: str
    parent_concept_ids: tuple[str, ...]
    category: str
    attributes: Mapping[str, Any] | None = None
    create_as_instance: bool = True


@dataclass(frozen=True)
class JiraRelationshipSpec:
    source_id: str
    predicate: str
    target_id: str


def _base_attributes(category: str) -> dict[str, Any]:
    return {
        "repo_seed_source_tag": JIRA_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "repo_seed_managed_by": JIRA_TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "schema_version": JIRA_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "tool_evidence_contract_id": JIRA_TOOL_EVIDENCE_CONTRACT_ID,
        "tool_evidence_contract_vocabulary_id": TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
        "tool_contract_category": category,
    }


def _concept(
    *,
    concept_id: str,
    name: str,
    description: str,
    parent_concept_ids: tuple[str, ...],
    category: str,
    attributes: Mapping[str, Any] | None = None,
) -> JiraConceptSpec:
    merged_attributes = _base_attributes(category)
    if attributes:
        merged_attributes.update(dict(attributes))
    return JiraConceptSpec(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=parent_concept_ids,
        category=category,
        attributes=merged_attributes,
    )


def _field(
    concept_id: str,
    name: str,
    description: str,
    *,
    field_key: str,
) -> JiraConceptSpec:
    return _concept(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=("#V#tool_result_field",),
        category="field",
        attributes={"field_key": field_key},
    )


def _wire_key(key: str) -> JiraConceptSpec:
    safe_key = key.replace(".", "_").replace("-", "_").lower()
    return _concept(
        concept_id=f"#V#jira_wire_key_{safe_key}",
        name=f"Jira wire key {key}",
        description=f"Wire key named {key} in Jira MCP tool payloads.",
        parent_concept_ids=("#V#tool_wire_key",),
        category="wire_key",
        attributes={"wire_key": key},
    )


def _payload_path(path_id: str, path: str) -> JiraConceptSpec:
    return _concept(
        concept_id=path_id,
        name=f"Jira payload path {path}",
        description=f"Payload path {path} in Jira MCP tool results.",
        parent_concept_ids=("#V#tool_payload_path",),
        category="payload_path",
        attributes={"payload_path": path},
    )


_CORE_CONCEPT_SPECS: tuple[JiraConceptSpec, ...] = (
    _concept(
        concept_id=JIRA_TOOL_EVIDENCE_CONTRACT_ID,
        name="Jira tool evidence contract v1",
        description="Contract binding Jira read tools, issue fields, and final-answer evidence views.",
        parent_concept_ids=("#V#tool_interface_contract",),
        category="contract",
    ),
    _concept(
        concept_id=JIRA_SEARCH_TOOL_ID,
        name="jira_search tool",
        description="Built-in internal MCP tool that searches Jira issues.",
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={
            "mcp_tool_name": "jira_search",
            "category": "jira",
            "dispatch_surface_family": "jira",
            "evidence_surface_family": "jira",
            "external_surface": True,
            "operation_category": "read",
            "evidence_role": "search",
        },
    ),
    _concept(
        concept_id=JIRA_GET_ISSUE_TOOL_ID,
        name="jira_get_issue tool",
        description="Built-in internal MCP tool that fetches one Jira issue by key.",
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={
            "mcp_tool_name": "jira_get_issue",
            "category": "jira",
            "dispatch_surface_family": "jira",
            "evidence_surface_family": "jira",
            "external_surface": True,
            "operation_category": "read",
            "evidence_role": "verification",
        },
    ),
    _concept(
        concept_id=JIRA_ISSUE_ENTITY_TYPE_ID,
        name="Jira issue result entity type",
        description="Entity type for Jira issue rows and detail payloads returned by Jira MCP read tools.",
        parent_concept_ids=("#V#tool_result_entity_type",),
        category="entity_type",
    ),
    _concept(
        concept_id=JIRA_FINAL_ANSWER_VIEW_ID,
        name="Jira issue final-answer evidence view",
        description="Evidence view requiring the issue fields needed to answer a direct Jira issue lookup.",
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
        name="Jira search final-answer evidence view",
        description="Evidence view preserving Jira search rows and counts for answer synthesis.",
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
)

_FIELD_CONCEPT_SPECS: tuple[JiraConceptSpec, ...] = (
    _field(
        JIRA_ISSUES_COLLECTION_FIELD_ID,
        "Jira issues collection field",
        "Collection field containing Jira issue rows.",
        field_key="issues",
    ),
    _field(
        JIRA_KEY_FIELD_ID,
        "Jira issue key field",
        "Stable Jira issue key.",
        field_key="key",
    ),
    _field(
        JIRA_SUMMARY_FIELD_ID,
        "Jira issue summary field",
        "Jira issue summary/title.",
        field_key="summary",
    ),
    _field(
        JIRA_STATUS_FIELD_ID,
        "Jira issue status field",
        "Jira issue workflow status name.",
        field_key="status",
    ),
    _field(
        JIRA_ASSIGNEE_FIELD_ID,
        "Jira issue assignee field",
        "Jira issue assignee display name.",
        field_key="assignee",
    ),
    _field(
        JIRA_ISSUE_TYPE_FIELD_ID,
        "Jira issue type field",
        "Jira issue type name.",
        field_key="issue_type",
    ),
    _field(
        JIRA_PARENT_FIELD_ID,
        "Jira issue parent field",
        "Jira parent issue key and summary.",
        field_key="parent",
    ),
    _field(
        JIRA_CREATED_FIELD_ID,
        "Jira issue created field",
        "Jira created timestamp.",
        field_key="created",
    ),
    _field(
        JIRA_UPDATED_FIELD_ID,
        "Jira issue updated field",
        "Jira updated timestamp.",
        field_key="updated",
    ),
    _field(
        JIRA_DESCRIPTION_FIELD_ID,
        "Jira issue description field",
        "Jira issue description or acceptance context.",
        field_key="description",
    ),
    _field(
        JIRA_TOTAL_FIELD_ID,
        "Jira search total field",
        "Total Jira issue count returned by search.",
        field_key="total",
    ),
    _field(
        JIRA_JQL_FIELD_ID,
        "Jira search JQL field",
        "JQL query used for a Jira search.",
        field_key="jql",
    ),
    _field(
        JIRA_NEXT_PAGE_TOKEN_FIELD_ID,
        "Jira search next-page token field",
        "Opaque continuation token for the next Jira search page.",
        field_key="next_page_token",
    ),
    _field(
        JIRA_IS_LAST_FIELD_ID,
        "Jira search final-page field",
        "Whether Jira reports that the current search page is the final page.",
        field_key="is_last",
    ),
)

_WIRE_KEYS = (
    "issues",
    "key",
    "summary",
    "status",
    "name",
    "assignee",
    "displayName",
    "issuetype",
    "issue_type",
    "parent",
    "created",
    "updated",
    "description",
    "total",
    "jql",
    "nextPageToken",
    "next_page_token",
    "isLast",
    "is_last",
)

_WIRE_KEY_CONCEPT_SPECS: tuple[JiraConceptSpec, ...] = tuple(
    _wire_key(key) for key in _WIRE_KEYS
)

_PAYLOAD_PATH_SPECS: tuple[JiraConceptSpec, ...] = (
    _payload_path("#V#jira_payload_path_issues", "issues"),
    _payload_path("#V#jira_payload_path_key", "key"),
    _payload_path("#V#jira_payload_path_fields_summary", "fields.summary"),
    _payload_path("#V#jira_payload_path_fields_status_name", "fields.status.name"),
    _payload_path(
        "#V#jira_payload_path_fields_assignee_display_name",
        "fields.assignee.displayName",
    ),
    _payload_path(
        "#V#jira_payload_path_fields_issuetype_name", "fields.issuetype.name"
    ),
    _payload_path("#V#jira_payload_path_fields_parent", "fields.parent"),
    _payload_path("#V#jira_payload_path_fields_parent_key", "fields.parent.key"),
    _payload_path("#V#jira_payload_path_fields_created", "fields.created"),
    _payload_path("#V#jira_payload_path_fields_updated", "fields.updated"),
    _payload_path("#V#jira_payload_path_fields_description", "fields.description"),
    _payload_path("#V#jira_payload_path_issues_key", "issues[].key"),
    _payload_path(
        "#V#jira_payload_path_issues_fields_summary", "issues[].fields.summary"
    ),
    _payload_path(
        "#V#jira_payload_path_issues_fields_status_name", "issues[].fields.status.name"
    ),
    _payload_path(
        "#V#jira_payload_path_issues_fields_assignee_display_name",
        "issues[].fields.assignee.displayName",
    ),
    _payload_path(
        "#V#jira_payload_path_issues_fields_issuetype_name",
        "issues[].fields.issuetype.name",
    ),
    _payload_path(
        "#V#jira_payload_path_issues_fields_parent", "issues[].fields.parent"
    ),
    _payload_path(
        "#V#jira_payload_path_issues_fields_created", "issues[].fields.created"
    ),
    _payload_path(
        "#V#jira_payload_path_issues_fields_updated", "issues[].fields.updated"
    ),
    _payload_path("#V#jira_payload_path_next_page_token", "nextPageToken"),
    _payload_path("#V#jira_payload_path_is_last", "isLast"),
)

JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS: tuple[JiraConceptSpec, ...] = (
    *_CORE_CONCEPT_SPECS,
    *_FIELD_CONCEPT_SPECS,
    *_WIRE_KEY_CONCEPT_SPECS,
    *_PAYLOAD_PATH_SPECS,
)


def _relationship(
    source_id: str, predicate: str, target_id: str
) -> JiraRelationshipSpec:
    return JiraRelationshipSpec(
        source_id=source_id, predicate=predicate, target_id=target_id
    )


def _wire_key_id(key: str) -> str:
    safe_key = key.replace(".", "_").replace("-", "_").lower()
    return f"#V#jira_wire_key_{safe_key}"


def _field_alias_relationships() -> tuple[JiraRelationshipSpec, ...]:
    alias_map = {
        JIRA_ISSUES_COLLECTION_FIELD_ID: ("issues",),
        JIRA_KEY_FIELD_ID: ("key",),
        JIRA_SUMMARY_FIELD_ID: ("summary",),
        JIRA_STATUS_FIELD_ID: ("status", "name"),
        JIRA_ASSIGNEE_FIELD_ID: ("assignee", "displayName"),
        JIRA_ISSUE_TYPE_FIELD_ID: ("issuetype", "issue_type"),
        JIRA_PARENT_FIELD_ID: ("parent",),
        JIRA_CREATED_FIELD_ID: ("created",),
        JIRA_UPDATED_FIELD_ID: ("updated",),
        JIRA_DESCRIPTION_FIELD_ID: ("description",),
        JIRA_TOTAL_FIELD_ID: ("total",),
        JIRA_JQL_FIELD_ID: ("jql",),
        JIRA_NEXT_PAGE_TOKEN_FIELD_ID: ("nextPageToken", "next_page_token"),
        JIRA_IS_LAST_FIELD_ID: ("isLast", "is_last"),
    }
    return tuple(
        _relationship(field_id, "#V#field_has_wire_alias", _wire_key_id(alias))
        for field_id, aliases in alias_map.items()
        for alias in aliases
    )


def _field_payload_path_relationships() -> tuple[JiraRelationshipSpec, ...]:
    path_map = {
        JIRA_ISSUES_COLLECTION_FIELD_ID: ("#V#jira_payload_path_issues",),
        JIRA_KEY_FIELD_ID: (
            "#V#jira_payload_path_key",
            "#V#jira_payload_path_issues_key",
        ),
        JIRA_SUMMARY_FIELD_ID: (
            "#V#jira_payload_path_fields_summary",
            "#V#jira_payload_path_issues_fields_summary",
        ),
        JIRA_STATUS_FIELD_ID: (
            "#V#jira_payload_path_fields_status_name",
            "#V#jira_payload_path_issues_fields_status_name",
        ),
        JIRA_ASSIGNEE_FIELD_ID: (
            "#V#jira_payload_path_fields_assignee_display_name",
            "#V#jira_payload_path_issues_fields_assignee_display_name",
        ),
        JIRA_ISSUE_TYPE_FIELD_ID: (
            "#V#jira_payload_path_fields_issuetype_name",
            "#V#jira_payload_path_issues_fields_issuetype_name",
        ),
        JIRA_PARENT_FIELD_ID: (
            "#V#jira_payload_path_fields_parent",
            "#V#jira_payload_path_fields_parent_key",
            "#V#jira_payload_path_issues_fields_parent",
        ),
        JIRA_CREATED_FIELD_ID: (
            "#V#jira_payload_path_fields_created",
            "#V#jira_payload_path_issues_fields_created",
        ),
        JIRA_UPDATED_FIELD_ID: (
            "#V#jira_payload_path_fields_updated",
            "#V#jira_payload_path_issues_fields_updated",
        ),
        JIRA_DESCRIPTION_FIELD_ID: ("#V#jira_payload_path_fields_description",),
        JIRA_NEXT_PAGE_TOKEN_FIELD_ID: ("#V#jira_payload_path_next_page_token",),
        JIRA_IS_LAST_FIELD_ID: ("#V#jira_payload_path_is_last",),
    }
    return tuple(
        _relationship(field_id, "#V#field_extracts_from_payload_path", path_id)
        for field_id, path_ids in path_map.items()
        for path_id in path_ids
    )


def _field_role_relationships() -> tuple[JiraRelationshipSpec, ...]:
    answer_fields = (
        JIRA_KEY_FIELD_ID,
        JIRA_SUMMARY_FIELD_ID,
        JIRA_STATUS_FIELD_ID,
        JIRA_ASSIGNEE_FIELD_ID,
        JIRA_ISSUE_TYPE_FIELD_ID,
        JIRA_PARENT_FIELD_ID,
        JIRA_CREATED_FIELD_ID,
        JIRA_UPDATED_FIELD_ID,
        JIRA_DESCRIPTION_FIELD_ID,
        JIRA_TOTAL_FIELD_ID,
        JIRA_JQL_FIELD_ID,
        JIRA_NEXT_PAGE_TOKEN_FIELD_ID,
        JIRA_IS_LAST_FIELD_ID,
    )
    relationships = [
        _relationship(
            JIRA_ISSUES_COLLECTION_FIELD_ID,
            "#V#field_has_role",
            "#V#tool_field_role_collection_membership",
        ),
        _relationship(
            JIRA_KEY_FIELD_ID,
            "#V#field_has_role",
            "#V#tool_field_role_entity_identifier",
        ),
        _relationship(
            JIRA_KEY_FIELD_ID, "#V#field_has_role", "#V#tool_field_role_display_label"
        ),
        _relationship(
            JIRA_SUMMARY_FIELD_ID,
            "#V#field_has_role",
            "#V#tool_field_role_display_label",
        ),
    ]
    relationships.extend(
        _relationship(
            field_id, "#V#field_has_role", "#V#tool_field_role_answer_evidence"
        )
        for field_id in answer_fields
    )
    return tuple(relationships)


def _field_policy_relationships() -> tuple[JiraRelationshipSpec, ...]:
    return tuple(
        _relationship(
            field_id,
            "#V#field_has_completion_policy",
            "#V#tool_field_completion_required_before_answer",
        )
        for field_id in JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS
    )


def _tool_field_relationships() -> tuple[JiraRelationshipSpec, ...]:
    search_outputs = (
        JIRA_ISSUES_COLLECTION_FIELD_ID,
        JIRA_KEY_FIELD_ID,
        JIRA_SUMMARY_FIELD_ID,
        JIRA_STATUS_FIELD_ID,
        JIRA_ASSIGNEE_FIELD_ID,
        JIRA_ISSUE_TYPE_FIELD_ID,
        JIRA_PARENT_FIELD_ID,
        JIRA_CREATED_FIELD_ID,
        JIRA_UPDATED_FIELD_ID,
        JIRA_TOTAL_FIELD_ID,
        JIRA_JQL_FIELD_ID,
        JIRA_NEXT_PAGE_TOKEN_FIELD_ID,
        JIRA_IS_LAST_FIELD_ID,
    )
    detail_outputs = (
        JIRA_KEY_FIELD_ID,
        JIRA_SUMMARY_FIELD_ID,
        JIRA_STATUS_FIELD_ID,
        JIRA_ASSIGNEE_FIELD_ID,
        JIRA_ISSUE_TYPE_FIELD_ID,
        JIRA_PARENT_FIELD_ID,
        JIRA_CREATED_FIELD_ID,
        JIRA_UPDATED_FIELD_ID,
        JIRA_DESCRIPTION_FIELD_ID,
    )
    relationships: list[JiraRelationshipSpec] = []
    relationships.extend(
        _relationship(JIRA_SEARCH_TOOL_ID, "#V#tool_has_output_field", field_id)
        for field_id in search_outputs
    )
    relationships.extend(
        _relationship(JIRA_GET_ISSUE_TOOL_ID, "#V#tool_has_output_field", field_id)
        for field_id in detail_outputs
    )
    return tuple(relationships)


def _evidence_view_relationships() -> tuple[JiraRelationshipSpec, ...]:
    detail_fields = (
        JIRA_KEY_FIELD_ID,
        JIRA_SUMMARY_FIELD_ID,
        JIRA_STATUS_FIELD_ID,
        JIRA_ASSIGNEE_FIELD_ID,
        JIRA_ISSUE_TYPE_FIELD_ID,
        JIRA_PARENT_FIELD_ID,
        JIRA_CREATED_FIELD_ID,
        JIRA_UPDATED_FIELD_ID,
        JIRA_DESCRIPTION_FIELD_ID,
    )
    search_fields = (
        JIRA_ISSUES_COLLECTION_FIELD_ID,
        JIRA_KEY_FIELD_ID,
        JIRA_SUMMARY_FIELD_ID,
        JIRA_STATUS_FIELD_ID,
        JIRA_ASSIGNEE_FIELD_ID,
        JIRA_ISSUE_TYPE_FIELD_ID,
        JIRA_PARENT_FIELD_ID,
        JIRA_CREATED_FIELD_ID,
        JIRA_UPDATED_FIELD_ID,
        JIRA_TOTAL_FIELD_ID,
        JIRA_JQL_FIELD_ID,
        JIRA_NEXT_PAGE_TOKEN_FIELD_ID,
        JIRA_IS_LAST_FIELD_ID,
    )
    relationships = [
        _relationship(
            JIRA_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_applies_to_tool",
            JIRA_GET_ISSUE_TOOL_ID,
        ),
        _relationship(
            JIRA_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_applies_to_entity_type",
            JIRA_ISSUE_ENTITY_TYPE_ID,
        ),
        _relationship(
            JIRA_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_has_purpose",
            "#V#tool_evidence_view_purpose_final_answer",
        ),
        _relationship(
            JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_applies_to_tool",
            JIRA_SEARCH_TOOL_ID,
        ),
        _relationship(
            JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_applies_to_entity_type",
            JIRA_ISSUE_ENTITY_TYPE_ID,
        ),
        _relationship(
            JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_has_purpose",
            "#V#tool_evidence_view_purpose_final_answer",
        ),
    ]
    relationships.extend(
        _relationship(
            JIRA_FINAL_ANSWER_VIEW_ID, "#V#evidence_view_requires_field", field_id
        )
        for field_id in JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS
    )
    relationships.extend(
        _relationship(
            JIRA_FINAL_ANSWER_VIEW_ID, "#V#evidence_view_includes_field", field_id
        )
        for field_id in detail_fields
    )
    relationships.extend(
        _relationship(
            JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_requires_field",
            field_id,
        )
        for field_id in (
            JIRA_ISSUES_COLLECTION_FIELD_ID,
            JIRA_KEY_FIELD_ID,
            JIRA_SUMMARY_FIELD_ID,
            JIRA_STATUS_FIELD_ID,
            JIRA_IS_LAST_FIELD_ID,
        )
    )
    relationships.extend(
        _relationship(
            JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_includes_field",
            field_id,
        )
        for field_id in search_fields
    )
    return tuple(relationships)


def _entity_relationships() -> tuple[JiraRelationshipSpec, ...]:
    fields = (
        JIRA_KEY_FIELD_ID,
        JIRA_SUMMARY_FIELD_ID,
        JIRA_STATUS_FIELD_ID,
        JIRA_ASSIGNEE_FIELD_ID,
        JIRA_ISSUE_TYPE_FIELD_ID,
        JIRA_PARENT_FIELD_ID,
        JIRA_CREATED_FIELD_ID,
        JIRA_UPDATED_FIELD_ID,
        JIRA_DESCRIPTION_FIELD_ID,
    )
    return tuple(
        _relationship(
            JIRA_ISSUE_ENTITY_TYPE_ID, "#V#entity_type_has_tool_field", field_id
        )
        for field_id in fields
    )


def _contract_relationships() -> tuple[JiraRelationshipSpec, ...]:
    relationships = [
        _relationship(
            JIRA_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_applies_to_tool",
            JIRA_SEARCH_TOOL_ID,
        ),
        _relationship(
            JIRA_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_applies_to_tool",
            JIRA_GET_ISSUE_TOOL_ID,
        ),
        _relationship(
            JIRA_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_has_evidence_view",
            JIRA_FINAL_ANSWER_VIEW_ID,
        ),
        _relationship(
            JIRA_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_has_evidence_view",
            JIRA_SEARCH_FINAL_ANSWER_VIEW_ID,
        ),
    ]
    relationships.extend(
        _relationship(
            JIRA_TOOL_EVIDENCE_CONTRACT_ID, "#V#vocabulary_includes_concept", concept_id
        )
        for concept_id in canonical_jira_tool_evidence_contract_concept_ids()
        if concept_id != JIRA_TOOL_EVIDENCE_CONTRACT_ID
    )
    return tuple(relationships)


def _jira_relationship_specs() -> tuple[JiraRelationshipSpec, ...]:
    return (
        *_contract_relationships(),
        *_entity_relationships(),
        *_tool_field_relationships(),
        *_field_alias_relationships(),
        *_field_payload_path_relationships(),
        *_field_role_relationships(),
        *_field_policy_relationships(),
        *_evidence_view_relationships(),
    )


def canonical_jira_tool_evidence_contract_concept_ids() -> tuple[str, ...]:
    return tuple(spec.concept_id for spec in JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS)


def canonical_jira_tool_evidence_contract_relationships() -> tuple[
    JiraRelationshipSpec, ...
]:
    return _jira_relationship_specs()


def _normalise_targets(value: Any) -> list[str]:
    return normalise_relationship_targets(value)


def _ensure_structural_targets(
    *, concept_id: str, relationship_key: str, target_ids: Sequence[str]
) -> bool:
    concept_doc = load_concept(concept_id)
    if not isinstance(concept_doc, Mapping):
        return False
    relationships = dict(concept_doc.get("relationships") or {})
    existing_targets = _normalise_targets(relationships.get(relationship_key))
    updated_targets = list(existing_targets)
    for target_id in target_ids:
        cleaned = str(target_id or "").strip()
        if cleaned and cleaned not in updated_targets:
            updated_targets.append(cleaned)
    if updated_targets == existing_targets:
        return False
    concept_service.update_concept(
        concept_id, {f"relationships.{relationship_key}": updated_targets}
    )
    return True


def _ensure_concept_attributes(spec: JiraConceptSpec) -> bool:
    expected_attributes = dict(spec.attributes or {})
    if not expected_attributes:
        return False
    concept_doc = load_concept(spec.concept_id)
    if not isinstance(concept_doc, Mapping):
        return False
    existing_attributes = concept_doc.get("attributes")
    existing_mapping: Mapping[str, Any] = (
        existing_attributes if isinstance(existing_attributes, Mapping) else {}
    )
    updates: dict[str, Any] = {}
    for key, expected_value in expected_attributes.items():
        if existing_mapping.get(key) != expected_value:
            updates[f"attributes.{key}"] = expected_value
    if not updates:
        return False
    concept_service.update_concept(spec.concept_id, updates)
    return True


def _ensure_concept(spec: JiraConceptSpec) -> str:
    concept_doc = load_concept(spec.concept_id)
    if not isinstance(concept_doc, Mapping):
        concept_service.create_concept(
            name=spec.name,
            concept_id=spec.concept_id,
            description=spec.description,
            parent_concept_ids=list(spec.parent_concept_ids),
            create_as_instance=spec.create_as_instance,
            attributes=dict(spec.attributes or {}),
            system_tags=[
                "jira_tool_evidence_contract",
                JIRA_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
            ],
            visibility_scope_mode="global_general",
        )
        return "created"
    relationship_key = (
        "is_an_instance_of" if spec.create_as_instance else "is_a_type_of"
    )
    repaired = _ensure_structural_targets(
        concept_id=spec.concept_id,
        relationship_key=relationship_key,
        target_ids=spec.parent_concept_ids,
    )
    repaired = _ensure_concept_attributes(spec) or repaired
    return "repaired" if repaired else "existing"


def _ensure_relationship(spec: JiraRelationshipSpec) -> dict[str, Any]:
    result = add_relationship(
        source_id=spec.source_id, predicate=spec.predicate, target=spec.target_id
    )
    return dict(result) if isinstance(result, Mapping) else {"success": False}


def _relationship_identity(spec: JiraRelationshipSpec) -> str:
    return f"{spec.source_id}:{spec.predicate}:{spec.target_id}"


def bootstrap_jira_tool_evidence_contract() -> dict[str, Any]:
    vocabulary_report = bootstrap_tool_evidence_contract_vocabulary()
    concept_status_by_id: dict[str, str] = {}
    relationship_ids: list[str] = []
    errors: list[dict[str, Any]] = []
    if not vocabulary_report.get("success"):
        errors.append(
            {
                "section": "generic_vocabulary",
                "reason_code": "tool_evidence_contract_vocabulary_invalid",
                "details": vocabulary_report,
            }
        )

    with suspend_event_workflow_integration():
        for spec in JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
            try:
                concept_status_by_id[spec.concept_id] = _ensure_concept(spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "concepts",
                        "concept_id": spec.concept_id,
                        "reason_code": str(exc),
                    }
                )

        for relationship_spec in _jira_relationship_specs():
            try:
                result = _ensure_relationship(relationship_spec)
            except Exception as exc:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": _relationship_identity(relationship_spec),
                        "reason_code": str(exc),
                    }
                )
                continue
            if result.get("success") is True:
                relationship_ids.append(_relationship_identity(relationship_spec))
            else:
                errors.append(
                    {
                        "section": "relationships",
                        "relationship": _relationship_identity(relationship_spec),
                        "reason_code": str(
                            result.get("error") or "relationship_failed"
                        ),
                        "details": result,
                    }
                )

    validation = validate_jira_tool_evidence_contract()
    validation_errors = validation.get("errors") or []
    if isinstance(validation_errors, list):
        errors.extend(
            dict(error) for error in validation_errors if isinstance(error, Mapping)
        )

    created_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "created"
    ]
    repaired_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "repaired"
    ]
    existing_concept_ids = [
        concept_id
        for concept_id, status in concept_status_by_id.items()
        if status == "existing"
    ]
    return {
        "success": not errors and bool(validation.get("success")),
        "schema_version": JIRA_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "source_tag": JIRA_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "managed_by": JIRA_TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "contract_concept_id": JIRA_TOOL_EVIDENCE_CONTRACT_ID,
        "vocabulary_report": vocabulary_report,
        "created_concept_ids": created_concept_ids,
        "repaired_concept_ids": repaired_concept_ids,
        "existing_concept_ids": existing_concept_ids,
        "relationship_ids": relationship_ids,
        "validation": validation,
        "errors": errors,
        "counts": {
            "concept_specs": len(JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "relationship_specs": len(_jira_relationship_specs()),
            "created_concepts": len(created_concept_ids),
            "existing_concepts": len(existing_concept_ids),
            "repaired_concepts": len(repaired_concept_ids),
            "relationships_written": len(relationship_ids),
            "errors": len(errors),
        },
    }


def validate_jira_tool_evidence_contract() -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    missing_concept_ids: list[str] = []
    missing_relationships: list[str] = []
    vocabulary_validation = validate_tool_evidence_contract_vocabulary()
    if not vocabulary_validation.get("success"):
        errors.append(
            {
                "section": "generic_vocabulary",
                "reason_code": "tool_evidence_contract_vocabulary_invalid",
                "details": vocabulary_validation,
            }
        )
    for spec in JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
        concept_doc = load_concept(spec.concept_id)
        if not isinstance(concept_doc, Mapping):
            missing_concept_ids.append(spec.concept_id)
            errors.append(
                {
                    "section": "concepts",
                    "concept_id": spec.concept_id,
                    "reason_code": "missing_concept",
                }
            )
    for relationship_spec in _jira_relationship_specs():
        source_doc = load_concept(relationship_spec.source_id)
        if not isinstance(source_doc, Mapping):
            continue
        targets = set(
            _normalise_targets(
                (source_doc.get("relationships") or {}).get(relationship_spec.predicate)
            )
        )
        if relationship_spec.target_id not in targets:
            identity = _relationship_identity(relationship_spec)
            missing_relationships.append(identity)
            errors.append(
                {
                    "section": "relationships",
                    "relationship": identity,
                    "reason_code": "missing_relationship",
                }
            )
    return {
        "success": not errors,
        "schema_version": JIRA_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "contract_concept_id": JIRA_TOOL_EVIDENCE_CONTRACT_ID,
        "issue_entity_type_concept_id": JIRA_ISSUE_ENTITY_TYPE_ID,
        "required_final_answer_field_ids": JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS,
        "missing_concept_ids": missing_concept_ids,
        "missing_relationships": missing_relationships,
        "generic_vocabulary_validation": vocabulary_validation,
        "errors": errors,
        "counts": {
            "expected_concepts": len(JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "expected_relationships": len(_jira_relationship_specs()),
            "missing_concepts": len(missing_concept_ids),
            "missing_relationships": len(missing_relationships),
            "errors": len(errors),
        },
    }


__all__ = [
    "JIRA_FINAL_ANSWER_VIEW_ID",
    "JIRA_GET_ISSUE_TOOL_ID",
    "JIRA_ISSUE_ENTITY_TYPE_ID",
    "JIRA_KEY_FIELD_ID",
    "JIRA_REQUIRED_FINAL_ANSWER_FIELD_IDS",
    "JIRA_SEARCH_FINAL_ANSWER_VIEW_ID",
    "JIRA_SEARCH_TOOL_ID",
    "JIRA_STATUS_FIELD_ID",
    "JIRA_SUMMARY_FIELD_ID",
    "JIRA_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS",
    "JIRA_TOOL_EVIDENCE_CONTRACT_ID",
    "JIRA_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION",
    "bootstrap_jira_tool_evidence_contract",
    "canonical_jira_tool_evidence_contract_concept_ids",
    "canonical_jira_tool_evidence_contract_relationships",
    "validate_jira_tool_evidence_contract",
]
