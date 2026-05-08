"""Vontology KR materialisation for Gmail tool evidence contracts.

This module seeds Gmail-specific concepts and relationships that instantiate the
generic tool evidence contract vocabulary. It intentionally does not change
runtime projection behaviour; workflow/runtime consumers should query the
materialised graph rather than growing Gmail-specific orchestrator branches.
"""

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

GMAIL_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION = "gmail_tool_evidence_contract.v1"
GMAIL_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG = "JVNAUTOSCI-2288"
GMAIL_TOOL_EVIDENCE_CONTRACT_MANAGED_BY = (
    "gmail_tool_evidence_contract_vontology_service"
)

GMAIL_TOOL_EVIDENCE_CONTRACT_ID = "#V#gmail_tool_evidence_contract_v1"
GMAIL_MESSAGE_ENTITY_TYPE_ID = "#V#gmail_message_result_entity_type"
GMAIL_LIST_MESSAGES_TOOL_ID = "#V#gmail_list_messages_tool"
GMAIL_GET_MESSAGE_TOOL_ID = "#V#gmail_get_message_tool"
GMAIL_FINAL_ANSWER_VIEW_ID = "#V#gmail_message_final_answer_evidence_view"
GMAIL_FOLLOW_UP_VIEW_ID = "#V#gmail_message_follow_up_selection_evidence_view"
GMAIL_USER_DISPLAY_VIEW_ID = "#V#gmail_message_user_display_evidence_view"
GMAIL_LIST_DETAIL_AFFORDANCE_ID = "#V#gmail_message_list_detail_affordance"

GMAIL_MESSAGES_COLLECTION_FIELD_ID = "#V#gmail_messages_collection_field"
GMAIL_PROFILE_ARGUMENT_FIELD_ID = "#V#gmail_profile_argument_field"
GMAIL_QUERY_ARGUMENT_FIELD_ID = "#V#gmail_query_argument_field"
GMAIL_LABEL_IDS_ARGUMENT_FIELD_ID = "#V#gmail_label_ids_argument_field"
GMAIL_MAX_RESULTS_ARGUMENT_FIELD_ID = "#V#gmail_max_results_argument_field"
GMAIL_BYPASS_PROFILE_FILTER_ARGUMENT_FIELD_ID = (
    "#V#gmail_bypass_profile_filter_argument_field"
)
GMAIL_FORMAT_ARGUMENT_FIELD_ID = "#V#gmail_format_argument_field"
GMAIL_MESSAGE_ID_FIELD_ID = "#V#gmail_message_id_field"
GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID = "#V#gmail_message_id_argument_field"
GMAIL_THREAD_ID_FIELD_ID = "#V#gmail_thread_id_field"
GMAIL_SENDER_FIELD_ID = "#V#gmail_sender_field"
GMAIL_SUBJECT_FIELD_ID = "#V#gmail_subject_field"
GMAIL_DATE_FIELD_ID = "#V#gmail_date_field"
GMAIL_SNIPPET_FIELD_ID = "#V#gmail_snippet_field"
GMAIL_LABEL_IDS_FIELD_ID = "#V#gmail_label_ids_field"
GMAIL_PAYLOAD_FIELD_ID = "#V#gmail_payload_field"
GMAIL_EFFECTIVE_QUERY_FIELD_ID = "#V#gmail_effective_query_field"
GMAIL_NOTES_FIELD_ID = "#V#gmail_notes_field"

GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS: tuple[str, ...] = (
    GMAIL_SENDER_FIELD_ID,
    GMAIL_SUBJECT_FIELD_ID,
    GMAIL_DATE_FIELD_ID,
    GMAIL_SNIPPET_FIELD_ID,
)


@dataclass(frozen=True)
class GmailConceptSpec:
    concept_id: str
    name: str
    description: str
    parent_concept_ids: tuple[str, ...]
    create_as_instance: bool
    category: str
    attributes: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class GmailRelationshipSpec:
    source_id: str
    predicate: str
    target_id: str


def _base_attributes(category: str) -> dict[str, Any]:
    return {
        "repo_seed_source_tag": GMAIL_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "repo_seed_managed_by": GMAIL_TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "schema_version": GMAIL_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "tool_evidence_contract_id": GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
        "tool_evidence_contract_vocabulary_id": TOOL_EVIDENCE_CONTRACT_VOCABULARY_ID,
        "tool_contract_category": category,
    }


def _concept(
    *,
    concept_id: str,
    name: str,
    description: str,
    parent_concept_ids: tuple[str, ...],
    create_as_instance: bool = True,
    category: str,
    attributes: Mapping[str, Any] | None = None,
) -> GmailConceptSpec:
    merged_attributes = _base_attributes(category)
    if attributes:
        merged_attributes.update(dict(attributes))
    return GmailConceptSpec(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=parent_concept_ids,
        create_as_instance=create_as_instance,
        category=category,
        attributes=merged_attributes,
    )


