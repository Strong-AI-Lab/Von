from __future__ import annotations

from typing import Dict, Any, List


def test_sync_code_predicate_creates_missing_concepts(monkeypatch):
    from src.backend.services import code_predicate_sync_service as sync_service

    created: List[Dict[str, Any]] = []
    looked_up: List[str] = []

    def _fake_get(concept_id: str):
        looked_up.append(concept_id)
        if concept_id in {
            sync_service.PREDICATE_TYPE_ID,
            sync_service.MENTIONED_IN_CODE_ID,
        }:
            return {"concept_id": concept_id, "relationships": {}}
        return None

    def _fake_create(**kwargs):
        created.append(kwargs)
        return {"concept_id": kwargs.get("concept_id")}

    monkeypatch.setattr(
        sync_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )
    monkeypatch.setattr(sync_service.concept_service, "create_concept", _fake_create)
    monkeypatch.setattr(sync_service.concept_service, "update_concept", lambda *a, **k: {})
    monkeypatch.setattr(
        sync_service, "list_code_predicate_ids", lambda: ["#V#hasContent"]
    )

    result = sync_service.sync_code_predicate_concepts()

    assert result.created == ["#V#hasContent"]
    assert created[0]["concept_id"] == "#V#hasContent"
    assert created[0]["parent_concept_ids"] == [
        sync_service.PREDICATE_TYPE_ID,
        sync_service.MENTIONED_IN_CODE_ID,
    ]


def test_sync_code_predicate_updates_instance_of(monkeypatch):
    from src.backend.services import code_predicate_sync_service as sync_service

    updates: List[Dict[str, Any]] = []

    def _fake_get(concept_id: str):
        if concept_id in {
            sync_service.PREDICATE_TYPE_ID,
            sync_service.MENTIONED_IN_CODE_ID,
        }:
            return {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#hasContent":
            return {
                "concept_id": concept_id,
                "relationships": {"is_an_instance_of": [sync_service.PREDICATE_TYPE_ID]},
            }
        return None

    def _fake_update(concept_id: str, payload: Dict[str, Any]):
        updates.append({"concept_id": concept_id, "payload": payload})
        return {"concept_id": concept_id}

    monkeypatch.setattr(
        sync_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )
    monkeypatch.setattr(sync_service.concept_service, "create_concept", lambda *a, **k: {})
    monkeypatch.setattr(sync_service.concept_service, "update_concept", _fake_update)
    monkeypatch.setattr(
        sync_service, "list_code_predicate_ids", lambda: ["#V#hasContent"]
    )

    result = sync_service.sync_code_predicate_concepts()

    assert result.updated == ["#V#hasContent"]
    assert updates
    updated_inst_of = updates[0]["payload"]["relationships"]["is_an_instance_of"]
    assert sync_service.MENTIONED_IN_CODE_ID in updated_inst_of


def test_sync_code_predicate_retags_non_predicate(monkeypatch):
    from src.backend.services import code_predicate_sync_service as sync_service

    updates: List[Dict[str, Any]] = []

    def _fake_get(concept_id: str):
        if concept_id in {
            sync_service.PREDICATE_TYPE_ID,
            sync_service.MENTIONED_IN_CODE_ID,
        }:
            return {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#has_blob_uri":
            return {"concept_id": concept_id, "relationships": {"is_an_instance_of": []}}
        return None

    def _fake_update(concept_id: str, payload: Dict[str, Any]):
        updates.append({"concept_id": concept_id, "payload": payload})
        return {"concept_id": concept_id}

    monkeypatch.setattr(
        sync_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )
    monkeypatch.setattr(sync_service.concept_service, "create_concept", lambda *a, **k: {})
    monkeypatch.setattr(sync_service.concept_service, "update_concept", _fake_update)
    monkeypatch.setattr(
        sync_service, "list_code_predicate_ids", lambda: ["#V#has_blob_uri"]
    )

    result = sync_service.sync_code_predicate_concepts(retag_non_predicates=True)

    assert result.updated == ["#V#has_blob_uri"]
    assert updates
    updated_inst_of = updates[0]["payload"]["relationships"]["is_an_instance_of"]
    assert sync_service.PREDICATE_TYPE_ID in updated_inst_of


def test_sync_code_predicate_creates_for_virtual_concept(monkeypatch):
    from src.backend.services import code_predicate_sync_service as sync_service

    created: List[Dict[str, Any]] = []

    def _fake_get(concept_id: str):
        if concept_id in {
            sync_service.PREDICATE_TYPE_ID,
            sync_service.MENTIONED_IN_CODE_ID,
        }:
            return {"concept_id": concept_id, "relationships": {}}
        if concept_id == "#V#hasContent":
            return {"concept_id": concept_id, "metadata": {"virtual": True}}
        return None

    def _fake_create(**kwargs):
        created.append(kwargs)
        return {"concept_id": kwargs.get("concept_id")}

    monkeypatch.setattr(
        sync_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get,
    )
    monkeypatch.setattr(sync_service.concept_service, "create_concept", _fake_create)
    monkeypatch.setattr(sync_service.concept_service, "update_concept", lambda *a, **k: {})
    monkeypatch.setattr(
        sync_service, "list_code_predicate_ids", lambda: ["#V#hasContent"]
    )

    result = sync_service.sync_code_predicate_concepts()

    assert result.created == ["#V#hasContent"]
    assert created
