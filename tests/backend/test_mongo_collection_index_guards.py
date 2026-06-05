from __future__ import annotations

from typing import Any, cast

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
        str(call["kwargs"].get("name"))
        for call in coll_first.create_index_calls
    } == {
        "text_text_search",
        "text_1",
        "lang_1",
        "fingerprint_lang_unique",
        "created_at_-1",
        "updated_at_-1",
    }


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
        "updated_at_-1",
    }.issubset(concept_index_names)


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


def test_text_relation_indexes_include_atlas_name_lookup(monkeypatch):
    _reset_collection_index_state(monkeypatch)
    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(mc, "get_db", lambda: db)

    coll = cast(_FakeCollection, mc.get_text_relations_collection())

    assert coll is not None
    assert {
        str(call["kwargs"].get("name") or "")
        for call in coll.create_index_calls
    } >= {"context_name_type_predicate_text"}


def test_workflow_instance_indexes_include_atlas_claim_and_lookup_indexes(monkeypatch):
    from src.backend.workflows.durable import instance_manager

    db = _FakeDb("test_von_db", client=object())
    monkeypatch.setattr(instance_manager, "get_db", lambda: db)
    monkeypatch.setattr(instance_manager, "_indexes_ensured", False)

    instance_manager._ensure_indexes()

    coll = db[instance_manager.WORKFLOW_INSTANCES_COLLECTION]
    calls_by_name = {
        str(call["kwargs"].get("name") or ""): call
        for call in coll.create_index_calls
    }
    assert calls_by_name["started_status_created_instance_lock"]["keys"] == [
        ("started_at", mc.ASCENDING),
        ("status", mc.ASCENDING),
        ("created_at", mc.ASCENDING),
        ("instance_id", mc.ASCENDING),
        ("lock_expires_at", mc.ASCENDING),
    ]
    assert calls_by_name["created_at_desc"]["keys"] == [
        ("created_at", mc.DESCENDING)
    ]
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
        str(call["kwargs"].get("name") or "")
        for call in coll.create_index_calls
    } >= {"session_id_1_created_at_1"}