def _field(
    concept_id: str,
    name: str,
    description: str,
    *,
    field_key: str,
) -> GmailConceptSpec:
    return _concept(
        concept_id=concept_id,
        name=name,
        description=description,
        parent_concept_ids=("#V#tool_result_field",),
        category="field",
        attributes={"field_key": field_key},
    )


def _wire_key(key: str) -> GmailConceptSpec:
    safe_key = key.replace(".", "_").replace("-", "_").lower()
    return _concept(
        concept_id=f"#V#gmail_wire_key_{safe_key}",
        name=f"Gmail wire key {key}",
        description=f"Wire key named {key} in Gmail MCP tool payloads.",
        parent_concept_ids=("#V#tool_wire_key",),
        category="wire_key",
        attributes={"wire_key": key},
    )


def _payload_path(path_id: str, path: str) -> GmailConceptSpec:
    return _concept(
        concept_id=path_id,
        name=f"Gmail payload path {path}",
        description=f"Payload path {path} in Gmail MCP tool results.",
        parent_concept_ids=("#V#tool_payload_path",),
        category="payload_path",
        attributes={"payload_path": path},
    )


_CORE_CONCEPT_SPECS: tuple[GmailConceptSpec, ...] = (
    _concept(
        concept_id=GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
        name="Gmail tool evidence contract v1",
        description=(
            "Gmail-specific contract binding Gmail MCP tools, message fields, "
            "evidence views, and list/detail follow-up affordances."
        ),
        parent_concept_ids=("#V#tool_interface_contract",),
        category="contract",
    ),
    _concept(
        concept_id=GMAIL_LIST_MESSAGES_TOOL_ID,
        name="gmail_list_messages tool",
        description="Built-in internal MCP tool that lists Gmail messages for a profile.",
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={"mcp_tool_name": "gmail_list_messages"},
    ),
    _concept(
        concept_id=GMAIL_GET_MESSAGE_TOOL_ID,
        name="gmail_get_message tool",
        description="Built-in internal MCP tool that fetches details for one Gmail message.",
        parent_concept_ids=("#V#mcp_tool",),
        category="tool",
        attributes={"mcp_tool_name": "gmail_get_message"},
    ),
    _concept(
        concept_id=GMAIL_MESSAGE_ENTITY_TYPE_ID,
        name="Gmail message result entity type",
        description="Entity type for a Gmail message item returned by Gmail MCP read tools.",
        parent_concept_ids=("#V#tool_result_entity_type",),
        category="entity_type",
    ),
    _concept(
        concept_id=GMAIL_FINAL_ANSWER_VIEW_ID,
        name="Gmail message final-answer evidence view",
        description=(
            "Evidence view requiring the Gmail fields needed when answering a "
            "user's request for message rows or message details."
        ),
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=GMAIL_FOLLOW_UP_VIEW_ID,
        name="Gmail message follow-up selection evidence view",
        description=(
            "Evidence view preserving identifiers and profile arguments needed "
            "to call gmail_get_message after gmail_list_messages."
        ),
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=GMAIL_USER_DISPLAY_VIEW_ID,
        name="Gmail message user-display evidence view",
        description="Evidence view for fields that may be displayed directly to the user.",
        parent_concept_ids=("#V#tool_evidence_view",),
        category="evidence_view",
    ),
    _concept(
        concept_id=GMAIL_LIST_DETAIL_AFFORDANCE_ID,
        name="Gmail message list-detail affordance",
        description=(
            "Affordance stating that gmail_list_messages items can be completed "
            "by gmail_get_message using the message identifier."
        ),
        parent_concept_ids=("#V#tool_list_detail_affordance",),
        category="list_detail_affordance",
    ),
)

