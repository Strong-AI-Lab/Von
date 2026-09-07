"""Maintained briefs must remain usable after ordinary delegated edits."""

from contextlib import nullcontext

import mongomock
import pytest
from bson import ObjectId

from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.services import ontology_mutation_command_service as command
from src.backend.services import ontology_publication_authority_service as authority
from src.backend.services import text_value_service as texts
from src.backend.services.adaptive_turn_service import (
    _build_effect_outcome_report,
    _canonical_effect_readback_receipt,
    _canonically_verified_material_effect_ids,
    _model_visible_input_schema,
    _trusted_tool_payload,
    ordinary_turn_capability_delegation,
)
from src.backend.services.turn_evidence_store import TrustedTurnScope


@pytest.fixture
def editing(monkeypatch):
    db = mongomock.MongoClient()["maintained_text"]
    db.relations.create_index(
        [("subject_concept_id", 1), ("predicate", 1), ("object_text_id", 1)],
        unique=True,
    )
    db.values.create_index([("fingerprint", 1), ("lang", 1)], unique=True)
    monkeypatch.setattr(TextRelationsRepository, "collection", lambda: db.relations)
    monkeypatch.setattr(TextValuesRepository, "collection", lambda: db.values)
    monkeypatch.setattr(texts, "can_access_concept", lambda _: True)
    monkeypatch.setattr(texts, "_emit_text_relation_mutation_event", lambda **_: None)
    monkeypatch.setattr(
        texts,
        "_invalidate_workflow_routing_projection_for_text_relation_change",
        lambda **_: None,
    )
    monkeypatch.setattr(
        authority,
        "get_ontology_authority_delegations_collection",
        lambda: db.delegations,
    )
    monkeypatch.setattr(
        authority, "get_ontology_mutation_receipts_collection", lambda: db.receipts
    )
    monkeypatch.setattr(
        command, "ontology_mutation_resource_lock", lambda _: nullcontext()
    )
    monkeypatch.setattr(command, "can_access_concept", lambda _: True)
    monkeypatch.setattr(authority, "can_access_concept", lambda _: True)
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda _: authority.PublicationContext.user("#V#alice"),
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    return db, gateway


def test_ordinary_editor_is_discoverable_and_retains_old_text(editing):
    _, gateway = editing
    name = "upsert_singleton_text_relation"
    assert name in ordinary_turn_capability_delegation(
        gateway,
        user_concept_id="#V#alice",
        trusted_argument_values={"turn_namespace": "#V#alice"},
    )
    assert name not in ordinary_turn_capability_delegation(
        gateway, user_concept_id=None
    )
    schema = _model_visible_input_schema(gateway.get_method_definition(name))
    assert {"concept_id", "predicate", "language", "text", "context"} <= set(
        schema["properties"]
    )
    assert not {"namespace", "provenance", "garbage_collect", "policy"} & set(
        schema["properties"]
    )
    payload = _trusted_tool_payload(
        gateway=gateway,
        tool_name=name,
        model_payload={
            "concept_id": "#V#brief",
            "predicate": "hasContent",
            "text": "current",
            "garbage_collect": True,
            "provenance": {"actor": "#V#other"},
        },
        trusted_argument_values={"turn_namespace": "#V#alice"},
    )
    assert payload["garbage_collect"] is False
    assert payload["provenance"] is None
    assert payload["namespace"] == "#V#alice"


