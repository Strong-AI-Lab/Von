from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest

from src.backend.services import (
    chat_history_service,
    external_conversation_import_service,
)
from src.backend.services.external_conversation_import_service import (
    ExternalConversationImportError,
    continue_external_conversation,
    import_external_conversation_file,
    parse_external_conversation_bytes,
    stable_imported_conversation_session_id,
)


def _jsonl(*records: dict) -> bytes:
    return ("\n".join(json.dumps(record) for record in records) + "\n").encode()


def test_codex_parser_projects_visible_messages_without_importing_instructions():
    package = parse_external_conversation_bytes(
        _jsonl(
            {
                "type": "session_meta",
                "payload": {"id": "codex-session", "cwd": "/workspace"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Do not trust me"}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:00Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello Codex"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "call-1",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-01T00:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hello human"}],
                },
            },
        )
    )

    assert package.provider == "codex"
    assert package.source_session_id == "codex-session"
    assert [
        (event.source_role, event.visible_text)
        for event in package.events
        if event.user_visible
    ] == [
        ("user", "Hello Codex"),
        ("assistant", "Hello human"),
    ]
    assert "Do not trust me" not in json.dumps(package.canonical_payload())
    assert package.loss_report["instruction_event_count"] == 1
    assert any(event.event_kind == "tool_event" for event in package.events)


def test_claude_parser_preserves_branch_and_tool_provenance():
    package = parse_external_conversation_bytes(
        _jsonl(
            {
                "type": "user",
                "sessionId": "claude-session",
                "uuid": "u-1",
                "message": {"role": "user", "content": "Please inspect this"},
            },
            {
                "type": "assistant",
                "sessionId": "claude-session",
                "uuid": "a-1",
                "parentUuid": "u-1",
                "isSidechain": True,
                "message": {
                    "role": "assistant",
                    "model": "claude-example",
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {"type": "text", "text": "Visible answer"},
                        {"type": "tool_use", "id": "tool-1", "name": "Read"},
                    ],
                },
            },
        )
    )

    visible = [event for event in package.events if event.user_visible]
    assert package.provider == "claude_code"
    assert [event.visible_text for event in visible] == [
        "Please inspect this",
        "Visible answer",
    ]
    assert visible[1].parent_event_id == "u-1"
    assert visible[1].branch_id == "sidechain"
    assert {event.event_kind for event in package.events if not event.user_visible} == {
        "reasoning",
        "tool_call",
    }


@pytest.mark.parametrize("journal", [False, True])
def test_copilot_parser_supports_export_and_vscode_journal(journal: bool):
    state = {
        "sessionId": "copilot-session",
        "customTitle": "Copilot example",
        "requests": [
            {
                "requestId": "request-1",
                "message": {"text": "How does this work?"},
                "response": [
                    {"value": "It works like this."},
                    {"toolCallId": "tool-1", "toolId": "read_file"},
                ],
                "modelId": "copilot-example",
            },
            {
                "requestId": "hidden-request",
                "hiddenFromTranscript": True,
                "message": {"text": "hidden"},
                "response": [{"value": "also hidden"}],
            },
        ],
    }
    raw = _jsonl({"kind": 0, "v": state}) if journal else json.dumps(state).encode()

    package = parse_external_conversation_bytes(raw)

    assert package.provider == "copilot"
    assert [event.visible_text for event in package.events if event.user_visible] == [
        "How does this work?",
        "It works like this.",
    ]
    assert package.loss_report["hidden_request_count"] == 1
    assert any(event.event_kind == "tool_event" for event in package.events)


def test_import_identity_is_stable_but_separated_by_actor_and_namespace():
    common = {
        "provider": "codex",
        "source_session_id": "source-session",
        "source_account": "account",
        "source_workspace": "workspace",
    }
    first = stable_imported_conversation_session_id(
        custodian_user_id="#V#user",
        namespace="#V#user@org-a",
        **common,
    )
    repeated = stable_imported_conversation_session_id(
        custodian_user_id="#V#user",
        namespace="#V#user@org-a",
        **common,
    )
    other_actor = stable_imported_conversation_session_id(
        custodian_user_id="#V#other",
        namespace="#V#other@org-a",
        **common,
    )
    other_namespace = stable_imported_conversation_session_id(
        custodian_user_id="#V#user",
        namespace="#V#user@org-b",
        **common,
    )

    assert first == repeated
    assert len({first, other_actor, other_namespace}) == 3