_FIELD_CONCEPT_SPECS: tuple[GmailConceptSpec, ...] = (
    _field(
        GMAIL_MESSAGES_COLLECTION_FIELD_ID,
        "Gmail messages collection field",
        "Collection field containing Gmail message rows in gmail_list_messages output.",
        field_key="messages",
    ),
    _field(
        GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        "Gmail profile argument field",
        "Profile alias or authorised address used to scope Gmail read tools.",
        field_key="profile",
    ),
    _field(
        GMAIL_QUERY_ARGUMENT_FIELD_ID,
        "Gmail query argument field",
        "Optional Gmail search query argument for gmail_list_messages.",
        field_key="query",
    ),
    _field(
        GMAIL_LABEL_IDS_ARGUMENT_FIELD_ID,
        "Gmail label IDs argument field",
        "Optional Gmail label IDs argument for gmail_list_messages.",
        field_key="label_ids",
    ),
    _field(
        GMAIL_MAX_RESULTS_ARGUMENT_FIELD_ID,
        "Gmail max results argument field",
        "Optional maximum result count argument for gmail_list_messages.",
        field_key="max_results",
    ),
    _field(
        GMAIL_BYPASS_PROFILE_FILTER_ARGUMENT_FIELD_ID,
        "Gmail bypass profile filter argument field",
        "Optional flag that bypasses profile-level query and label filters.",
        field_key="bypass_profile_query_prefix",
    ),
    _field(
        GMAIL_FORMAT_ARGUMENT_FIELD_ID,
        "Gmail format argument field",
        "Optional Gmail API format argument for gmail_get_message.",
        field_key="format",
    ),
    _field(
        GMAIL_MESSAGE_ID_FIELD_ID,
        "Gmail message identifier field",
        "Stable Gmail message identifier returned by list and detail calls.",
        field_key="message_id",
    ),
    _field(
        GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID,
        "Gmail message identifier argument field",
        "Input argument on gmail_get_message populated from a listed message id.",
        field_key="message_id",
    ),
    _field(
        GMAIL_THREAD_ID_FIELD_ID,
        "Gmail thread identifier field",
        "Gmail thread identifier associated with a message.",
        field_key="threadId",
    ),
    _field(
        GMAIL_SENDER_FIELD_ID,
        "Gmail sender field",
        "Normalised sender value extracted from Gmail message metadata.",
        field_key="sender",
    ),
    _field(
        GMAIL_SUBJECT_FIELD_ID,
        "Gmail subject field",
        "Normalised subject value extracted from Gmail message metadata.",
        field_key="subject",
    ),
    _field(
        GMAIL_DATE_FIELD_ID,
        "Gmail date field",
        "Normalised date value extracted from Gmail message metadata.",
        field_key="date",
    ),
    _field(
        GMAIL_SNIPPET_FIELD_ID,
        "Gmail snippet field",
        "Snippet text returned by Gmail for a message.",
        field_key="snippet",
    ),
    _field(
        GMAIL_LABEL_IDS_FIELD_ID,
        "Gmail label IDs field",
        "Labels associated with a Gmail message result.",
        field_key="labelIds",
    ),
    _field(
        GMAIL_PAYLOAD_FIELD_ID,
        "Gmail payload field",
        "Full Gmail payload object returned by detail calls.",
        field_key="payload",
    ),
    _field(
        GMAIL_EFFECTIVE_QUERY_FIELD_ID,
        "Gmail effective query field",
        "Diagnostic field describing profile and caller query filters applied to a list call.",
        field_key="effective_query",
    ),
    _field(
        GMAIL_NOTES_FIELD_ID,
        "Gmail notes field",
        "Diagnostic notes emitted by Gmail list calls.",
        field_key="notes",
    ),
)

_WIRE_KEYS = (
    "profile",
    "query",
    "label_ids",
    "max_results",
    "maxResults",
    "bypass_profile_query_prefix",
    "format",
    "messages",
    "message_id",
    "id",
    "threadId",
    "sender",
    "from",
    "subject",
    "date",
    "snippet",
    "labelIds",
    "payload",
    "effective_query",
    "notes",
)

_WIRE_KEY_CONCEPT_SPECS: tuple[GmailConceptSpec, ...] = tuple(
    _wire_key(key) for key in _WIRE_KEYS
)

_PAYLOAD_PATH_SPECS: tuple[GmailConceptSpec, ...] = (
    _payload_path("#V#gmail_payload_path_messages", "messages"),
    _payload_path("#V#gmail_payload_path_messages_message_id", "messages[].message_id"),
    _payload_path("#V#gmail_payload_path_messages_id", "messages[].id"),
    _payload_path("#V#gmail_payload_path_messages_thread_id", "messages[].threadId"),
    _payload_path("#V#gmail_payload_path_profile", "profile"),
    _payload_path("#V#gmail_payload_path_effective_query", "effective_query"),
    _payload_path("#V#gmail_payload_path_notes", "notes"),
    _payload_path("#V#gmail_payload_path_message_id", "message_id"),
    _payload_path("#V#gmail_payload_path_id", "id"),
    _payload_path("#V#gmail_payload_path_thread_id", "threadId"),
    _payload_path("#V#gmail_payload_path_sender", "sender"),
    _payload_path("#V#gmail_payload_path_from", "from"),
    _payload_path("#V#gmail_payload_path_subject", "subject"),
    _payload_path("#V#gmail_payload_path_date", "date"),
    _payload_path("#V#gmail_payload_path_snippet", "snippet"),
    _payload_path("#V#gmail_payload_path_label_ids", "labelIds"),
    _payload_path("#V#gmail_payload_path_payload", "payload"),
    _payload_path("#V#gmail_payload_path_header_from", "payload.headers[name=From]"),
    _payload_path("#V#gmail_payload_path_header_subject", "payload.headers[name=Subject]"),
    _payload_path("#V#gmail_payload_path_header_date", "payload.headers[name=Date]"),
)

GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS: tuple[GmailConceptSpec, ...] = (
    *_CORE_CONCEPT_SPECS,
    *_FIELD_CONCEPT_SPECS,
    *_WIRE_KEY_CONCEPT_SPECS,
    *_PAYLOAD_PATH_SPECS,
)


def _relationship(
    source_id: str,
    predicate: str,
    target_id: str,
) -> GmailRelationshipSpec:
    return GmailRelationshipSpec(source_id=source_id, predicate=predicate, target_id=target_id)


