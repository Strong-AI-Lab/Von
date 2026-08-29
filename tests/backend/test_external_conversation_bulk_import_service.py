from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import mongomock
import pytest

from src.backend.services import external_conversation_bulk_import_service as bulk
from src.backend.services import external_conversation_import_service as importer
from src.backend.services import chat_history_service
from src.backend.services.blob_store import LocalBlobStore


def _collections(monkeypatch):
    database = mongomock.MongoClient().von_test
    batches = database[bulk.BATCH_COLLECTION]
    items = database[bulk.ITEM_COLLECTION]
    monkeypatch.setattr(bulk, "_collections", lambda: (batches, items))
    monkeypatch.setattr(
        bulk, "start_external_conversation_bulk_import_worker", lambda: None
    )
    return batches, items


def _codex_source(path: Path) -> None:
    records = [
        {"type": "session_meta", "payload": {"id": "session-1"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "message-1",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def test_local_blob_store_streams_a_file_without_changing_its_bytes(tmp_path: Path):
    source = tmp_path / "source.bin"
    source.write_bytes((b"bounded-stream" * 1024) + b"end")
    store = LocalBlobStore(tmp_path / "blobs")

    ref = store.put_file(
        "imports/test/source.bin",
        source,
        content_type="application/octet-stream",
        metadata={"custodian": "test"},
    )

    assert ref.size_bytes == source.stat().st_size
    assert store.get_bytes(ref.key) == source.read_bytes()
    assert ref.metadata == {"custodian": "test"}


def test_discovery_is_allowlisted_and_preview_does_not_persist(
    monkeypatch, tmp_path: Path
):
    root = tmp_path / "codex"
    root.mkdir()
    source = root / "conversation.jsonl"
    _codex_source(source)
    outside = tmp_path / "outside.jsonl"
    _codex_source(outside)
    (root / "escaped.jsonl").symlink_to(outside)
    monkeypatch.setenv("VON_CODEX_CONVERSATION_ROOT", str(root))
    monkeypatch.setattr(
        bulk,
        "_collections",
        lambda: (_ for _ in ()).throw(AssertionError("preview must not persist")),
    )

    preview = bulk.preview_local_conversation_import(providers=["codex"])

    assert preview["read_only"] is True
    assert preview["source_count"] == 1
    assert preview["providers"]["codex"]["source_count"] == 1
    assert preview["warning_count"] == 1
    assert str(tmp_path) not in json.dumps(preview)
    assert "local_path" not in json.dumps(preview)


def test_batch_pause_resume_and_expired_lease_recovery(monkeypatch, tmp_path: Path):
    batches, items = _collections(monkeypatch)
    root = tmp_path / "codex"
    root.mkdir()
    _codex_source(root / "conversation.jsonl")
    monkeypatch.setenv("VON_CODEX_CONVERSATION_ROOT", str(root))

    started = bulk.start_local_conversation_import(
        custodian_user_id="#V#user",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        role_in_org="member",
        providers=["codex"],
    )
    batch_id = started["batch_id"]
    paused = bulk.control_local_conversation_import(
        custodian_user_id="#V#user", batch_id=batch_id, action="pause"
    )
    assert paused["status"] == "pause_requested"
    bulk._settle_batch(batch_id)
    stored_batch = batches.find_one({"batch_id": batch_id})
    assert stored_batch is not None
    assert stored_batch["status"] == "paused"
    resumed = bulk.control_local_conversation_import(
        custodian_user_id="#V#user", batch_id=batch_id, action="resume"
    )
    assert resumed["batch_id"] == batch_id
    assert resumed["status"] == "ready"

    item = items.find_one({"batch_id": batch_id})
    assert item is not None
    items.update_one(
        {"_id": item["_id"]},
        {
            "$set": {
                "status": "running",
                "lease_token": "dead-worker",
                "lease_expires_at": bulk._now() - timedelta(seconds=1),
            }
        },
    )
    claimed = bulk._claim_item()
    assert claimed is not None
    assert claimed["item_id"] == item["item_id"]
    assert claimed["lease_token"] != "dead-worker"
    assert claimed["attempt_count"] == 1

    safe_items = bulk.list_local_conversation_import_items(
        custodian_user_id="#V#user", batch_id=batch_id
    )
    assert "local_path" not in safe_items["items"][0]
    with pytest.raises(bulk.ExternalConversationBulkImportError) as denied:
        bulk.get_local_conversation_import_batch(
            custodian_user_id="#V#other", batch_id=batch_id
        )
    assert denied.value.status_code == 404


def test_worker_recovers_planning_batch_after_startup_database_loss(
    monkeypatch, tmp_path: Path
):
    batches, _items = _collections(monkeypatch)
    root = tmp_path / "codex"
    root.mkdir()
    _codex_source(root / "conversation.jsonl")
    monkeypatch.setenv("VON_CODEX_CONVERSATION_ROOT", str(root))
    batches.insert_one(
        {
            "batch_id": "recover-planning",
            "custodian_user_id": "#V#user",
            "providers": ["codex"],
            "status": "planning",
            "created_at": bulk._now(),
            "updated_at": bulk._now(),
        }
    )

    claimed = bulk._claim_item()

    assert claimed is not None
    assert claimed["batch_id"] == "recover-planning"
    assert claimed["status"] == "running"
    recovered = batches.find_one({"batch_id": "recover-planning"})
    assert recovered is not None
    assert recovered["status"] == "running"
    assert recovered["source_count"] == 1


def test_projection_failure_retries_same_item_then_completes(
    monkeypatch, tmp_path: Path
):
    batches, items = _collections(monkeypatch)
    root = tmp_path / "codex"
    root.mkdir()
    _codex_source(root / "conversation.jsonl")
    monkeypatch.setenv("VON_CODEX_CONVERSATION_ROOT", str(root))
    started = bulk.start_local_conversation_import(
        custodian_user_id="#V#user",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        providers=["codex"],
    )
    claimed = bulk._claim_item()
    assert claimed is not None

    def _fail_after_raw_checkpoint(**kwargs):
        kwargs["checkpoint_callback"](
            "raw_ingested",
            {
                "completed": True,
                "source_sha256": "a" * 64,
                "document_concept_id": "#V#raw",
            },
        )
        return {
            "success": False,
            "status": "conversation_projection_failed",
            "error_code": "conversation_projection_failed",
        }

    monkeypatch.setattr(
        bulk, "import_external_conversation_file", _fail_after_raw_checkpoint
    )
    bulk._process_item(claimed)
    retriable = items.find_one({"item_id": claimed["item_id"]})
    assert retriable is not None
    assert retriable["status"] == "retry_wait"
    assert retriable["raw_checkpoint"]["document_concept_id"] == "#V#raw"
    items.update_one(
        {"item_id": claimed["item_id"]},
        {"$set": {"next_retry_at": bulk._now() - timedelta(seconds=1)}},
    )
    reclaimed = bulk._claim_item()
    assert reclaimed is not None
    monkeypatch.setattr(
        bulk,
        "import_external_conversation_file",
        lambda **_kwargs: {
            "success": True,
            "status": "appended",
            "raw_ingestion": {
                "action": "updated",
                "document_concept_id": "#V#raw",
            },
            "projection": {
                "action": "appended",
                "session_id": "external-codex-1",
                "appended_message_count": 1,
                "divergence": None,
            },
        },
    )
    bulk._process_item(reclaimed)
    completed = items.find_one({"item_id": claimed["item_id"]})
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["attempt_count"] == 2
    assert completed["projection_checkpoint"]["session_id"] == "external-codex-1"
    completed_batch = batches.find_one({"batch_id": started["batch_id"]})
    assert completed_batch is not None
    assert completed_batch["status"] == "completed"


def test_gemini_journal_is_imported_without_inventing_assistant_messages():
    raw = (
        json.dumps(
            {
                "sessionId": "gemini-session",
                "projectHash": "project-hash",
                "kind": "main",
            }
        )
        + "\n"
        + json.dumps(
            {
                "$set": {
                    "messages": [
                        {
                            "id": "user-1",
                            "timestamp": "2026-08-01T00:00:00Z",
                            "type": "user",
                            "content": [{"text": "Please explain this."}],
                        }
                    ]
                }
            }
        )
        + "\n"
    ).encode()

    package = importer.parse_external_conversation_bytes(raw)

    assert package.provider == "gemini"
    assert [event.source_role for event in package.events] == ["user"]
    assert package.loss_report["source_has_no_assistant_messages"] is True


def test_large_jsonl_parser_discards_one_oversized_record_boundedly(
    monkeypatch, tmp_path: Path
):
    source = tmp_path / "large.jsonl"
    source.write_bytes(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "large-session"}}
        ).encode()
        + b"\n"
        + b'{"oversized":"'
        + (b"x" * 512)
        + b'"}\n'
        + json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "message-1",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Kept"}],
                },
            }
        ).encode()
        + b"\n"
    )
    monkeypatch.setattr(importer, "MAX_EXTERNAL_CONVERSATION_SOURCE_BYTES", 100)
    monkeypatch.setattr(importer, "MAX_EXTERNAL_CONVERSATION_JSONL_LINE_BYTES", 256)

    package = importer.parse_external_conversation_file(source, provider_hint="codex")

    assert package.source_session_id == "large-session"
    assert [event.visible_text for event in package.events] == ["Kept"]
    assert package.loss_report["oversized_record_count"] == 1


