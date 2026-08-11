from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services import (
    gmail_tool_evidence_contract_vontology_service as service,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield


def _relationships(concept_id: str) -> dict[str, Any]:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    return dict(concept_doc.get("relationships") or {})


def _targets(concept_id: str, predicate: str) -> set[str]:
    values = _relationships(concept_id).get(predicate) or []
    if isinstance(values, str):
        return {values}
    return {str(value) for value in values}


def _attributes(concept_id: str) -> dict[str, Any]:
    concept_doc = concept_service.get_concept_by_concept_id(concept_id)
    assert concept_doc is not None
    return dict(concept_doc.get("attributes") or {})


def test_bootstrap_materialises_gmail_tool_contract_graph_kr(
    _reset_mock_db: Any,
) -> None:
    report = service.bootstrap_gmail_tool_evidence_contract()

    assert report["success"] is True
    assert report["schema_version"] == "gmail_tool_evidence_contract.v1"
    assert report["contract_concept_id"] == "#V#gmail_tool_evidence_contract_v1"
    assert report["vocabulary_report"]["success"] is True
    assert report["counts"]["concept_specs"] >= 40
    assert report["counts"]["relationship_specs"] >= 100
    assert report["errors"] == []

    contract_tools = _targets(
        service.GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
        "#V#tool_contract_applies_to_tool",
    )
    assert service.GMAIL_LIST_MESSAGES_TOOL_ID in contract_tools
    assert service.GMAIL_GET_MESSAGE_TOOL_ID in contract_tools

    contract_views = _targets(
        service.GMAIL_TOOL_EVIDENCE_CONTRACT_ID,
        "#V#tool_contract_has_evidence_view",
    )
    assert service.GMAIL_FINAL_ANSWER_VIEW_ID in contract_views
    assert service.GMAIL_FOLLOW_UP_VIEW_ID in contract_views

    list_tool_relationships = _relationships(service.GMAIL_LIST_MESSAGES_TOOL_ID)
    assert service.GMAIL_GET_MESSAGE_TOOL_ID in list_tool_relationships.get(
        "#V#list_tool_has_detail_tool",
        [],
    )
    assert service.GMAIL_MESSAGE_ENTITY_TYPE_ID in list_tool_relationships.get(
        "#V#tool_emits_collection_entity_type",
        [],
    )

    detail_tool_relationships = _relationships(service.GMAIL_GET_MESSAGE_TOOL_ID)
    assert service.GMAIL_MESSAGE_ENTITY_TYPE_ID in detail_tool_relationships.get(
        "#V#tool_emits_entity_type",
        [],
    )
    assert service.GMAIL_ATTACHMENT_ENTITY_TYPE_ID in detail_tool_relationships.get(
        "#V#tool_emits_collection_entity_type",
        [],
    )
    assert service.GMAIL_MESSAGE_ID_FIELD_ID in detail_tool_relationships.get(
        "#V#detail_tool_accepts_identifier_field",
        [],
    )

    list_tool_attributes = _attributes(service.GMAIL_LIST_MESSAGES_TOOL_ID)
    assert list_tool_attributes["mcp_tool_name"] == "gmail_list_messages"
    assert list_tool_attributes["category"] == "gmail"
    assert list_tool_attributes["dispatch_surface_family"] == "gmail"
    assert list_tool_attributes["evidence_surface_family"] == "gmail"
    assert list_tool_attributes["external_surface"] is True
    assert list_tool_attributes["operation_category"] == "read"
    assert list_tool_attributes["evidence_role"] == "search"

    detail_tool_attributes = _attributes(service.GMAIL_GET_MESSAGE_TOOL_ID)
    assert detail_tool_attributes["mcp_tool_name"] == "gmail_get_message"
    assert detail_tool_attributes["category"] == "gmail"
    assert detail_tool_attributes["dispatch_surface_family"] == "gmail"
    assert detail_tool_attributes["evidence_surface_family"] == "gmail"
    assert detail_tool_attributes["external_surface"] is True
    assert detail_tool_attributes["operation_category"] == "read"
    assert detail_tool_attributes["evidence_role"] == "verification"

    validation = service.validate_gmail_tool_evidence_contract()
    assert validation["success"] is True
    assert validation["missing_concept_ids"] == []
    assert validation["missing_relationships"] == []


def test_conversation_turn_support_bootstrap_materialises_gmail_contract(
    _reset_mock_db: Any,
) -> None:
    from src.backend.services.conversation_turn_workflow_vontology_service import (
        _ensure_conversation_turn_prompt_support,
    )

    report = _ensure_conversation_turn_prompt_support(
        ensure_tool_evidence_contracts=True
    )

    gmail_bootstrap = report["support_bootstraps"]["gmail_tool_evidence_contract"]
    assert gmail_bootstrap["success"] is True
    assert gmail_bootstrap["validation"]["success"] is True
    assert (
        concept_service.get_concept_by_concept_id(
            service.GMAIL_TOOL_EVIDENCE_CONTRACT_ID
        )
        is not None
    )


def test_gmail_final_answer_view_requires_detail_completed_message_fields(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_gmail_tool_evidence_contract()

    required_fields = _targets(
        service.GMAIL_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_requires_field",
    )
    assert required_fields == set(service.GMAIL_REQUIRED_FINAL_ANSWER_FIELD_IDS)
    assert required_fields == {
        service.GMAIL_SENDER_FIELD_ID,
        service.GMAIL_SUBJECT_FIELD_ID,
        service.GMAIL_DATE_FIELD_ID,
        service.GMAIL_SNIPPET_FIELD_ID,
    }

    included_fields = _targets(
        service.GMAIL_FINAL_ANSWER_VIEW_ID,
        "#V#evidence_view_includes_field",
    )
    assert service.GMAIL_BODY_FIELD_ID in included_fields
    assert service.GMAIL_BODY_TRUNCATED_FIELD_ID in included_fields
    assert {
        service.GMAIL_ATTACHMENTS_COLLECTION_FIELD_ID,
        service.GMAIL_ATTACHMENT_ID_FIELD_ID,
        service.GMAIL_ATTACHMENT_FILENAME_FIELD_ID,
        service.GMAIL_ATTACHMENT_CONTENT_TYPE_FIELD_ID,
        service.GMAIL_ATTACHMENT_SIZE_BYTES_FIELD_ID,
        service.GMAIL_ATTACHMENT_COUNT_FIELD_ID,
        service.GMAIL_ATTACHMENTS_TRUNCATED_FIELD_ID,
    }.issubset(included_fields)

    completed_fields = _targets(
        service.GMAIL_GET_MESSAGE_TOOL_ID,
        "#V#detail_tool_completes_field",
    )
    assert required_fields.issubset(completed_fields)

    preserved_detail_fields = _targets(
        service.GMAIL_GET_MESSAGE_TOOL_ID,
        "#V#tool_result_preserves_field",
    )
    assert required_fields.issubset(preserved_detail_fields)
    assert service.GMAIL_BODY_FIELD_ID in preserved_detail_fields
    assert service.GMAIL_BODY_TRUNCATED_FIELD_ID in preserved_detail_fields

    detail_output_fields = _targets(
        service.GMAIL_GET_MESSAGE_TOOL_ID,
        "#V#tool_has_output_field",
    )
    assert service.GMAIL_PAYLOAD_FIELD_ID not in detail_output_fields
    assert service.GMAIL_ATTACHMENTS_COLLECTION_FIELD_ID in detail_output_fields

    message_entity_fields = _targets(
        service.GMAIL_MESSAGE_ENTITY_TYPE_ID,
        "#V#entity_type_has_tool_field",
    )
    assert service.GMAIL_PAYLOAD_FIELD_ID not in message_entity_fields
    attachment_entity_fields = _targets(
        service.GMAIL_ATTACHMENT_ENTITY_TYPE_ID,
        "#V#entity_type_has_tool_field",
    )
    assert attachment_entity_fields == {
        service.GMAIL_ATTACHMENT_ID_FIELD_ID,
        service.GMAIL_ATTACHMENT_FILENAME_FIELD_ID,
        service.GMAIL_ATTACHMENT_CONTENT_TYPE_FIELD_ID,
        service.GMAIL_ATTACHMENT_SIZE_BYTES_FIELD_ID,
    }


def test_gmail_list_detail_affordance_maps_list_identifier_to_detail_argument(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_gmail_tool_evidence_contract()

    affordance_relationships = _relationships(service.GMAIL_LIST_DETAIL_AFFORDANCE_ID)
    assert service.GMAIL_LIST_MESSAGES_TOOL_ID in affordance_relationships.get(
        "#V#list_detail_affordance_has_list_tool",
        [],
    )
    assert service.GMAIL_GET_MESSAGE_TOOL_ID in affordance_relationships.get(
        "#V#list_detail_affordance_has_detail_tool",
        [],
    )
    assert service.GMAIL_MESSAGE_ID_FIELD_ID in affordance_relationships.get(
        "#V#list_detail_affordance_uses_identifier_field",
        [],
    )

    message_id_argument_relationships = _relationships(
        service.GMAIL_MESSAGE_ID_ARGUMENT_FIELD_ID
    )
    assert service.GMAIL_MESSAGE_ID_FIELD_ID in message_id_argument_relationships.get(
        "#V#tool_argument_maps_from_field",
        [],
    )

    list_preserved_fields = _targets(
        service.GMAIL_LIST_MESSAGES_TOOL_ID,
        "#V#tool_result_preserves_field",
    )
    assert {
        service.GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        service.GMAIL_MESSAGE_ID_FIELD_ID,
    }.issubset(list_preserved_fields)

    follow_up_required_fields = _targets(
        service.GMAIL_FOLLOW_UP_VIEW_ID,
        "#V#evidence_view_requires_field",
    )
    assert {
        service.GMAIL_PROFILE_ARGUMENT_FIELD_ID,
        service.GMAIL_MESSAGE_ID_FIELD_ID,
    }.issubset(follow_up_required_fields)


def test_gmail_fields_record_wire_aliases_and_payload_paths_as_concepts(
    _reset_mock_db: Any,
) -> None:
    service.bootstrap_gmail_tool_evidence_contract()

    sender_aliases = _targets(service.GMAIL_SENDER_FIELD_ID, "#V#field_has_wire_alias")
    assert "#V#gmail_wire_key_sender" in sender_aliases
    assert "#V#gmail_wire_key_from" in sender_aliases

    sender_paths = _targets(
        service.GMAIL_SENDER_FIELD_ID,
        "#V#field_extracts_from_payload_path",
    )
    assert "#V#gmail_payload_path_sender" in sender_paths
    assert "#V#gmail_payload_path_from" in sender_paths
    assert "#V#gmail_payload_path_header_from" in sender_paths

    message_id_aliases = _targets(
        service.GMAIL_MESSAGE_ID_FIELD_ID,
        "#V#field_has_wire_alias",
    )
    assert "#V#gmail_wire_key_message_id" in message_id_aliases
    assert "#V#gmail_wire_key_id" in message_id_aliases

    message_id_paths = _targets(
        service.GMAIL_MESSAGE_ID_FIELD_ID,
        "#V#field_extracts_from_payload_path",
    )
    assert "#V#gmail_payload_path_messages_message_id" in message_id_paths
    assert "#V#gmail_payload_path_messages_id" in message_id_paths


def test_gmail_contract_does_not_use_json_text_relation_contracts() -> None:
    concept_ids = service.canonical_gmail_tool_evidence_contract_concept_ids()
    relationship_specs = service.canonical_gmail_tool_evidence_contract_relationships()

    assert all("json" not in concept_id.lower() for concept_id in concept_ids)
    assert all("json" not in spec.predicate.lower() for spec in relationship_specs)
    assert all(spec.predicate != "hasText" for spec in relationship_specs)


def test_bootstrap_removes_obsolete_raw_payload_relationships(
    _reset_mock_db: Any,
) -> None:
    from src.backend.services.relationship_write_service import add_relationship

    first_report = service.bootstrap_gmail_tool_evidence_contract()
    assert first_report["success"] is True

    obsolete_sources = (
        (service.GMAIL_GET_MESSAGE_TOOL_ID, "#V#tool_has_output_field"),
        (service.GMAIL_GET_MESSAGE_TOOL_ID, "#V#detail_tool_completes_field"),
        (service.GMAIL_MESSAGE_ENTITY_TYPE_ID, "#V#entity_type_has_tool_field"),
        (service.GMAIL_USER_DISPLAY_VIEW_ID, "#V#evidence_view_redacts_field"),
    )
    for source_id, predicate in obsolete_sources:
        result = add_relationship(
            source_id=source_id,
            predicate=predicate,
            target=service.GMAIL_PAYLOAD_FIELD_ID,
        )
        assert result.get("success") is True

    second_report = service.bootstrap_gmail_tool_evidence_contract()

    assert second_report["success"] is True
    assert second_report["removed_obsolete_payload_relationship_count"] == len(
        obsolete_sources
    )
    assert second_report["validation"]["obsolete_payload_relationships"] == []
    for source_id, predicate in obsolete_sources:
        assert service.GMAIL_PAYLOAD_FIELD_ID not in _targets(source_id, predicate)