def _wire_key_id(key: str) -> str:
    safe_key = key.replace(".", "_").replace("-", "_").lower()
    return f"#V#gmail_wire_key_{safe_key}"


def _field_alias_relationships() -> tuple[GmailRelationshipSpec, ...]:
    alias_map = {
        GMAIL_MESSAGES_COLLECTION_FIELD_ID: ("messages",),
        GMAIL_PROFILE_ARGUMENT_FIELD_ID: ("profile",),
        GMAIL_QUERY_ARGUMENT_FIELD_ID: ("query",),
        GMAIL_LABEL_IDS_ARGUMENT_FIELD_ID: ("label_ids",),
        GMAIL_MAX_RESULTS_ARGUMENT_FIELD_ID: ("max_results", "maxResults"),
        GMAIL_BYPASS_PROFILE_FILTER_ARGUMENT_FIELD_ID: ("bypass_profile_query_prefix",),
        GMAIL_FORMAT_ARGUMENT_FIELD_ID: ("format",),
        GMAIL_MESSAGE_ID_FIELD_ID: ("message_id", "id"),
        GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID: ("message_id", "id"),
        GMAIL_THREAD_ID_FIELD_ID: ("threadId",),
        GMAIL_SENDER_FIELD_ID: ("sender", "from"),
        GMAIL_SUBJECT_FIELD_ID: ("subject",),
        GMAIL_DATE_FIELD_ID: ("date",),
        GMAIL_SNIPPET_FIELD_ID: ("snippet",),
        GMAIL_LABEL_IDS_FIELD_ID: ("labelIds",),
        GMAIL_PAYLOAD_FIELD_ID: ("payload",),
        GMAIL_EFFECTIVE_QUERY_FIELD_ID: ("effective_query",),
        GMAIL_NOTES_FIELD_ID: ("notes",),
    }
    return tuple(
        _relationship(field_id, "#V#field_has_wire_alias", _wire_key_id(alias))
        for field_id, aliases in alias_map.items()
        for alias in aliases
    )


def _field_payload_path_relationships() -> tuple[GmailRelationshipSpec, ...]:
    path_map = {
        GMAIL_MESSAGES_COLLECTION_FIELD_ID: ("#V#gmail_payload_path_messages",),
        GMAIL_PROFILE_ARGUMENT_FIELD_ID: ("#V#gmail_payload_path_profile",),
        GMAIL_MESSAGE_ID_FIELD_ID: (
            "#V#gmail_payload_path_messages_message_id",
            "#V#gmail_payload_path_messages_id",
            "#V#gmail_payload_path_message_id",
            "#V#gmail_payload_path_id",
        ),
        GMAIL_THREAD_ID_FIELD_ID: (
            "#V#gmail_payload_path_messages_thread_id",
            "#V#gmail_payload_path_thread_id",
        ),
        GMAIL_SENDER_FIELD_ID: (
            "#V#gmail_payload_path_sender",
            "#V#gmail_payload_path_from",
            "#V#gmail_payload_path_header_from",
        ),
        GMAIL_SUBJECT_FIELD_ID: (
            "#V#gmail_payload_path_subject",
            "#V#gmail_payload_path_header_subject",
        ),
        GMAIL_DATE_FIELD_ID: (
            "#V#gmail_payload_path_date",
            "#V#gmail_payload_path_header_date",
        ),
        GMAIL_SNIPPET_FIELD_ID: ("#V#gmail_payload_path_snippet",),
        GMAIL_LABEL_IDS_FIELD_ID: ("#V#gmail_payload_path_label_ids",),
        GMAIL_PAYLOAD_FIELD_ID: ("#V#gmail_payload_path_payload",),
        GMAIL_EFFECTIVE_QUERY_FIELD_ID: ("#V#gmail_payload_path_effective_query",),
        GMAIL_NOTES_FIELD_ID: ("#V#gmail_payload_path_notes",),
    }
    return tuple(
        _relationship(field_id, "#V#field_extracts_from_payload_path", path_id)
        for field_id, path_ids in path_map.items()
        for path_id in path_ids
    )


