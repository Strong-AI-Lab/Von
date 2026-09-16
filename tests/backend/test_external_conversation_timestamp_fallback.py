from datetime import datetime, timezone
import json
import os

import mongomock

from src.backend.services import chat_history_service as history_service
from src.backend.services import external_conversation_import_service as importer


def test_file_mtime_fills_only_missing_message_dates(monkeypatch, tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {"type": "session_meta", "payload": {"id": "dated-source"}},
                {
                    "type": "response_item",
                    "timestamp": "2026-01-02T03:04:05Z",
                    "payload": {"type": "message", "role": "user", "content": "Dated"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": "Undated",
                    },
                },
            ]
        )
    )
    modified = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    os.utime(path, (modified.timestamp(), modified.timestamp()))
    original = importer._history_projection
    captured = []

    def project(package, **kwargs):
        result = original(package, **kwargs)
        captured.extend(result[0])
        return result

    monkeypatch.setattr(importer, "_history_projection", project)
    monkeypatch.setattr(
        history_service,
        "classify_external_conversation_projection",
        lambda **kwargs: "new",
    )
    importer.import_external_conversation_file(
        path=path,
        custodian_user_id="#V#user",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
        dry_run=True,
    )
    assert captured[0]["timestamp"] == "2026-01-02T03:04:05+00:00"
    assert captured[0]["external_timestamp_source"] == "source_message"
    assert captured[1]["timestamp"] == modified
    assert captured[1]["external_source_timestamp_utc"] is None
    assert captured[1]["external_timestamp_source"] == "source_file_modified"


def test_reimport_repairs_fallbacks_preserving_source_dates_and_appends(monkeypatch):
    collection = mongomock.MongoClient().test.chat_history
    monkeypatch.setattr(
        history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: collection,
    )
    monkeypatch.setattr(
        history_service,
        "_read_find_one",
        lambda coll, query, projection=None, **kwargs: coll.find_one(query, projection),
    )
    manifest = dict(
        schema_version="external_conversation_import.v1",
        read_only=True,
        custodian_user_id="#V#user",
        source_identity_key="same",
        source_sha256="a" * 64,
        package_sha256="b" * 64,
        parser_version="old",
        provider="codex",
    )
    dated = dict(
        role="user",
        content="Dated",
        timestamp="2026-01-02T00:00:00Z",
        external_source_timestamp_utc="2026-01-02T00:00:00Z",
        external_event_id="one",
    )
    undated = dict(
        role="assistant",
        content="Undated",
        timestamp="2026-09-16T00:00:00Z",
        external_source_timestamp_utc=None,
        external_event_id="two",
    )

    def write(entries, revision):
        return history_service.upsert_external_conversation_projection(
            user_id="#V#user",
            session_id="external",
            session_name="Imported",
            namespace=None,
            organisation_concept_id=None,
            role_in_org=None,
            history=entries,
            manifest={**manifest, "parser_version": revision},
        )

    write([dated, undated], "old")
    repaired = {
        **undated,
        "timestamp": "2026-01-03T00:00:00Z",
        "external_timestamp_source": "source_file_modified",
    }
    extra = {**dated, "content": "Appended", "external_event_id": "three"}
    result = write([dated, repaired, extra], "new")
    assert result["corrected_fallback_timestamp_count"] == 1
    assert result["appended_message_count"] == 1
    stored = collection.find_one({})
    assert [e["timestamp"] for e in stored["history"]] == [
        dated["timestamp"],
        repaired["timestamp"],
        extra["timestamp"],
    ]
    assert stored["updated_at"] == datetime(2026, 1, 3)
    assert write([dated, repaired, extra], "new")["action"] == "unchanged"
    # Changed historical content remains a divergence, never a timestamp repair.
    divergent = write(
        [
            dated,
            {**repaired, "content": "Changed", "timestamp": "2026-03-03T00:00:00Z"},
            extra,
        ],
        "newer",
    )
    assert divergent["action"] == "diverged"
    assert divergent["corrected_fallback_timestamp_count"] == 0
    assert collection.find_one({})["history"] == stored["history"]


def test_repair_corrects_conversation_creation_date(monkeypatch):
    collection = mongomock.MongoClient().test.chat_history
    monkeypatch.setattr(
        history_service,
        "get_chat_history_collection_service",
        lambda **kwargs: collection,
    )
    monkeypatch.setattr(
        history_service,
        "_read_find_one",
        lambda coll, query, projection=None, **kwargs: coll.find_one(query, projection),
    )
    kwargs = dict(
        user_id="#V#user",
        session_id="external",
        session_name="Imported",
        namespace=None,
        organisation_concept_id=None,
        role_in_org=None,
    )
    manifest = dict(
        schema_version="external_conversation_import.v1",
        read_only=True,
        custodian_user_id="#V#user",
        source_identity_key="same",
        source_sha256="a" * 64,
        package_sha256="b" * 64,
        parser_version="old",
        provider="codex",
    )
    entry = dict(
        role="user",
        content="Undated",
        timestamp="2026-09-16T00:00:00Z",
        external_source_timestamp_utc=None,
        external_event_id="one",
    )
    history_service.upsert_external_conversation_projection(
        **kwargs, history=[entry], manifest=manifest
    )
    history_service.upsert_external_conversation_projection(
        **kwargs,
        history=[
            {
                **entry,
                "timestamp": "2026-01-03T00:00:00Z",
                "external_timestamp_source": "source_file_modified",
            }
        ],
        manifest={**manifest, "parser_version": "new"},
    )
    assert collection.find_one({})["created_at"] == datetime(2026, 1, 3)
