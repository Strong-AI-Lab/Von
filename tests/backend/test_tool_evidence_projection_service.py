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
    project_nested_workflow_progress_evidence,
    project_surfaceable_concept_evidence,
    project_tool_payload_for_llm,
    render_surfaceable_concept_lines,
    resolve_tool_projection_contract,
    surfaceable_concept_ids_from_evidence,
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


def test_projection_runtime_uses_supplied_turn_snapshot_without_reresolving(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _materialise_mock_projection_contract()
    contract = resolve_tool_projection_contract("example_projection_tool")
    assert contract is not None

    def fail_if_resolved(_tool_name: str) -> None:
        raise AssertionError("supplied turn snapshot must bypass live resolution")

    monkeypatch.setattr(
        "src.backend.services.tool_evidence_projection_service."
        "resolve_tool_projection_contract",
        fail_if_resolved,
    )

    projected = project_tool_payload_for_llm(
        "example_projection_tool",
        {"headline": "Snapshot projection works"},
        contract=contract,
    )

    assert projected is not None
    assert projected["title"] == "Snapshot projection works"


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
            "body": "The explicitly requested useful body.",
            "body_truncated": False,
            "payload": {"headers": [], "parts": [{"body": "large" * 100}]},
        },
    )

    assert projected is not None
    assert projected["message_id"] == "msg-1"
    assert projected["sender"] == "sender@example.org"
    assert projected["subject"] == "A useful message"
    assert projected["date"] == "Fri, 8 May 2026 12:00:00 +0000"
    assert projected["snippet"] == "The short useful description."
    assert projected["body"] == "The explicitly requested useful body."
    assert projected["body_truncated"] is False
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
        "#V#gmail_body_field",
        "#V#gmail_body_truncated_field",
    }.issubset(preserved_ids)
    assert telemetry["missing_required_fields"] == []