def _field_role_relationships() -> tuple[GmailRelationshipSpec, ...]:
    role_map = {
        GMAIL_MESSAGES_COLLECTION_FIELD_ID: ("#V#tool_field_role_collection_membership",),
        GMAIL_PROFILE_ARGUMENT_FIELD_ID: ("#V#tool_field_role_follow_up_argument",),
        GMAIL_QUERY_ARGUMENT_FIELD_ID: ("#V#tool_field_role_follow_up_argument",),
        GMAIL_LABEL_IDS_ARGUMENT_FIELD_ID: ("#V#tool_field_role_follow_up_argument",),
        GMAIL_MAX_RESULTS_ARGUMENT_FIELD_ID: ("#V#tool_field_role_follow_up_argument",),
        GMAIL_BYPASS_PROFILE_FILTER_ARGUMENT_FIELD_ID: (
            "#V#tool_field_role_follow_up_argument",
        ),
        GMAIL_FORMAT_ARGUMENT_FIELD_ID: ("#V#tool_field_role_follow_up_argument",),
        GMAIL_MESSAGE_ID_FIELD_ID: (
            "#V#tool_field_role_entity_identifier",
            "#V#tool_field_role_follow_up_argument",
        ),
        GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID: ("#V#tool_field_role_follow_up_argument",),
        GMAIL_THREAD_ID_FIELD_ID: ("#V#tool_field_role_entity_identifier",),
        GMAIL_SENDER_FIELD_ID: ("#V#tool_field_role_answer_evidence",),
        GMAIL_SUBJECT_FIELD_ID: (
            "#V#tool_field_role_answer_evidence",
            "#V#tool_field_role_display_label",
        ),
        GMAIL_DATE_FIELD_ID: ("#V#tool_field_role_answer_evidence",),
        GMAIL_SNIPPET_FIELD_ID: (
            "#V#tool_field_role_answer_evidence",
            "#V#tool_field_role_sensitive_content",
        ),
        GMAIL_LABEL_IDS_FIELD_ID: ("#V#tool_field_role_answer_evidence",),
        GMAIL_PAYLOAD_FIELD_ID: ("#V#tool_field_role_sensitive_content",),
        GMAIL_EFFECTIVE_QUERY_FIELD_ID: ("#V#tool_field_role_answer_evidence",),
        GMAIL_NOTES_FIELD_ID: ("#V#tool_field_role_answer_evidence",),
    }
    return tuple(
        _relationship(field_id, "#V#field_has_role", role_id)
        for field_id, role_ids in role_map.items()
        for role_id in role_ids
    )


def _field_policy_relationships() -> tuple[GmailRelationshipSpec, ...]:
    relationships: list[GmailRelationshipSpec] = []
    for field_id in GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS:
        relationships.append(
            _relationship(
                field_id,
                "#V#field_has_completion_policy",
                "#V#tool_field_completion_required_before_answer",
            )
        )
        relationships.append(
            _relationship(
                field_id,
                "#V#field_has_redaction_policy",
                "#V#tool_redaction_policy_include_plaintext",
            )
        )
    for field_id in (
        GMAIL_MESSAGE_ID_FIELD_ID,
        GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID,
        GMAIL_PROFILE_ARGUMENT_FIELD_ID,
    ):
        relationships.append(
            _relationship(
                field_id,
                "#V#field_has_completion_policy",
                "#V#tool_field_completion_required_for_follow_up",
            )
        )
        relationships.append(
            _relationship(
                field_id,
                "#V#field_has_redaction_policy",
                "#V#tool_redaction_policy_preserve_identifier_only",
            )
        )
    relationships.append(
        _relationship(
            GMAIL_PAYLOAD_FIELD_ID,
            "#V#field_has_redaction_policy",
            "#V#tool_redaction_policy_redact_by_default",
        )
    )
    return tuple(relationships)


def _tool_field_relationships() -> tuple[GmailRelationshipSpec, ...]:
    list_input_fields = (
        GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        GMAIL_QUERY_ARGUMENT_FIELD_ID,
        GMAIL_LABEL_IDS_ARGUMENT_FIELD_ID,
        GMAIL_MAX_RESULTS_ARGUMENT_FIELD_ID,
        GMAIL_BYPASS_PROFILE_FILTER_ARGUMENT_FIELD_ID,
    )
    list_output_fields = (
        GMAIL_MESSAGES_COLLECTION_FIELD_ID,
        GMAIL_MESSAGE_ID_FIELD_ID,
        GMAIL_THREAD_ID_FIELD_ID,
        GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        GMAIL_EFFECTIVE_QUERY_FIELD_ID,
        GMAIL_NOTES_FIELD_ID,
    )
    detail_input_fields = (
        GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID,
        GMAIL_FORMAT_ARGUMENT_FIELD_ID,
    )
    detail_output_fields = (
        GMAIL_MESSAGE_ID_FIELD_ID,
        GMAIL_THREAD_ID_FIELD_ID,
        GMAIL_LABEL_IDS_FIELD_ID,
        GMAIL_SNIPPET_FIELD_ID,
        GMAIL_PAYLOAD_FIELD_ID,
        GMAIL_SENDER_FIELD_ID,
        GMAIL_SUBJECT_FIELD_ID,
        GMAIL_DATE_FIELD_ID,
    )

    relationships: list[GmailRelationshipSpec] = []
    relationships.extend(
        _relationship(GMAIL_LIST_MESSAGES_TOOL_ID, "#V#tool_has_input_field", field_id)
        for field_id in list_input_fields
    )
    relationships.extend(
        _relationship(GMAIL_LIST_MESSAGES_TOOL_ID, "#V#tool_has_output_field", field_id)
        for field_id in list_output_fields
    )
    relationships.extend(
        _relationship(GMAIL_GET_MESSAGE_TOOL_ID, "#V#tool_has_input_field", field_id)
        for field_id in detail_input_fields
    )
    relationships.extend(
        _relationship(GMAIL_GET_MESSAGE_TOOL_ID, "#V#tool_has_output_field", field_id)
        for field_id in detail_output_fields
    )
    return tuple(relationships)