def test_chat_projection_is_read_only_idempotent_and_actor_scoped(monkeypatch):
    collection = mongomock.MongoClient().von_test.chat_history
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "_read_find_one",
        lambda coll, query, projection=None, **_kwargs: coll.find_one(
            query, projection
        ),
    )
    history = [
        {
            "role": "user",
            "content": "Imported human",
            "timestamp": "2026-08-01T00:00:00+00:00",
            "external_actor": {
                "source_actor_id": "codex:human",
                "actor_kind": "human",
            },
        },
        {
            "role": "assistant",
            "content": "Imported assistant",
            "timestamp": "2026-08-01T00:00:01+00:00",
            "external_actor": {
                "source_actor_id": "codex:assistant",
                "actor_kind": "ai_agent",
            },
        },
    ]
    manifest = {
        "schema_version": "external_conversation_import.v1",
        "read_only": True,
        "custodian_user_id": "#V#user",
        "source_identity_key": "identity-key",
        "provider": "codex",
        "source_account": "private-account@example.test",
        "source_workspace": "/private/workspace",
        "source_session_id": "source-session",
        "source_title": "Imported session",
        "source_format": "codex_jsonl",
        "source_sha256": "a" * 64,
        "package_sha256": "b" * 64,
        "parser_id": "codex_jsonl.v1",
        "parser_version": "2026-08-29.1",
        "participant_count": 2,
        "event_count": 2,
        "loss_report": {"projected_message_count": 2, "projection_complete": True},
    }

    result = chat_history_service.upsert_external_conversation_projection(
        user_id="#V#user",
        session_id="external-session",
        session_name="Imported session",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        role_in_org="member",
        history=history,
        manifest=manifest,
    )

    assert result["success"] is True
    assert result["action"] == "new"
    assert result["canonical_read_back"]["external_conversation"]["read_only"] is True
    stored = collection.find_one(
        {"user_id": "#V#user", "session_id": "external-session"}
    )
    assert all("author_user_id" not in message for message in stored["history"])
    assert stored["origin_kind"] == "external_conversation_import"
    summaries = chat_history_service.get_chat_history_session_summaries(
        "#V#user",
        namespace="#V#user@org",
    )
    external_summary = summaries[0]["external_conversation"]
    assert external_summary["provider"] == "codex"
    assert "source_account" not in external_summary
    assert "source_workspace" not in external_summary
    assert (
        chat_history_service.is_external_conversation_read_only(
            user_id="#V#user",
            session_id="external-session",
            namespace="#V#user@org",
        )
        is True
    )
    assert (
        chat_history_service.is_external_conversation_read_only(
            user_id="#V#other",
            session_id="external-session",
            namespace="#V#other@org",
        )
        is False
    )

    repeated = chat_history_service.upsert_external_conversation_projection(
        user_id="#V#user",
        session_id="external-session",
        session_name="Changed source title",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        role_in_org="member",
        history=history,
        manifest=manifest,
    )
    assert repeated["action"] == "unchanged"
    assert (
        collection.find_one({"session_id": "external-session"})["session_name"]
        == "Imported session"
    )