def test_gmail_detail_projection_preserves_exact_attachment_handle_without_raw_part_data(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()
    opaque_attachment_id = "opaque-" + ("x" * 700)

    projected = project_tool_payload_for_llm(
        "gmail_get_message",
        {
            "message_id": "message-easyjet",
            "threadId": "thread-easyjet",
            "sender": "easyJet <noreply@easyjet.com>",
            "subject": "easyJet booking KD5BJTT",
            "date": "Mon, 10 Aug 2026 12:00:00 +0000",
            "snippet": "Your booking is confirmed.",
            "attachments": [
                {
                    "attachment_id": opaque_attachment_id,
                    "filename": "itinerary.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 42_000,
                    "body": {"data": "raw-attachment-base64-must-not-survive"},
                }
            ],
            "attachment_count": 1,
            "attachments_truncated": False,
            "payload": {
                "parts": [
                    {"body": {"data": "raw-message-base64-must-not-survive"}}
                ]
            },
        },
    )

    assert projected is not None
    assert projected["attachments"] == [
        {
            "attachment_id": opaque_attachment_id,
            "filename": "itinerary.pdf",
            "content_type": "application/pdf",
            "size_bytes": 42_000,
        }
    ]
    assert projected["attachment_count"] == 1
    assert projected["attachments_truncated"] is False
    assert "payload" not in projected
    assert "raw-attachment-base64-must-not-survive" not in repr(projected)
    assert "raw-message-base64-must-not-survive" not in repr(projected)

    referents = projected["_tool_evidence_projection"]["referent_candidates"]
    assert referents == [
        {
            "schema_version": "tool_referent_candidate.v1",
            "stable_id": "message-easyjet",
            "display_label": "easyJet booking KD5BJTT",
            "source_kind": "#V#gmail_message_result_entity_type",
            "identity_field_concept_id": "#V#gmail_message_id_field",
        },
        {
            "schema_version": "tool_referent_candidate.v1",
            "stable_id": opaque_attachment_id,
            "display_label": "itinerary.pdf",
            "source_kind": "#V#gmail_attachment_result_entity_type",
            "identity_field_concept_id": "#V#gmail_attachment_id_field",
        },
    ]


def test_over_limit_referent_identity_is_omitted_instead_of_truncated(
    _reset_mock_db: Any,
) -> None:
    from src.backend.services.selected_referent_contract import (
        MAX_EXACT_REFERENT_ID_CHARS,
    )

    bootstrap_gmail_tool_evidence_contract()
    oversized_attachment_id = "a" * (MAX_EXACT_REFERENT_ID_CHARS + 1)

    projected = project_tool_payload_for_llm(
        "gmail_get_message",
        {
            "message_id": "message-1",
            "sender": "sender@example.test",
            "subject": "One attachment",
            "date": "Mon, 10 Aug 2026 12:00:00 +0000",
            "snippet": "Attachment included.",
            "attachments": [
                {
                    "attachment_id": oversized_attachment_id,
                    "filename": "oversized-id.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": 1,
                }
            ],
        },
    )

    assert projected is not None
    assert projected["attachments"][0]["attachment_id"] == oversized_attachment_id
    referents = projected["_tool_evidence_projection"]["referent_candidates"]
    assert [candidate["stable_id"] for candidate in referents] == ["message-1"]
    assert not any(
        candidate["stable_id"] == oversized_attachment_id[:MAX_EXACT_REFERENT_ID_CHARS]
        for candidate in referents
    )


def test_represented_field_roles_project_a_bounded_gmail_referent_candidate(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()

    projected = project_tool_payload_for_llm(
        "gmail_get_message",
        {
            "message_id": "19fece69a5839e69",
            "threadId": "19fece69a5839e69",
            "sender": "easyJet <noreply@easyjet.com>",
            "subject": "easyJet booking KD5BJTT",
            "date": "Mon, 10 Aug 2026 12:00:00 +0000",
            "snippet": "Private snippet that is not a referent label.",
            "body": "Private message body that must not enter continuity.",
        },
    )

    assert projected is not None
    telemetry = projected["_tool_evidence_projection"]
    assert telemetry["entity_type_concept_ids"] == [
        "#V#gmail_message_result_entity_type",
        "#V#gmail_attachment_result_entity_type",
    ]
    assert telemetry["referent_candidates"] == [
        {
            "schema_version": "tool_referent_candidate.v1",
            "stable_id": "19fece69a5839e69",
            "display_label": "easyJet booking KD5BJTT",
            "source_kind": "#V#gmail_message_result_entity_type",
            "identity_field_concept_id": "#V#gmail_message_id_field",
        }
    ]
    assert "Private snippet" not in str(telemetry["referent_candidates"])
    assert "Private message body" not in str(telemetry["referent_candidates"])


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


def test_surfaceable_concept_projection_collects_nested_workflow_outputs() -> None:
    evidence = project_surfaceable_concept_evidence(
        {
            "workflow_execution_summary": {
                "result": {
                    "successful_results": [
                        {
                            "arxiv_id": "2402.18144",
                            "paper_concept_id": "#V#paper_on_arxiv_2402_18144_c100899e",
                            "file_copy_concept_id": "#V#file_copy_arxiv_2402_18144_c100899e",
                        },
                        {
                            "arxiv_id": "2603.24621",
                            "paper_concept_id": "#V#paper_on_arxiv_2603_24621_eb7a21c4",
                        },
                    ]
                }
            },
            "search_results": [
                {"concept_id": "#V#ordinary_search_hit", "name": "Search hit"}
            ],
            "user_concept_id": "#V#user_should_not_surface",
        }
    )

    assert surfaceable_concept_ids_from_evidence(evidence) == [
        "#V#paper_on_arxiv_2402_18144_c100899e",
        "#V#file_copy_arxiv_2402_18144_c100899e",
        "#V#paper_on_arxiv_2603_24621_eb7a21c4",
    ]
    assert render_surfaceable_concept_lines(evidence)[:2] == [
        "Paper concept: #V#paper_on_arxiv_2402_18144_c100899e.",
        "File copy concept: #V#file_copy_arxiv_2402_18144_c100899e.",
    ]


def test_surfaceable_concept_projection_preserves_generic_verified_handles() -> None:
    evidence = project_surfaceable_concept_evidence(
        {
            "result": {
                "dataset_concept_id": "#V#symmetry_dataset_representation",
                "dataset_verified": True,
                "verification_failures": [],
            }
        }
    )

    assert evidence == [
        {
            "concept_id": "#V#symmetry_dataset_representation",
            "source_key": "dataset_concept_id",
            "source_path": "result.dataset_concept_id",
            "artefact_type": "dataset_concept",
            "verified": True,
            "verification_key": "dataset_verified",
            "verification_status": "verified",
            "verification_failure_key": "verification_failures",
            "verification_failure_count": 0,
        }
    ]
    assert render_surfaceable_concept_lines(
        evidence,
        include_source_paths=True,
        include_verification_status=True,
    ) == [
        "Verified dataset concept: #V#symmetry_dataset_representation "
        "(verified=true; verification_key=dataset_verified; "
        "verification_failures=0; source=result.dataset_concept_id)."
    ]


def test_nested_workflow_progress_projection_uses_represented_facts_only() -> None:
    evidence = project_nested_workflow_progress_evidence(
        {
            "iteration_results": [
                {
                    "last_action_outputs": {
                        "paper_concept_id": "#V#should_not_be_inferred",
                        "progress_facts": [
                            {
                                "schema_version": "workflow_progress_projection.v1",
                                "fact_id": "represented_readback",
                                "label": "Represented read-back",
                                "status": "available",
                                "present": True,
                                "value": "#V#paper_nested",
                                "value_kind": "concept_id",
                                "workflow_id": "#V#represented_workflow",
                                "state_id": "read_back",
                                "action_id": "verify",
                                "contract_id": "#V#represented_readback_fact",
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert evidence is not None
    assert evidence["schema_version"] == "nested_workflow_progress_evidence.v1"
    assert evidence["contract_ids"] == ["#V#represented_readback_fact"]
    assert evidence["facts"] == [
        {
            "schema_version": "workflow_progress_projection.v1",
            "fact_id": "represented_readback",
            "label": "Represented read-back",
            "status": "available",
            "present": True,
            "redacted": False,
            "truncated": False,
            "source_path": "iteration_results.0.last_action_outputs.progress_facts",
            "payload_source_path": "iteration_results.0.last_action_outputs.progress_facts",
            "value_kind": "concept_id",
            "workflow_id": "#V#represented_workflow",
            "state_id": "read_back",
            "action_id": "verify",
            "contract_id": "#V#represented_readback_fact",
            "value": "#V#paper_nested",
        }
    ]
    assert "#V#should_not_be_inferred" not in json.dumps(evidence, sort_keys=True)


def test_nested_workflow_progress_projection_fails_closed_without_represented_facts() -> None:
    evidence = project_nested_workflow_progress_evidence(
        {
            "iteration_results": [
                {
                    "last_action_outputs": {
                        "paper_concept_id": "#V#should_not_be_inferred",
                        "scholarly_representation_verified": True,
                    }
                }
            ]
        }
    )

    assert evidence is not None
    assert evidence["facts"] == []
    assert evidence["contract_ids"] == []
    assert evidence["workflow_evidence_seen"] is True


def test_surfaceable_concept_projection_does_not_treat_search_hits_as_created() -> None:
    evidence = project_surfaceable_concept_evidence(
        {
            "results": [
                {
                    "success": True,
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool Calling Workflow",
                    "kind": "individual",
                },
                {
                    "concept_id": "#V#scholarly_article",
                    "name": "Scholarly Article",
                    "kind": "type",
                },
            ],
            "total_count": 2,
        }
    )

    assert evidence == []
    assert render_surfaceable_concept_lines(evidence) == []


def test_surfaceable_concept_projection_uses_explicit_created_metadata() -> None:
    evidence = project_surfaceable_concept_evidence(
        {
            "created_concept_ids": ["#V#new_workflow_marker"],
            "results": [
                {
                    "success": True,
                    "concept_id": "#V#used_support_type",
                    "name": "Used support type",
                }
            ],
        }
    )

    assert surfaceable_concept_ids_from_evidence(evidence) == ["#V#new_workflow_marker"]
    assert render_surfaceable_concept_lines(evidence) == [
        "Created concept: #V#new_workflow_marker."
    ]


def test_orchestrator_format_preserves_surfaceable_concepts_when_payload_truncates() -> (
    None
):
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, object()),
        max_tool_invocations=1,
        max_tool_result_chars=900,
        max_tool_result_field_chars=30_000,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "workflow_execute",
        {
            "content": "x" * 20_000,
            "result": {
                "successful_results": [
                    {
                        "paper_concept_id": "#V#paper_on_arxiv_2603_24621_eb7a21c4",
                        "file_copy_concept_id": "#V#file_copy_arxiv_2603_24621_eb7a21c4",
                    }
                ]
            },
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    payload = parsed["payload"]
    assert payload["_truncated"] is True
    concept_ids = surfaceable_concept_ids_from_evidence(
        payload["_surfaceable_concepts"]
    )
    assert concept_ids == [
        "#V#paper_on_arxiv_2603_24621_eb7a21c4",
        "#V#file_copy_arxiv_2603_24621_eb7a21c4",
    ]


def test_orchestrator_format_does_not_surface_search_concepts_as_created() -> None:
    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, object()),
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    encoded = orchestrator._format_tool_result(
        "search_concepts",
        {
            "success": True,
            "results": [
                {
                    "concept_id": "#V#tool_calling_workflow",
                    "name": "Tool Calling Workflow",
                    "kind": "individual",
                },
                {
                    "concept_id": "#V#scholarly_article",
                    "name": "Scholarly Article",
                    "kind": "type",
                },
            ],
            "total_count": 2,
        },
        1.0,
        "ok",
    )

    parsed = json.loads(encoded)
    assert parsed["tool"] == "search_concepts"
    payload = parsed["payload"]
    assert "_surfaceable_concepts" not in payload
    assert "Tool Calling Workflow" in json.dumps(payload, default=str)


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
