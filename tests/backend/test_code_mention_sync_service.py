from __future__ import annotations

import src.backend.services.code_mention_sync_service as mention_service
from src.backend.vontology.code_concepts_registry import (
    MENTIONED_IN_VON_CODE_ID,
    MENTIONED_IN_VON_TEST_ID,
)


def test_sync_code_mentions_dry_run_reports_updates(monkeypatch):
    concept_doc = {"concept_id": "#V#alpha", "relationships": {"is_an_instance_of": []}}
    updates = []

    def fake_get(concept_id: str):
        if concept_id == "#V#alpha":
            return concept_doc
        raise mention_service.ConceptNotFoundError("missing")

    def fake_update(concept_id: str, payload):
        updates.append((concept_id, payload))
        return {}

    monkeypatch.setattr(
        mention_service.concept_service, "get_concept_by_concept_id", fake_get
    )
    monkeypatch.setattr(mention_service.concept_service, "update_concept", fake_update)

    result = mention_service.sync_code_mention_concepts(
        code_mentions={"#V#alpha"},
        test_mentions={"#V#beta"},
        dry_run=True,
    )

    assert updates == []
    assert result.updated == {"#V#alpha": [MENTIONED_IN_VON_CODE_ID]}
    assert result.missing == ["#V#beta"]


def test_sync_code_mentions_applies_updates(monkeypatch):
    concept_doc = {
        "concept_id": "#V#alpha",
        "relationships": {"is_an_instance_of": [MENTIONED_IN_VON_CODE_ID]},
    }
    updates = []

    def fake_get(concept_id: str):
        if concept_id == "#V#alpha":
            return concept_doc
        raise mention_service.ConceptNotFoundError("missing")

    def fake_update(concept_id: str, payload):
        updates.append((concept_id, payload))
        return {}

    monkeypatch.setattr(
        mention_service.concept_service, "get_concept_by_concept_id", fake_get
    )
    monkeypatch.setattr(mention_service.concept_service, "update_concept", fake_update)

    result = mention_service.sync_code_mention_concepts(
        code_mentions={"#V#alpha"},
        test_mentions={"#V#alpha"},
        dry_run=False,
    )

    assert result.updated == {"#V#alpha": [MENTIONED_IN_VON_TEST_ID]}
    assert updates
    concept_id, payload = updates[0]
    assert concept_id == "#V#alpha"
    assert payload["relationships"]["is_an_instance_of"] == [
        MENTIONED_IN_VON_CODE_ID,
        MENTIONED_IN_VON_TEST_ID,
    ]


def test_sync_code_mentions_skips_virtual(monkeypatch):
    concept_doc = {
        "concept_id": "#V#virtual",
        "relationships": {"is_an_instance_of": []},
        "metadata": {"virtual": True},
    }
    updates = []

    def fake_get(concept_id: str):
        if concept_id == "#V#virtual":
            return concept_doc
        raise mention_service.ConceptNotFoundError("missing")

    def fake_update(concept_id: str, payload):
        updates.append((concept_id, payload))
        return {}

    monkeypatch.setattr(
        mention_service.concept_service, "get_concept_by_concept_id", fake_get
    )
    monkeypatch.setattr(mention_service.concept_service, "update_concept", fake_update)

    result = mention_service.sync_code_mention_concepts(
        code_mentions={"#V#virtual"},
        test_mentions=set(),
        dry_run=False,
    )

    assert updates == []
    assert result.virtual == ["#V#virtual"]
