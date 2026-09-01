from __future__ import annotations

from typing import Any, cast

import mongomock
import pytest

from src.backend.db import mongo_client as mc


class _FakeCollection:
    def __init__(self, *, index_names: list[str] | None = None) -> None:
        self.index_names = list(index_names or [])
        self.list_indexes_calls = 0
        self.create_index_calls: list[dict[str, Any]] = []
        self.drop_index_calls: list[str] = []

    def list_indexes(self) -> list[dict[str, str]]:
        self.list_indexes_calls += 1
        return [{"name": name} for name in self.index_names]

    def create_index(self, keys: list[tuple[str, Any]], **kwargs: Any) -> None:
        name = str(kwargs.get("name") or "")
        if not name:
            name = "_".join(f"{field}_{direction}" for field, direction in keys)
        self.create_index_calls.append({"keys": list(keys), "kwargs": dict(kwargs)})
        if name not in self.index_names:
            self.index_names.append(name)

    def drop_index(self, name: str) -> None:
        self.drop_index_calls.append(name)
        if name in self.index_names:
            self.index_names.remove(name)


class _FakeDb:
    def __init__(self, name: str, *, client: Any) -> None:
        self.name = name
        self.client = client
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, collection_name: str) -> _FakeCollection:
        return self._collections.setdefault(collection_name, _FakeCollection())