def _evidence_view_relationships() -> tuple[GmailRelationshipSpec, ...]:
    final_included_fields = (
        GMAIL_MESSAGE_ID_FIELD_ID,
        GMAIL_THREAD_ID_FIELD_ID,
        GMAIL_LABEL_IDS_FIELD_ID,
        *GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS,
    )
    user_display_fields = (
        GMAIL_MESSAGE_ID_FIELD_ID,
        *GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS,
    )
    follow_up_fields = (
        GMAIL_MESSAGES_COLLECTION_FIELD_ID,
        GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        GMAIL_MESSAGE_ID_FIELD_ID,
        GMAIL_THREAD_ID_FIELD_ID,
    )

    relationships: list[GmailRelationshipSpec] = [
        _relationship(
            GMAIL_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_applies_to_tool",
            GMAIL_GET_MESSAGE_TOOL_ID,
        ),
        _relationship(
            GMAIL_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_applies_to_entity_type",
            GMAIL_MESSAGE_ENTITY_TYPE_ID,
        ),
        _relationship(
            GMAIL_FINAL_ANSWER_VIEW_ID,
            "#V#evidence_view_has_purpose",
            "#V#tool_evidence_view_purpose_final_answer",
        ),
        _relationship(
            GMAIL_FOLLOW_UP_VIEW_ID,
            "#V#evidence_view_applies_to_tool",
            GMAIL_LIST_MESSAGES_TOOL_ID,
        ),
        _relationship(
            GMAIL_FOLLOW_UP_VIEW_ID,
            "#V#evidence_view_applies_to_entity_type",
            GMAIL_MESSAGE_ENTITY_TYPE_ID,
        ),
        _relationship(
            GMAIL_FOLLOW_UP_VIEW_ID,
            "#V#evidence_view_has_purpose",
            "#V#tool_evidence_view_purpose_follow_up_selection",
        ),
        _relationship(
            GMAIL_USER_DISPLAY_VIEW_ID,
            "#V#evidence_view_applies_to_tool",
            GMAIL_GET_MESSAGE_TOOL_ID,
        ),
        _relationship(
            GMAIL_USER_DISPLAY_VIEW_ID,
            "#V#evidence_view_applies_to_entity_type",
            GMAIL_MESSAGE_ENTITY_TYPE_ID,
        ),
        _relationship(
            GMAIL_USER_DISPLAY_VIEW_ID,
            "#V#evidence_view_has_purpose",
            "#V#tool_evidence_view_purpose_user_display",
        ),
    ]
    relationships.extend(
        _relationship(GMAIL_FINAL_ANSWER_VIEW_ID, "#V#evidence_view_requires_field", field_id)
        for field_id in GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS
    )
    relationships.extend(
        _relationship(GMAIL_FINAL_ANSWER_VIEW_ID, "#V#evidence_view_includes_field", field_id)
        for field_id in final_included_fields
    )
    relationships.extend(
        _relationship(GMAIL_FOLLOW_UP_VIEW_ID, "#V#evidence_view_requires_field", field_id)
        for field_id in (GMAIL_PROFILE_ARGUMENT_FIELD_ID, GMAIL_MESSAGE_ID_FIELD_ID)
    )
    relationships.extend(
        _relationship(GMAIL_FOLLOW_UP_VIEW_ID, "#V#evidence_view_includes_field", field_id)
        for field_id in follow_up_fields
    )
    relationships.extend(
        _relationship(GMAIL_USER_DISPLAY_VIEW_ID, "#V#evidence_view_includes_field", field_id)
        for field_id in user_display_fields
    )
    relationships.append(
        _relationship(
            GMAIL_USER_DISPLAY_VIEW_ID,
            "#V#evidence_view_redacts_field",
            GMAIL_PAYLOAD_FIELD_ID,
        )
    )
    return tuple(relationships)


def _entity_relationships() -> tuple[GmailRelationshipSpec, ...]:
    fields = (
        GMAIL_MESSAGE_ID_FIELD_ID,
        GMAIL_THREAD_ID_FIELD_ID,
        GMAIL_SENDER_FIELD_ID,
        GMAIL_SUBJECT_FIELD_ID,
        GMAIL_DATE_FIELD_ID,
        GMAIL_SNIPPET_FIELD_ID,
        GMAIL_LABEL_IDS_FIELD_ID,
        GMAIL_PAYLOAD_FIELD_ID,
    )
    return tuple(
        _relationship(GMAIL_MESSAGE_ENTITY_TYPE_ID, "#V#entity_type_has_tool_field", field_id)
        for field_id in fields
    )