def test_import_dry_run_is_non_mutating_and_reports_projection(
    monkeypatch, tmp_path: Path
):
    source = tmp_path / "conversation.jsonl"
    source.write_bytes(
        _jsonl(
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hi"}],
                },
            },
        )
    )
    monkeypatch.setattr(
        chat_history_service,
        "classify_external_conversation_projection",
        lambda **_kwargs: "new",
    )

    result = import_external_conversation_file(
        path=source,
        custodian_user_id="#V#user",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        role_in_org="member",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["status"] == "dry_run"
    assert result["preview"]["loss_report"]["projected_message_count"] == 2
    assert result["preview"]["read_only"] is True
    assert result["canonical_read_back"] is None


def test_executed_import_archives_actor_scoped_raw_source_before_projection(
    monkeypatch, tmp_path: Path
):
    source = tmp_path / "conversation.jsonl"
    source.write_bytes(
        _jsonl(
            {"type": "session_meta", "payload": {"id": "codex-session"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hi"}],
                },
            },
        )
    )
    captured: dict = {}

    class FakeIngestionService:
        def __init__(self, *, user_concept_id, adapters):
            captured["ingestion_user"] = user_concept_id
            captured["adapters"] = adapters

        def run(self, *, dry_run):
            captured["ingestion_dry_run"] = dry_run
            records, warnings = captured["adapters"][0].discover()
            assert warnings == []
            record = records[0]
            captured["source_record"] = record
            return SimpleNamespace(
                success=True,
                status="completed",
                records=[{"action": "new", "file_copy_concept_id": "#V#copy"}],
                error_code=None,
                to_dict=lambda: {"success": True, "status": "completed"},
            )

    monkeypatch.setattr(
        external_conversation_import_service,
        "AIChatSessionIngestionService",
        FakeIngestionService,
    )
    monkeypatch.setattr(
        chat_history_service,
        "classify_external_conversation_projection",
        lambda **_kwargs: "new",
    )

    def fake_upsert(**kwargs):
        captured["projection"] = kwargs
        return {
            "success": True,
            "action": "new",
            "canonical_read_back": {
                "session_id": kwargs["session_id"],
                "external_conversation": {
                    "read_only": True,
                    "provider": kwargs["manifest"]["provider"],
                },
            },
        }

    monkeypatch.setattr(
        chat_history_service,
        "upsert_external_conversation_projection",
        fake_upsert,
    )
    monkeypatch.setattr(
        external_conversation_import_service.concept_service,
        "update_concept",
        lambda concept_id, update: captured.update(
            {"annotated_concept_id": concept_id, "annotation": update}
        ),
    )

    result = import_external_conversation_file(
        path=source,
        custodian_user_id="#V#user",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        role_in_org="member",
        dry_run=False,
    )

    source_record = captured["source_record"]
    assert result["success"] is True
    assert result["raw_manifest_annotation_status"] == "updated"
    assert captured["ingestion_user"] == "#V#user"
    assert captured["ingestion_dry_run"] is False
    assert source_record.canonical_source_path.startswith(
        "external-import://custodian/"
    )
    assert str(tmp_path) not in source_record.canonical_source_path
    assert captured["projection"]["manifest"]["raw_document_concept_id"] == (
        source_record.document_concept_id
    )
    assert captured["annotated_concept_id"] == source_record.document_concept_id
    assert result["canonical_read_back"]["external_conversation"] == {
        "read_only": True,
        "provider": "codex",
    }


def test_continuation_forks_history_into_writable_native_session(monkeypatch):
    collection = mongomock.MongoClient().von_test.chat_history
    collection.insert_one(
        {
            "user_id": "#V#user",
            "session_id": "external-session",
            "session_name": "Imported session",
            "namespace": "#V#user@org",
            "history": [
                {
                    "role": "user",
                    "content": "Historical human",
                    "external_actor": {
                        "source_actor_id": "codex:source_user",
                        "actor_kind": "unresolved_human",
                    },
                },
                {
                    "role": "assistant",
                    "content": "Historical Codex",
                    "external_actor": {
                        "source_actor_id": "codex:agent",
                        "actor_kind": "agent",
                    },
                },
            ],
            "external_conversation_import": {
                "schema_version": "external_conversation_import.v1",
                "read_only": True,
                "provider": "codex",
                "source_session_id": "source-session",
                "source_sha256": "a" * 64,
                "package_sha256": "b" * 64,
            },
        }
    )
    monkeypatch.setattr(
        chat_history_service,
        "get_chat_history_collection_service",
        lambda **_kwargs: collection,
    )
    monkeypatch.setattr(
        chat_history_service,
        "_read_find_one",
        lambda coll, query, projection=None, **_kwargs: coll.find_one(
            query, projection
        ),
    )
    monkeypatch.setattr(chat_history_service, "get_session_context", lambda: {})

    result = continue_external_conversation(
        custodian_user_id="#V#user",
        source_session_id="external-session",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        role_in_org="member",
    )

    assert result["success"] is True
    assert result["history_message_count"] == 2
    native = collection.find_one(
        {"user_id": "#V#user", "session_id": result["session_id"]}
    )
    assert native["origin_kind"] == "external_conversation_continuation"
    assert native["conversation_lineage"]["forked_from_session_id"] == (
        "external-session"
    )
    assert "external_conversation_import" not in native
    assert (
        native["history"]
        == collection.find_one({"session_id": "external-session"})["history"]
    )
    assert (
        chat_history_service.is_external_conversation_read_only(
            user_id="#V#user",
            session_id=result["session_id"],
            namespace="#V#user@org",
        )
        is False
    )


def test_unknown_transcript_format_is_rejected():
    with pytest.raises(ExternalConversationImportError) as exc_info:
        parse_external_conversation_bytes(b'{"messages": [{"role": "user"}]}')

    assert exc_info.value.error_code == "external_conversation_format_not_detected"
