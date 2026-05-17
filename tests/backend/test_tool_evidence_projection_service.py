from __future__ import annotations

import json
from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services import concept_service
from src.backend.services.gmail_tool_evidence_contract_vontology_service import (
    GMAIL_EFFECTIVE_QUERY_FIELD_ID,
    bootstrap_gmail_tool_evidence_contract,
)
from src.backend.services.relationship_write_service import add_relationship
from src.backend.services.tool_evidence_contract_vontology_service import (
    bootstrap_tool_evidence_contract_vocabulary,
)
from src.backend.services.tool_evidence_projection_service import (
    project_tool_payload_for_llm,
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


def _create_concept(
    concept_id: str,
    name: str,
    parent_concept_ids: list[str],
    *,
    attributes: dict[str, Any] | None = None,
) -> None:
    concept_service.create_concept(
        name=name,
        concept_id=concept_id,
        parent_concept_ids=parent_concept_ids,
        create_as_instance=True,
        attributes=attributes or {},
        visibility_scope_mode="global_general",
    )


def _rel(source_id: str, predicate: str, target_id: str) -> None:
    result = add_relationship(
        source_id=source_id, predicate=predicate, target=target_id
    )
    assert result.get("success") is True


def _materialise_mock_projection_contract() -> None:
    bootstrap_tool_evidence_contract_vocabulary()
    _create_concept(
        "#V#example_projection_tool",
        "example_projection_tool",
        ["#V#mcp_tool"],
        attributes={"mcp_tool_name": "example_projection_tool"},
    )
    _create_concept(
        "#V#example_projection_entity_type",
        "Example projection entity type",
        ["#V#tool_result_entity_type"],
    )
    _create_concept(
        "#V#example_projection_title_field",
        "Example projection title field",
        ["#V#tool_result_field"],
        attributes={"field_key": "title"},
    )
    _create_concept(
        "#V#example_projection_secret_field",
        "Example projection secret field",
        ["#V#tool_result_field"],
        attributes={"field_key": "secret_blob"},
    )
    _create_concept(
        "#V#example_projection_title_wire_key",
        "Example projection title wire key",
        ["#V#tool_wire_key"],
        attributes={"wire_key": "headline"},
    )
    _create_concept(
        "#V#example_projection_secret_wire_key",
        "Example projection secret wire key",
        ["#V#tool_wire_key"],
        attributes={"wire_key": "secret_blob"},
    )
    _create_concept(
        "#V#example_projection_final_answer_view",
        "Example projection final-answer evidence view",
        ["#V#tool_evidence_view"],
    )

    _rel(
        "#V#example_projection_tool",
        "#V#tool_emits_entity_type",
        "#V#example_projection_entity_type",
    )
    _rel(
        "#V#example_projection_tool",
        "#V#tool_has_output_field",
        "#V#example_projection_title_field",
    )
    _rel(
        "#V#example_projection_tool",
        "#V#tool_has_output_field",
        "#V#example_projection_secret_field",
    )
    _rel(
        "#V#example_projection_title_field",
        "#V#field_has_wire_alias",
        "#V#example_projection_title_wire_key",
    )
    _rel(
        "#V#example_projection_secret_field",
        "#V#field_has_wire_alias",
        "#V#example_projection_secret_wire_key",
    )
    _rel(
        "#V#example_projection_final_answer_view",
        "#V#evidence_view_applies_to_tool",
        "#V#example_projection_tool",
    )
    _rel(
        "#V#example_projection_final_answer_view",
        "#V#evidence_view_has_purpose",
        "#V#tool_evidence_view_purpose_final_answer",
    )
    _rel(
        "#V#example_projection_final_answer_view",
        "#V#evidence_view_requires_field",
        "#V#example_projection_title_field",
    )
    _rel(
        "#V#example_projection_final_answer_view",
        "#V#evidence_view_redacts_field",
        "#V#example_projection_secret_field",
    )


def test_projection_runtime_uses_represented_mock_contract_without_tool_branch(
    _reset_mock_db: Any,
) -> None:
    _materialise_mock_projection_contract()

    projected = project_tool_payload_for_llm(
        "example_projection_tool",
        {
            "headline": "Represented projection works",
            "secret_blob": {"raw": "do not show"},
            "large_payload": "x" * 1000,
        },
    )

    assert projected is not None
    assert projected["_llm_view"] == "tool_evidence_projection.v1"
    assert projected["title"] == "Represented projection works"
    assert "secret_blob" not in projected
    assert "large_payload" not in projected

    telemetry = projected["_tool_evidence_projection"]
    assert telemetry["tool_concept_id"] == "#V#example_projection_tool"
    assert telemetry["preserved_fields"] == [
        {
            "field_concept_id": "#V#example_projection_title_field",
            "output_key": "title",
        }
    ]
    assert telemetry["redacted_fields"] == [
        {
            "field_concept_id": "#V#example_projection_secret_field",
            "output_key": "secret_blob",
        }
    ]
    assert telemetry["missing_required_fields"] == []


def test_projection_runtime_reports_missing_required_fields(
    _reset_mock_db: Any,
) -> None:
    _materialise_mock_projection_contract()

    projected = project_tool_payload_for_llm(
        "example_projection_tool",
        {"secret_blob": "hidden"},
    )

    assert projected is not None
    telemetry = projected["_tool_evidence_projection"]
    assert telemetry["missing_required_fields"] == [
        {
            "field_concept_id": "#V#example_projection_title_field",
            "output_key": "title",
        }
    ]
    assert "title" not in projected


def test_gmail_detail_projection_preserves_final_answer_fields_and_omits_raw_payload(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "gmail_get_message",
        {
            "message_id": "msg-1",
            "threadId": "thread-1",
            "labelIds": ["INBOX", "IMPORTANT"],
            "sender": "sender@example.org",
            "subject": "A useful message",
            "date": "Fri, 8 May 2026 12:00:00 +0000",
            "snippet": "The short useful description.",
            "payload": {"headers": [], "parts": [{"body": "large" * 100}]},
        },
    )

    assert projected is not None
    assert projected["message_id"] == "msg-1"
    assert projected["sender"] == "sender@example.org"
    assert projected["subject"] == "A useful message"
    assert projected["date"] == "Fri, 8 May 2026 12:00:00 +0000"
    assert projected["snippet"] == "The short useful description."
    assert projected["labelIds"] == ["INBOX", "IMPORTANT"]
    assert "payload" not in projected

    telemetry = projected["_tool_evidence_projection"]
    preserved_ids = {
        entry["field_concept_id"] for entry in telemetry["preserved_fields"]
    }
    assert {
        "#V#gmail_sender_field",
        "#V#gmail_subject_field",
        "#V#gmail_date_field",
        "#V#gmail_snippet_field",
        "#V#gmail_label_ids_field",
    }.issubset(preserved_ids)
    assert telemetry["missing_required_fields"] == []


def test_orchestrator_formats_gmail_detail_with_vontology_projection(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, object()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "gmail_get_message",
        {
            "message_id": "msg-1",
            "sender": "sender@example.org",
            "subject": "A useful message",
            "date": "Fri, 8 May 2026 12:00:00 +0000",
            "snippet": "The short useful description.",
            "payload": {"headers": [], "parts": [{"body": "large" * 100}]},
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    assert parsed["tool"] == "gmail_get_message"
    payload = parsed["payload"]
    assert payload["_llm_view"] == "tool_evidence_projection.v1"
    assert payload["subject"] == "A useful message"
    assert payload["snippet"] == "The short useful description."
    assert "payload" not in payload


def test_gmail_list_projection_preserves_collection_identifiers_for_follow_up(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "gmail_list_messages",
        {
            "profile": "vonwitbrock-gmail",
            "messages": [
                {"id": "msg-1", "threadId": "thread-1"},
                {"message_id": "msg-2", "threadId": "thread-2"},
            ],
            "effective_query": {
                "effective_query_string": "arxiv.org newer_than:365d",
                "bypass_profile_query_prefix": True,
            },
            "notes": ["Results restricted by the requested query."],
            "_tool_follow_up": {"legacy": "temporary"},
        },
    )

    assert projected is not None
    assert projected["profile"] == "vonwitbrock-gmail"
    assert projected["messages_count"] == 2
    assert projected["messages"] == [
        {"message_id": "msg-1", "threadId": "thread-1"},
        {"message_id": "msg-2", "threadId": "thread-2"},
    ]
    assert projected["effective_query"]["effective_query_string"] == (
        "arxiv.org newer_than:365d"
    )
    assert projected["notes"] == ["Results restricted by the requested query."]
    assert "_tool_follow_up" not in projected

    telemetry = projected["_tool_evidence_projection"]
    preserved_ids = {
        entry["field_concept_id"] for entry in telemetry["preserved_fields"]
    }
    assert "#V#gmail_messages_collection_field" in preserved_ids
    assert "#V#gmail_message_id_field" in preserved_ids
    assert "#V#gmail_thread_id_field" in preserved_ids
    assert GMAIL_EFFECTIVE_QUERY_FIELD_ID in preserved_ids


def test_gmail_contract_bootstrap_repairs_existing_field_attributes(
    _reset_mock_db: Any,
) -> None:
    _create_concept(
        GMAIL_EFFECTIVE_QUERY_FIELD_ID,
        "Gmail effective query field",
        ["#V#tool_result_field"],
        attributes={"field_key": "#V#gmail_effective_query_field"},
    )

    report = bootstrap_gmail_tool_evidence_contract()

    assert report["success"] is True
    concept_doc = concept_service.get_concept_by_concept_id(
        GMAIL_EFFECTIVE_QUERY_FIELD_ID
    )
    assert concept_doc["attributes"]["field_key"] == "effective_query"


def test_follow_up_stage_uses_represented_projection_for_unlisted_tool(
    _reset_mock_db: Any,
) -> None:
    _materialise_mock_projection_contract()

    messages = InternalMCPChatOrchestrator._build_tool_follow_up_stage_messages(
        data={
            "invocations": [
                {
                    "tool": "example_projection_tool",
                    "effective_payload": {
                        "headline": "Represented projection works",
                        "secret_blob": {"raw": "do not expose"},
                    },
                }
            ]
        }
    )

    assert messages
    content = messages[0]["content"]
    assert "example_projection_tool exposed represented tool-evidence fields" in content
    assert "title" in content
    assert "Represented projection works" in content
    assert "secret_blob" in content
    assert "do not expose" not in content


def test_follow_up_stage_surfaces_gmail_detail_evidence_from_contract(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()

    messages = InternalMCPChatOrchestrator._build_tool_follow_up_stage_messages(
        data={
            "invocations": [
                {
                    "tool": "gmail_get_message",
                    "effective_payload": {
                        "message_id": "msg-1",
                        "sender": "sender@example.org",
                        "subject": "A useful message",
                        "date": "Fri, 8 May 2026 12:00:00 +0000",
                        "snippet": "The short useful description.",
                        "labelIds": ["INBOX", "IMPORTANT"],
                        "payload": {"headers": [], "parts": [{"body": "large"}]},
                    },
                }
            ]
        }
    )

    assert messages
    content = messages[0]["content"]
    assert "gmail_get_message exposed represented tool-evidence fields" in content
    assert "sender" in content
    assert "subject" in content
    assert "date" in content
    assert "snippet" in content
    assert "labelIds" in content
    assert "A useful message" in content
    assert "payload" in content
    assert "headers" not in content