def _reset_collection_index_state(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_COLLECTION_INDEXES_READY", set())


def test_text_value_indexes_are_ensured_once_per_database(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll_first = cast(_FakeCollection, mc.get_text_values_collection())
    coll_second = cast(_FakeCollection, mc.get_text_values_collection())

    assert coll_first is coll_second
    assert coll_first is not None
    assert coll_first.list_indexes_calls == 0
    assert len(coll_first.create_index_calls) == 6
    assert {
        str(call["kwargs"].get("name")) for call in coll_first.create_index_calls
    } == {
        "text_text_search",
        "text_1",
        "lang_1",
        "fingerprint_lang_unique",
        "created_at_-1",
        "updated_at_-1",
    }


def test_scoped_assertion_indexes_are_ensured_once_per_database(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll_first = cast(
        _FakeCollection,
        mc.get_scoped_knowledge_assertions_collection(),
    )
    coll_second = cast(
        _FakeCollection,
        mc.get_scoped_knowledge_assertions_collection(),
    )

    assert coll_first is coll_second
    assert coll_first is not None
    assert len(coll_first.create_index_calls) == 10
    calls_by_name = {
        str(call["kwargs"].get("name")): call
        for call in coll_first.create_index_calls
    }
    assert set(calls_by_name) == {
        "assertion_id_unique",
        "audience_status_updated_at_desc_assertion_id",
        "subject_audience_status_updated_at_desc_assertion_id",
        "object_audience_status_updated_at_desc_assertion_id",
        "predicate_audience_status_updated_at_desc_assertion_id",
        "audience_status_id",
        "subject_audience_status_id",
        "predicate_audience_status_id",
        "concept_link_audience_status_updated_at_desc_assertion_id",
        "standalone_rag_queue_status_due_lease_updated_at",
    }
    assert calls_by_name["audience_status_updated_at_desc_assertion_id"]["keys"] == [
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("updated_at", mc.DESCENDING),
        ("assertion_id", mc.ASCENDING),
    ]
    assert calls_by_name[
        "object_audience_status_updated_at_desc_assertion_id"
    ]["keys"] == [
        ("object_concept_id", mc.ASCENDING),
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("updated_at", mc.DESCENDING),
        ("assertion_id", mc.ASCENDING),
    ]
    assert calls_by_name[
        "predicate_audience_status_updated_at_desc_assertion_id"
    ]["keys"] == [
        ("predicate", mc.ASCENDING),
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("updated_at", mc.DESCENDING),
        ("assertion_id", mc.ASCENDING),
    ]
    assert calls_by_name[
        "subject_audience_status_updated_at_desc_assertion_id"
    ]["keys"] == [
        ("subject_concept_id", mc.ASCENDING),
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("updated_at", mc.DESCENDING),
        ("assertion_id", mc.ASCENDING),
    ]
    assert calls_by_name["audience_status_id"]["keys"] == [
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("_id", mc.ASCENDING),
    ]
    assert calls_by_name["subject_audience_status_id"]["keys"] == [
        ("subject_concept_id", mc.ASCENDING),
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("_id", mc.ASCENDING),
    ]
    assert calls_by_name["predicate_audience_status_id"]["keys"] == [
        ("predicate", mc.ASCENDING),
        ("scope.audience_keys", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("_id", mc.ASCENDING),
    ]
    assert calls_by_name[
        "concept_link_audience_status_updated_at_desc_assertion_id"
    ]["keys"] == [
        ("concept_links.concept_id", mc.ASCENDING),
        ("scope.audience_key", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("updated_at", mc.DESCENDING),
        ("assertion_id", mc.ASCENDING),
    ]
    assert calls_by_name[
        "standalone_rag_queue_status_due_lease_updated_at"
    ]["keys"] == [
        ("assertion_form", mc.ASCENDING),
        ("object_kind", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("rag_index.desired_operation", mc.ASCENDING),
        ("rag_index.status", mc.ASCENDING),
        ("rag_index.next_attempt_at", mc.ASCENDING),
        ("rag_index.lease_expires_at", mc.ASCENDING),
        ("updated_at", mc.ASCENDING),
    ]


def test_interaction_session_indexes_are_ensured_once_per_database(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll_first = cast(_FakeCollection, mc.get_interaction_sessions_collection())
    coll_second = cast(_FakeCollection, mc.get_interaction_sessions_collection())

    assert coll_first is coll_second
    assert coll_first is not None
    assert len(coll_first.create_index_calls) == 2
    calls_by_name = {
        str(call["kwargs"].get("name") or ""): call
        for call in coll_first.create_index_calls
    }
    assert calls_by_name["indexing_status_1"]["keys"] == [
        ("indexing_status", mc.ASCENDING)
    ]
    assert calls_by_name["indexing_status_indexed_at_desc"]["keys"] == [
        ("indexing_status", mc.ASCENDING),
        ("indexed_at", mc.DESCENDING),
    ]


def test_application_settings_index_is_ensured_once_per_database(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll_first = cast(_FakeCollection, mc.get_application_settings_collection())
    coll_second = cast(_FakeCollection, mc.get_application_settings_collection())

    assert coll_first is coll_second
    assert coll_first is not None
    assert len(coll_first.create_index_calls) == 1
    assert coll_first.create_index_calls[0]["keys"] == [("setting_name", mc.ASCENDING)]
    assert coll_first.create_index_calls[0]["kwargs"] == {"unique": True}


def test_gmail_outbound_operational_indexes_are_ensured_once(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    quota_first = cast(_FakeCollection, mc.get_gmail_outbound_quota_collection())
    quota_second = cast(_FakeCollection, mc.get_gmail_outbound_quota_collection())
    deliveries_first = cast(
        _FakeCollection,
        mc.get_gmail_outbound_deliveries_collection(),
    )
    deliveries_second = cast(
        _FakeCollection,
        mc.get_gmail_outbound_deliveries_collection(),
    )

    assert quota_first is quota_second
    assert deliveries_first is deliveries_second
    assert quota_first.create_index_calls == [
        {
            "keys": [("expires_at", mc.ASCENDING)],
            "kwargs": {"name": "expires_at_ttl", "expireAfterSeconds": 0},
        }
    ]
    assert deliveries_first.create_index_calls == [
        {
            "keys": [("delivery_fingerprint", mc.ASCENDING)],
            "kwargs": {
                "name": "delivery_fingerprint_1_unique",
                "unique": True,
            },
        },
        {
            "keys": [("status", mc.ASCENDING), ("updated_at", mc.DESCENDING)],
            "kwargs": {"name": "status_1_updated_at_-1"},
        },
    ]


def test_concepts_indexes_are_guarded_per_database_key(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    shared_client = object()
    db_one = _FakeDb("test_von_db", client=shared_client)
    db_two = _FakeDb("alt_test_von_db", client=shared_client)
    db_one[mc.CONCEPTS_COLLECTION_NAME].index_names.append("metadata.concept_type_1")
    db_two[mc.CONCEPTS_COLLECTION_NAME].index_names.append("metadata.concept_type_1")

    current_db = {"value": db_one}
    monkeypatch.setattr(mc, "get_db", lambda: current_db["value"])

    first = cast(_FakeCollection, mc.get_concepts_collection())
    second = cast(_FakeCollection, mc.get_concepts_collection())
    current_db["value"] = db_two
    third = cast(_FakeCollection, mc.get_concepts_collection())

    assert first is second
    assert first is not None and third is not None
    assert db_one[mc.CONCEPTS_COLLECTION_NAME].list_indexes_calls == 1
    assert db_two[mc.CONCEPTS_COLLECTION_NAME].list_indexes_calls == 1
    assert db_one[mc.CONCEPTS_COLLECTION_NAME].drop_index_calls == [
        "metadata.concept_type_1"
    ]
    assert db_two[mc.CONCEPTS_COLLECTION_NAME].drop_index_calls == [
        "metadata.concept_type_1"
    ]

    concept_index_names = {
        str(call["kwargs"].get("name") or "")
        for call in db_one[mc.CONCEPTS_COLLECTION_NAME].create_index_calls
    }
    assert {
        "attributes_mcp_tool_name_1",
        "relationships_v_has_initial_step_camel_1",
        "relationships_hasInitialStep_1",
        "relationships_v_has_initial_step_snake_1",
        "relationships_has_initial_step_1",
        "relationships_v_evidence_view_applies_to_tool_1",
        "episode_critique_remediation_external_instance_lookup",
        "direct_message_idempotency_scope_unique",
        "embedding_status_updated_at_desc",
        "legacy_name_exact_1",
        "updated_at_-1",
    }.issubset(concept_index_names)
    concept_indexes_by_name = {
        str(call["kwargs"].get("name") or ""): call
        for call in db_one[mc.CONCEPTS_COLLECTION_NAME].create_index_calls
    }
    assert concept_indexes_by_name["legacy_name_exact_1"]["keys"] == [
        ("name", mc.ASCENDING)
    ]
    assert concept_indexes_by_name["legacy_name_exact_1"]["kwargs"] == {
        "name": "legacy_name_exact_1",
        "sparse": True,
    }
    assert concept_indexes_by_name["embedding_status_updated_at_desc"]["keys"] == [
        ("embedding_status", mc.ASCENDING),
        ("updated_at", mc.DESCENDING),
    ]
    assert concept_indexes_by_name["embedding_status_updated_at_desc"]["kwargs"] == {
        "name": "embedding_status_updated_at_desc"
    }
    assert concept_indexes_by_name["direct_message_idempotency_scope_unique"][
        "keys"
    ] == [
        (
            "concept_data.metadata.delivery_idempotency_scope",
            mc.ASCENDING,
        )
    ]
    assert concept_indexes_by_name["direct_message_idempotency_scope_unique"][
        "kwargs"
    ] == {
        "name": "direct_message_idempotency_scope_unique",
        "unique": True,
        "partialFilterExpression": {
            "concept_data.metadata.delivery_idempotency_scope": {
                "$exists": True,
                "$type": "string",
            }
        },
    }


def test_concepts_indexes_accept_existing_scalar_legacy_name_index() -> None:
    coll = _FakeCollection(index_names=["name_1"])

    mc._ensure_concepts_collection_indexes(cast(Any, coll))

    created_names = {
        str(call["kwargs"].get("name") or "") for call in coll.create_index_calls
    }
    assert "legacy_name_exact_1" not in created_names


def test_invalidate_connection_clears_collection_index_cache(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll = cast(_FakeCollection, mc.get_text_relations_collection())
    assert coll is not None
    initial_calls = len(coll.create_index_calls)

    mc.invalidate_connection()
    coll_after_reset = cast(_FakeCollection, mc.get_text_relations_collection())

    assert coll_after_reset is coll
    assert len(coll.create_index_calls) == initial_calls * 2


def test_text_relation_indexes_cover_concept_search_and_atlas_name_lookup(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll = cast(_FakeCollection, mc.get_text_relations_collection())

    assert coll is not None
    calls_by_name = {
        str(call["kwargs"].get("name") or ""): call for call in coll.create_index_calls
    }
    assert calls_by_name.keys() >= {
        "context_name_type_predicate_text",
        "object_predicate_subject_lookup",
        "subject_concept_id_id",
        "predicate_id",
        "updated_at_id",
    }
    assert calls_by_name["object_predicate_subject_lookup"]["keys"] == [
        ("object_text_id", mc.ASCENDING),
        ("predicate", mc.ASCENDING),
        ("subject_concept_id", mc.ASCENDING),
    ]
    assert calls_by_name["subject_concept_id_id"]["keys"] == [
        ("subject_concept_id", mc.ASCENDING),
        ("_id", mc.ASCENDING),
    ]
    assert calls_by_name["predicate_id"]["keys"] == [
        ("predicate", mc.ASCENDING),
        ("_id", mc.ASCENDING),
    ]
    assert calls_by_name["updated_at_id"]["keys"] == [
        ("updated_at", mc.ASCENDING),
        ("_id", mc.ASCENDING),
    ]


def test_relationship_extent_indexes_add_target_first_lookup_without_dropping_drift():
    coll = mongomock.MongoClient().db.relationship_extent_index
    wrong_keys = [
        ("predicate_id", mc.ASCENDING),
        ("source_concept_id", mc.ASCENDING),
        ("target_value", mc.ASCENDING),
    ]
    coll.create_index(
        wrong_keys,
        name="target_predicate_source_lookup",
    )

    mc._ensure_relationship_extent_index_indexes(coll)

    indexes = {index["name"]: index for index in coll.list_indexes()}
    assert list(indexes["target_predicate_source_lookup"]["key"].items()) == (
        wrong_keys
    )
    assert list(indexes["target_value_predicate_source_lookup_v2"]["key"].items()) == [
        ("target_value", mc.ASCENDING),
        ("predicate_id", mc.ASCENDING),
        ("source_concept_id", mc.ASCENDING),
    ]
    assert list(indexes["predicate_source_target_lookup"]["key"].items()) == (
        wrong_keys
    )
    assert indexes["relation_id_1_unique"]["unique"] is True


def test_relationship_extent_indexes_accept_equivalent_target_first_lookup():
    coll = mongomock.MongoClient().db.relationship_extent_index
    coll.create_index(
        [
            ("target_value", mc.ASCENDING),
            ("predicate_id", mc.ASCENDING),
            ("source_concept_id", mc.ASCENDING),
        ],
        name="deployment_specific_target_lookup",
    )

    mc._ensure_relationship_extent_index_indexes(coll)

    names = {index["name"] for index in coll.list_indexes()}
    assert "deployment_specific_target_lookup" in names
    assert "target_value_predicate_source_lookup_v2" not in names


def test_relationship_extent_target_index_failure_preserves_existing_indexes(
    monkeypatch,
):
    coll = mongomock.MongoClient().db.relationship_extent_index
    coll.create_index(
        [
            ("predicate_id", mc.ASCENDING),
            ("source_concept_id", mc.ASCENDING),
            ("target_value", mc.ASCENDING),
        ],
        name="target_predicate_source_lookup",
    )
    original_create_index = coll.create_index

    def fail_target_v2(keys, **kwargs):
        if kwargs.get("name") == "target_value_predicate_source_lookup_v2":
            raise RuntimeError("simulated Atlas index-build failure")
        return original_create_index(keys, **kwargs)

    monkeypatch.setattr(coll, "create_index", fail_target_v2)

    with pytest.raises(RuntimeError, match="simulated Atlas"):
        mc._ensure_relationship_extent_index_indexes(coll)

    indexes = {index["name"]: index for index in coll.list_indexes()}
    assert "target_predicate_source_lookup" in indexes
    assert list(indexes["target_predicate_source_lookup"]["key"].items()) == [
        ("predicate_id", mc.ASCENDING),
        ("source_concept_id", mc.ASCENDING),
        ("target_value", mc.ASCENDING),
    ]


def test_relationship_extent_index_ensure_does_not_replace_named_unique_drift():
    coll = mongomock.MongoClient().db.relationship_extent_index
    coll.create_index(
        [("relation_id", mc.ASCENDING)],
        name="relation_id_1_unique",
        unique=False,
    )

    mc._ensure_relationship_extent_index_indexes(coll)

    index = {item["name"]: item for item in coll.list_indexes()}["relation_id_1_unique"]
    assert index.get("unique") is not True


def test_chat_prompt_queue_indexes_include_active_legacy_submission_fence() -> None:
    coll = mongomock.MongoClient().db.chat_prompt_queue_indexes

    mc._ensure_chat_prompt_queue_indexes(coll)

    indexes = {index["name"]: index for index in coll.list_indexes()}
    active_legacy = indexes["active_legacy_submission_key_unique"]
    assert list(active_legacy["key"].items()) == [
        ("active_legacy_submission_key", mc.ASCENDING)
    ]
    assert active_legacy["unique"] is True
    assert active_legacy["partialFilterExpression"] == {
        "active_legacy_submission_key": {
            "$exists": True,
            "$type": "string",
        }
    }
    assert list(indexes["legacy_submission_expires_at"]["key"].items()) == [
        ("legacy_submission_expires_at", mc.ASCENDING)
    ]


def test_chat_prompt_queue_indexes_support_idempotent_dispatch_and_retention() -> None:
    coll = mongomock.MongoClient().db.chat_prompt_queue_dispatch_indexes

    mc._ensure_chat_prompt_queue_indexes(coll)

    indexes = {index["name"]: index for index in coll.list_indexes()}
    submission = indexes["scope_enqueue_submission_id_unique"]
    assert list(submission["key"].items()) == [
        ("user_concept_id", mc.ASCENDING),
        ("organisation_concept_id", mc.ASCENDING),
        ("namespace", mc.ASCENDING),
        ("enqueue_submission_id", mc.ASCENDING),
    ]
    assert submission["unique"] is True
    assert submission["partialFilterExpression"] == {
        "enqueue_submission_id": {"$exists": True, "$type": "string"}
    }
    active_task = indexes["active_task_execution_key_unique"]
    assert list(active_task["key"].items()) == [
        ("active_task_execution_key", mc.ASCENDING)
    ]
    assert active_task["unique"] is True
    assert active_task["partialFilterExpression"] == {
        "active_task_execution_key": {"$exists": True, "$type": "string"}
    }
    handoff_source = indexes["handoff_source_queue_id_unique"]
    assert list(handoff_source["key"].items()) == [
        ("handoff_source_queue_id", mc.ASCENDING)
    ]
    assert handoff_source["unique"] is True
    assert handoff_source["partialFilterExpression"] == {
        "handoff_source_queue_id": {"$exists": True, "$type": "string"}
    }
    assert list(indexes["scope_status_enqueue_sequence"]["key"].items()) == [
        ("user_concept_id", mc.ASCENDING),
        ("organisation_concept_id", mc.ASCENDING),
        ("namespace", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("enqueue_sequence", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("queue_id", mc.ASCENDING),
    ]
    assert list(
        indexes["conversation_status_enqueue_sequence"]["key"].items()
    ) == [
        ("conversation_key", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("enqueue_sequence", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("queue_id", mc.ASCENDING),
    ]
    assert list(
        indexes["dispatch_status_next_enqueue_sequence"]["key"].items()
    ) == [
        ("dispatch_mode", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("next_dispatch_at", mc.ASCENDING),
        ("enqueue_sequence", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("queue_id", mc.ASCENDING),
    ]
    assert list(indexes["dispatch_status_lease_expiry"]["key"].items()) == [
        ("dispatch_mode", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("dispatch_lease_expires_at", mc.ASCENDING),
    ]
    assert list(indexes["handoff_reconciliation_due"]["key"].items()) == [
        ("dispatch_mode", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("dispatch_ready", mc.ASCENDING),
        ("handoff_reconciliation_next_at", mc.ASCENDING),
        ("enqueue_sequence", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("queue_id", mc.ASCENDING),
    ]
    assert list(indexes["task_launch_reconciliation_due"]["key"].items()) == [
        ("dispatch_mode", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("dispatch_ready", mc.ASCENDING),
        ("task_launch_reconciliation_next_at", mc.ASCENDING),
        ("task_launch_reconciliation_lease_expires_at", mc.ASCENDING),
        ("enqueue_sequence", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("queue_id", mc.ASCENDING),
    ]
    assert list(indexes["task_execution_reconciliation_due"]["key"].items()) == [
        ("task_execution_reconciliation_status", mc.ASCENDING),
        ("task_execution_reconciliation_next_at", mc.ASCENDING),
        ("task_execution_reconciliation_lease_expires_at", mc.ASCENDING),
        ("task_execution_reconciliation_pending_at", mc.ASCENDING),
        ("queue_id", mc.ASCENDING),
    ]
    assert list(indexes["purge_after_ttl"]["key"].items()) == [
        ("purge_after", mc.ASCENDING)
    ]
    assert indexes["purge_after_ttl"]["expireAfterSeconds"] == 0
    assert indexes["purge_after_ttl"]["sparse"] is True


def test_window_session_binding_indexes_support_owner_cleanup_and_expiry() -> None:
    coll = mongomock.MongoClient().db.window_session_binding_indexes

    mc._ensure_window_session_binding_indexes(coll)

    indexes = {index["name"]: index for index in coll.list_indexes()}
    assert list(indexes["user_id_1"]["key"].items()) == [
        ("user_id", mc.ASCENDING)
    ]
    assert list(indexes["expires_at_ttl"]["key"].items()) == [
        ("expires_at", mc.ASCENDING)
    ]
    assert indexes["expires_at_ttl"]["expireAfterSeconds"] == 0


def test_workflow_instance_indexes_include_atlas_claim_and_lookup_indexes(monkeypatch):
    from src.backend.workflows.durable import instance_manager

    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(instance_manager, "get_db", lambda: db)
    monkeypatch.setattr(instance_manager, "_indexes_ensured", False)

    instance_manager._ensure_indexes()

    coll = db[instance_manager.WORKFLOW_INSTANCES_COLLECTION]
    calls_by_name = {
        str(call["kwargs"].get("name") or ""): call for call in coll.create_index_calls
    }
    assert calls_by_name["started_status_created_instance_lock"]["keys"] == [
        ("started_at", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("instance_id", mc.ASCENDING),
        ("lock_expires_at", mc.ASCENDING),
    ]
    assert calls_by_name["created_at_desc"]["keys"] == [("created_at", mc.DESCENDING)]
    assert calls_by_name["conversation_turn_namespace_created"]["keys"] == [
        ("inputs.conversation_session_id", mc.ASCENDING),
        ("inputs.turn_id", mc.ASCENDING),
        ("namespace", mc.ASCENDING),
        ("created_at", mc.DESCENDING),
    ]


def test_chat_history_indexes_include_session_created_lookup(monkeypatch):
    from src.backend.services import chat_history_service

    coll = _FakeCollection()
    monkeypatch.setattr(chat_history_service, "_CHAT_HISTORY_INDEXES_READY", False)

    chat_history_service._ensure_chat_history_indexes(coll)

    assert {
        str(call["kwargs"].get("name") or "") for call in coll.create_index_calls
    } >= {"session_id_1_created_at_1"}