@pytest.mark.parametrize("actor", ["#V#alice", "#V#bob"])
def test_edit_consolidates_only_authorised_group_with_readback_and_recovery(
    editing, actor
):
    db, gateway = editing
    originals = []
    for text, predicate, language in [
        ("old brief", "hasContent", "en-NZ"),
        ("competing brief", "hasContent", "en-NZ"),
        ("French brief", "hasContent", "fr"),
        ("independent note", "hasNote", "en-NZ"),
    ]:
        originals.append(
            texts.upsert_text_for_concept(
                subject_concept_id="#V#brief",
                predicate=predicate,
                text=text,
                lang=language,
                context={"source": text},
            )
        )
    args = _trusted_tool_payload(
        gateway=gateway,
        tool_name="upsert_singleton_text_relation",
        model_payload={
            "concept_id": "#V#brief",
            "predicate": "hasContent",
            "text": "Consolidated current brief",
        },
        trusted_argument_values={"turn_namespace": actor},
    )
    with authority.override_current_actor(actor, None):
        grant = command.issue_same_turn_method_delegation(
            method_name="upsert_singleton_text_relation",
            arguments=args,
            actor_concept_id=actor,
            organisation_concept_id=None,
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id="edit-brief",
            turn_id="continuation-turn",
        )
        if actor != "#V#alice":
            assert not grant.get("delegation_id")
            assert db.relations.count_documents({}) == 4
            assert db.values.count_documents({}) == 4
            return
        assert grant.get("delegation_id"), grant
        with authority.bind_ontology_invocation(
            surface="adaptive_turn",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            delegation_id=grant["delegation_id"],
            effect_id="edit-brief",
            turn_id="continuation-turn",
        ):
            result = gateway.invoke("upsert_singleton_text_relation", args).payload
            repeated = gateway.invoke("upsert_singleton_text_relation", args).payload
    assert result["success"] is True, result
    assert result["authority_receipt"]["status"] == "succeeded"
    assert result["authority_receipt"]["canonical_read_back"]["verified"] is True
    receipt = _canonical_effect_readback_receipt(result)
    material, verified = _canonically_verified_material_effect_ids(
        [
            {
                "effect_id": "edit-brief",
                "status": "ok",
                "execution_method": "upsert_singleton_text_relation",
            }
        ],
        {
            "edit-brief": {
                "effect_status": "succeeded",
                "changed": True,
                "turn_finality_required": True,
                "canonical_readback": receipt,
            }
        },
    )
    assert material == verified == {"edit-brief"}
    screen, report = _build_effect_outcome_report(
        terminal_status="effect_partially_completed",
        effect_snapshot={
            "edit-brief": {
                "effect_status": "succeeded",
                "changed": True,
                "canonical_readback": receipt,
            }
        },
        tool_invocations=[
            {"effect_id": "edit-brief", "tool": "upsert_singleton_text_relation"}
        ],
        trusted_scope=TrustedTurnScope(
            user_concept_id=actor, organisation_concept_id=None, namespace=actor
        ),
    )
    assert report["facts"][0]["canonical_readback_verified"] is True
    assert "succeeded with canonical read-back" in screen
    assert "did not verify" not in screen
    assert result["canonical_read_back"]["matching_relation_count"] == 1
    assert result["canonical_read_back"]["relation_id"] == result["kept_relation_id"]
    assert result["replaced_count"] == 2
    assert result["replaced_text_values_retained"] is True
    assert repeated["authority_receipt"]["status"] == "succeeded"
    assert repeated["canonical_read_back"]["verified"] is True
    assert db.relations.count_documents({}) == 3
    assert db.values.count_documents({}) == 5
    for original in originals[2:]:
        assert db.relations.find_one({"_id": ObjectId(original["relation_id"])})
    # Recovery uses the retained canonical text and recorded attachment context.
    for old in result["replaced_relations"]:
        value = db.values.find_one({"_id": ObjectId(old["text_value_id"])})
        assert value and value["text"] == old["context"]["source"]
        texts.link_text_to_concept(
            "#V#brief", "hasContent", old["text_value_id"], old["context"]
        )
    assert db.relations.count_documents({}) == 5


def test_singleton_postcondition_rejects_a_competing_body(editing):
    _, _gateway = editing
    args = {"concept_id": "#V#brief", "predicate": "hasContent", "text": "current"}
    for text in ["current", "unremoved competing body"]:
        row = texts.upsert_text_for_concept(
            subject_concept_id="#V#brief",
            predicate="hasContent",
            text=text,
            lang="en-NZ",
        )
        if text == "current":
            kept_id = row["relation_id"]
    state = command.canonical_read_back_for_method(
        method_name="upsert_singleton_text_relation",
        arguments=args,
        result={"kept_relation_id": kept_id},
    )
    with authority.override_current_actor("#V#alice", None):
        intent = command.build_ontology_mutation_intent(
            method_name="upsert_singleton_text_relation",
            arguments=args,
        )
    assert state["matching_relation_count"] == 2
    assert not command._verify_method_postcondition(
        method_name="upsert_singleton_text_relation",
        intent=intent,
        result={"success": True},
        canonical_state=state,
    )