def _contract_relationships() -> tuple[GmailRelationshipSpec, ...]:
    relationships = [
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_applies_to_tool",
            GMAIL_LIST_MESSAGES_TOOL_ID,
        ),
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_applies_to_tool",
            GMAIL_GET_MESSAGE_TOOL_ID,
        ),
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_has_evidence_view",
            GMAIL_FINAL_ANSWER_VIEW_ID,
        ),
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_has_evidence_view",
            GMAIL_FOLLOW_UP_VIEW_ID,
        ),
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_has_evidence_view",
            GMAIL_USER_DISPLAY_VIEW_ID,
        ),
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#tool_contract_has_list_detail_affordance",
            GMAIL_LIST_DETAIL_AFFORDANCE_ID,
        ),
    ]
    relationships.extend(
        _relationship(
            GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
            "#V#vocabulary_includes_concept",
            concept_id,
        )
        for concept_id in canonical_gmail_tool_evidence_contract_concept_ids()
        if concept_id != GMAIL_TOOL_EVIDENCE_CONTRACT_ID
    )
    return tuple(relationships)


def _list_detail_relationships() -> tuple[GmailRelationshipSpec, ...]:
    relationships = [
        _relationship(
            GMAIL_LIST_MESSAGES_TOOL_ID,
            "#V#tool_emits_collection_entity_type",
            GMAIL_MESSAGE_ENTITY_TYPE_ID,
        ),
        _relationship(
            GMAIL_GET_MESSAGE_TOOL_ID,
            "#V#tool_emits_entity_type",
            GMAIL_MESSAGE_ENTITY_TYPE_ID,
        ),
        _relationship(
            GMAIL_LIST_MESSAGES_TOOL_ID,
            "#V#list_tool_has_detail_tool",
            GMAIL_GET_MESSAGE_TOOL_ID,
        ),
        _relationship(
            GMAIL_LIST_DETAIL_AFFORDANCE_ID,
            "#V#list_detail_affordance_has_list_tool",
            GMAIL_LIST_MESSAGES_TOOL_ID,
        ),
        _relationship(
            GMAIL_LIST_DETAIL_AFFORDANCE_ID,
            "#V#list_detail_affordance_has_detail_tool",
            GMAIL_GET_MESSAGE_TOOL_ID,
        ),
        _relationship(
            GMAIL_LIST_DETAIL_AFFORDANCE_ID,
            "#V#list_detail_affordance_uses_identifier_field",
            GMAIL_MESSAGE_ID_FIELD_ID,
        ),
        _relationship(
            GMAIL_GET_MESSAGE_TOOL_ID,
            "#V#detail_tool_accepts_identifier_field",
            GMAIL_MESSAGE_ID_FIELD_ID,
        ),
        _relationship(
            GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID,
            "#V#tool_argument_maps_from_field",
            GMAIL_MESSAGE_ID_FIELD_ID,
        ),
    ]
    relationships.extend(
        _relationship(GMAIL_GET_MESSAGE_TOOL_ID, "#V#detail_tool_completes_field", field_id)
        for field_id in (
            GMAIL_SENDER_FIELD_ID,
            GMAIL_SUBJECT_FIELD_ID,
            GMAIL_DATE_FIELD_ID,
            GMAIL_SNIPPET_FIELD_ID,
            GMAIL_LABEL_IDS_FIELD_ID,
            GMAIL_PAYLOAD_FIELD_ID,
        )
    )
    relationships.extend(
        _relationship(GMAIL_LIST_MESSAGES_TOOL_ID, "#V#tool_result_preserves_field", field_id)
        for field_id in (
            GMAIL_PROFILE_ARGUMENT_FIELD_ID,
            GMAIL_MESSAGE_ID_FIELD_ID,
            GMAIL_THREAD_ID_FIELD_ID,
        )
    )
    relationships.extend(
        _relationship(GMAIL_GET_MESSAGE_TOOL_ID, "#V#tool_result_preserves_field", field_id)
        for field_id in (
            GMAIL_MESSAGE_ID_FIELD_ID,
            GMAIL_SENDER_FIELD_ID,
            GMAIL_SUBJECT_FIELD_ID,
            GMAIL_DATE_FIELD_ID,
            GMAIL_SNIPPET_FIELD_ID,
        )
    )
    return tuple(relationships)


def _gmail_relationship_specs() -> tuple[GmailRelationshipSpec, ...]:
    return (
        *_contract_relationships(),
        *_entity_relationships(),
        *_tool_field_relationships(),
        *_field_alias_relationships(),
        *_field_payload_path_relationships(),
        *_field_role_relationships(),
        *_field_policy_relationships(),
        *_evidence_view_relationships(),
        *_list_detail_relationships(),
    )


def canonical_gmail_tool_evidence_contract_concept_ids() -> tuple[str, ...]:
    """Return canonical Gmail tool contract concept IDs in materialisation order."""

    return tuple(spec.concept_id for spec in GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS)


def canonical_gmail_tool_evidence_contract_relationships() -> tuple[
    GmailRelationshipSpec, ...
]:
    """Return the canonical Gmail contract relationship graph."""

    return _gmail_relationship_specs()