def test_repeat_projection_appends_unseen_events_and_preserves_divergence(monkeypatch):
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

    def manifest(source: str, package: str):
        return {
            "schema_version": "external_conversation_import.v1",
            "read_only": True,
            "custodian_user_id": "#V#user",
            "source_identity_key": "same-source",
            "provider": "codex",
            "source_session_id": "source-session",
            "source_sha256": source,
            "package_sha256": package,
            "parser_id": "codex_jsonl.v1",
            "parser_version": "2026-08-29.1",
            "loss_report": {},
        }

    first = [
        {
            "role": "user",
            "content": "one",
            "timestamp": "2026-08-01T00:00:00Z",
            "external_event_id": "event-1",
        }
    ]
    chat_history_service.upsert_external_conversation_projection(
        user_id="#V#user",
        session_id="external-session",
        session_name="Imported",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        history=first,
        manifest=manifest("a" * 64, "b" * 64),
    )
    collection.update_one(
        {}, {"$unset": {"history.0.external_event_sha256": ""}}
    )
    appended = chat_history_service.upsert_external_conversation_projection(
        user_id="#V#user",
        session_id="external-session",
        session_name="Imported",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        history=[
            {
                **first[0],
                "timestamp": "2026-08-02T00:00:00Z",
                "external_source_timestamp_utc": None,
            },
            {
                "role": "assistant",
                "content": "two",
                "timestamp": "2026-08-01T00:00:01Z",
                "external_event_id": "event-2",
            },
        ],
        manifest=manifest("c" * 64, "d" * 64),
    )
    assert appended["action"] == "appended"
    assert appended["appended_message_count"] == 1
    stored_after_append = collection.find_one({})
    assert stored_after_append is not None
    assert [entry["content"] for entry in stored_after_append["history"]] == [
        "one",
        "two",
    ]

    diverged = chat_history_service.upsert_external_conversation_projection(
        user_id="#V#user",
        session_id="external-session",
        session_name="Imported",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        history=[
            {
                "role": "assistant",
                "content": "changed two",
                "timestamp": "2026-08-01T00:00:01Z",
                "external_event_id": "event-2",
            },
            {
                "role": "user",
                "content": "three",
                "timestamp": "2026-08-01T00:00:02Z",
                "external_event_id": "event-3",
            },
        ],
        manifest=manifest("e" * 64, "f" * 64),
    )
    assert diverged["action"] == "appended_with_divergence"
    assert diverged["divergence"]["missing_event_count"] == 1
    assert diverged["divergence"]["changed_event_count"] == 1
    stored_after_divergence = collection.find_one({})
    assert stored_after_divergence is not None
    assert [entry["content"] for entry in stored_after_divergence["history"]] == [
        "one",
        "two",
        "three",
    ]
    repeated = chat_history_service.upsert_external_conversation_projection(
        user_id="#V#user",
        session_id="external-session",
        session_name="Imported",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        history=[
            {
                "role": "assistant",
                "content": "changed two",
                "timestamp": "2026-08-01T00:00:01Z",
                "external_event_id": "event-2",
            },
            {
                "role": "user",
                "content": "three",
                "timestamp": "2026-08-01T00:00:02Z",
                "external_event_id": "event-3",
            },
        ],
        manifest=manifest("e" * 64, "f" * 64),
    )
    assert repeated["action"] == "unchanged"
    assert repeated["divergence"]["missing_event_count"] == 1
