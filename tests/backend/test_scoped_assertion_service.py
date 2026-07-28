from __future__ import annotations

import mongomock
import pytest


def _collection():
    return mongomock.MongoClient()["von_test"]["scoped_knowledge_assertions"]


def test_visible_global_subject_accepts_user_scoped_text_assertion(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    first = service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Gillian leads our research programme.",
        scope_mode="organisation",
        evidence={"source": "user_instruction"},
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        turn_id="turn-1",
        canonical_publication=False,
    )
    second = service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Gillian leads our research programme.",
        scope_mode="organisation",
        evidence={"source": "user_instruction"},
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
        namespace="#V#michael_witbrock@university_of_auckland_strong_ai_lab",
        turn_id="turn-1",
        canonical_publication=False,
    )

    assert first["success"] is True
    assert first["changed"] is True
    assert second["changed"] is False
    assert first["assertion_id"] == second["assertion_id"]
    assert collection.count_documents({}) == 1
    read_back = first["canonical_read_back"]
    assert read_back["subject_concept_id"] == "#V#gillian_dobbie"
    assert read_back["scope"]["mode"] == "organisation"
    assert read_back["canonical_publication"] is False
    assert read_back["provenance"]["turn_id"] == "turn-1"


def test_scoped_assertions_do_not_cross_actor_or_organisation(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Organisation-private knowledge.",
        scope_mode="organisation",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#organisation_a",
        namespace="#V#michael_witbrock@organisation_a",
        canonical_publication=False,
    )

    same_org = service.list_visible_scoped_assertions(
        subject_concept_ids=["#V#gillian_dobbie"],
        user_concept_id="#V#someone_else",
        organisation_concept_id="#V#organisation_a",
    )
    other_org = service.list_visible_scoped_assertions(
        subject_concept_ids=["#V#gillian_dobbie"],
        user_concept_id="#V#someone_else",
        organisation_concept_id="#V#organisation_b",
    )

    assert len(same_org) == 1
    assert other_org == []


def test_service_rejects_namespace_that_disagrees_with_trusted_actor(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        _collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)

    with pytest.raises(ValueError, match="namespace does not match"):
        service.upsert_scoped_assertion(
            subject_concept_id="#V#gillian_dobbie",
            predicate="hasDescription",
            target_text="Attempted scope substitution.",
            acting_user_concept_id="#V#michael_witbrock",
            organisation_concept_id="#V#trusted_org",
            namespace="#V#attacker@other_org",
            canonical_publication=False,
        )


def test_concept_target_assertion_is_retrievable_from_either_argument(monkeypatch):
    from src.backend.services import scoped_assertion_service as service

    collection = _collection()
    monkeypatch.setattr(
        service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(service, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(
        service,
        "validate_predicate_concept",
        lambda _predicate: (True, None, None),
    )
    receipt = service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="#V#co_directs",
        target_concept_id="#V#centre_for_machine_learning_for_social_good",
        scope_mode="user",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )

    from_subject = service.list_visible_scoped_assertions(
        argument_concept_id="#V#gillian_dobbie",
        object_kind="concept",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
    )
    from_target = service.list_visible_scoped_assertions(
        argument_concept_id="#V#centre_for_machine_learning_for_social_good",
        object_kind="concept",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
    )

    assert receipt["success"] is True
    assert [item["assertion_id"] for item in from_subject] == [receipt["assertion_id"]]
    assert [item["assertion_id"] for item in from_target] == [receipt["assertion_id"]]


def test_trusted_tool_payload_replaces_model_supplied_scope():
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )
    from src.backend.services.adaptive_turn_service import _trusted_tool_payload

    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name="upsert_scoped_assertion",
        model_payload={
            "subject_concept_id": "#V#gillian_dobbie",
            "predicate": "hasDescription",
            "target_text": "Scoped fact",
            "acting_user_concept_id": "#V#attacker",
            "organisation_concept_id": "#V#other_org",
            "namespace": "#V#attacker@other_org",
            "turn_id": "forged-turn",
            "canonical_publication": True,
        },
        trusted_argument_values={
            "actor_user_concept_id": "#V#michael_witbrock",
            "actor_organisation_concept_id": "#V#trusted_org",
            "turn_namespace": "#V#michael_witbrock@trusted_org",
            "turn_id": "trusted-turn",
        },
    )

    assert payload["acting_user_concept_id"] == "#V#michael_witbrock"
    assert payload["organisation_concept_id"] == "#V#trusted_org"
    assert payload["namespace"] == "#V#michael_witbrock@trusted_org"
    assert payload["turn_id"] == "trusted-turn"
    assert payload["canonical_publication"] is False


def test_generic_text_read_merges_visible_scoped_assertions(monkeypatch):
    from src.backend.security.access_control import override_current_actor
    from src.backend.services import scoped_assertion_service as scoped_service
    from src.backend.services import text_value_service

    collection = _collection()
    monkeypatch.setattr(
        scoped_service,
        "get_scoped_knowledge_assertions_collection",
        lambda: collection,
    )
    monkeypatch.setattr(
        scoped_service,
        "can_access_concept",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        text_value_service.TextRelationsRepository,
        "find",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        text_value_service,
        "filter_accessible_concept_ids",
        lambda concept_ids: set(concept_ids),
    )
    scoped_service.upsert_scoped_assertion(
        subject_concept_id="#V#gillian_dobbie",
        predicate="hasDescription",
        target_text="Scoped programme context.",
        acting_user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#trusted_org",
        namespace="#V#michael_witbrock@trusted_org",
        canonical_publication=False,
    )

    with override_current_actor("#V#michael_witbrock", "#V#trusted_org"):
        rows = text_value_service.get_texts_for_concept(
            "#V#gillian_dobbie",
            predicate="hasDescription",
        )

    assert len(rows) == 1
    assert rows[0]["text"] == "Scoped programme context."
    assert rows[0]["assertion_id"].startswith("ska_")
    assert rows[0]["canonical_publication"] is False
    assert rows[0]["storage_surface"] == "scoped_knowledge_assertions"