def _normalise_targets(value: Any) -> list[str]:
    return normalise_relationship_targets(value)


def _ensure_structural_targets(
    *,
    concept_id: str,
    relationship_key: str,
    target_ids: Sequence[str],
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
        concept_id,
        {f"relationships.{relationship_key}": updated_targets},
    )
    return True


def _ensure_concept(spec: GmailConceptSpec) -> str:
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
                "gmail_tool_evidence_contract",
                GMAIL_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
            ],
            visibility_scope_mode="global_general",
        )
        return "created"

    relationship_key = "is_an_instance_of" if spec.create_as_instance else "is_a_type_of"
    repaired = _ensure_structural_targets(
        concept_id=spec.concept_id,
        relationship_key=relationship_key,
        target_ids=spec.parent_concept_ids,
    )
    return "repaired" if repaired else "existing"


def _ensure_relationship(spec: GmailRelationshipSpec) -> dict[str, Any]:
    result = add_relationship(
        source_id=spec.source_id,
        predicate=spec.predicate,
        target=spec.target_id,
    )
    return dict(result) if isinstance(result, Mapping) else {"success": False}


def _relationship_identity(spec: GmailRelationshipSpec) -> str:
    return f"{spec.source_id}:{spec.predicate}:{spec.target_id}"


def bootstrap_gmail_tool_evidence_contract() -> dict[str, Any]:
    """Materialise Gmail-specific tool evidence contracts into Vontology."""

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
        for spec in GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
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

        for relationship_spec in _gmail_relationship_specs():
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
                        "reason_code": str(result.get("error") or "relationship_failed"),
                        "details": result,
                    }
                )

    validation = validate_gmail_tool_evidence_contract()
    validation_errors = validation.get("errors") or []
    if isinstance(validation_errors, list):
        for error in validation_errors:
            if isinstance(error, Mapping):
                errors.append(dict(error))

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
        "schema_version": GMAIL_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "source_tag": GMAIL_TOOL_EVIDENCE_CONTRACT_SOURCE_TAG,
        "managed_by": GMAIL_TOOL_EVIDENCE_CONTRACT_MANAGED_BY,
        "contract_concept_id": GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
        "vocabulary_report": vocabulary_report,
        "created_concept_ids": created_concept_ids,
        "repaired_concept_ids": repaired_concept_ids,
        "existing_concept_ids": existing_concept_ids,
        "relationship_ids": relationship_ids,
        "validation": validation,
        "errors": errors,
        "counts": {
            "concept_specs": len(GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "relationship_specs": len(_gmail_relationship_specs()),
            "created_concepts": len(created_concept_ids),
            "existing_concepts": len(existing_concept_ids),
            "repaired_concepts": len(repaired_concept_ids),
            "relationships_written": len(relationship_ids),
            "errors": len(errors),
        },
    }


def validate_gmail_tool_evidence_contract() -> dict[str, Any]:
    """Validate that the Gmail evidence contract has the expected graph shape."""

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

    for spec in GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS:
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

    for relationship_spec in _gmail_relationship_specs():
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
        "schema_version": GMAIL_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION,
        "contract_concept_id": GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
        "message_entity_type_concept_id": GMAIL_MESSAGE_ENTITY_TYPE_ID,
        "required_final_answer_field_ids": GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS,
        "missing_concept_ids": missing_concept_ids,
        "missing_relationships": missing_relationships,
        "generic_vocabulary_validation": vocabulary_validation,
        "errors": errors,
        "counts": {
            "expected_concepts": len(GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS),
            "expected_relationships": len(_gmail_relationship_specs()),
            "missing_concepts": len(missing_concept_ids),
            "missing_relationships": len(missing_relationships),
            "errors": len(errors),
        },
    }


__all__ = [
    "GMAIL_DATE_FIELD_ID",
    "GMAIL_FINAL_ANSWER_VIEW_ID",
    "GMAIL_FOLLOW_UP_VIEW_ID",
    "GMAIL_GET_MESSAGE_TOOL_ID",
    "GMAIL_LIST_DETAIL_AFFORDANCE_ID",
    "GMAIL_LIST_MESSAGES_TOOL_ID",
    "GMAIL_MESSAGE_ENTITY_TYPE_ID",
    "GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID",
    "GMAIL_MESSAGE_ID_FIELD_ID",
    "GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS",
    "GMAIL_SENDER_FIELD_ID",
    "GMAIL_SNIPPET_FIELD_ID",
    "GMAIL_SUBJECT_FIELD_ID",
    "GMAIL_TOOL_EVIDENCE_CONTRACT_CONCEPT_SPECS",
    "GMAIL_TOOL_EVIDENCE_CONTRACT_ID",
    "GMAIL_TOOL_EVIDENCE_CONTRACT_SCHEMA_VERSION",
    "bootstrap_gmail_tool_evidence_contract",
    "canonical_gmail_tool_evidence_contract_concept_ids",
    "canonical_gmail_tool_evidence_contract_relationships",
    "validate_gmail_tool_evidence_contract",
]