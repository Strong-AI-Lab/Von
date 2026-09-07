"""Public retrieval plus governed identity, provenance, read-back and export."""

import json
import os
import re
from copy import deepcopy
from pathlib import Path

import mongomock
import pytest

from src.backend.integrations.internal_mcp import knowkat_tools as tools
from src.backend.integrations.internal_mcp import knowkat_proxy_mcp as proxy
from src.backend.services.ontology_source_adoption_service import (
    build_adoption_proposal,
)
from src.backend.security.access_control import override_current_actor

IRI = "https://example.org/Bicycle"
OWNER = "#V#knowkat_fixture_owner"
PARENT = "#V#fixture_mechanical_entity"


def source(identifier=IRI):
    return {
        "concept": {
            "term": {"kind": "iri", "value": identifier},
            "labels": [{"value": "bicycle", "language": "en"}],
        },
        "statements": [
            {
                "statement_id": "a" * 64 + ":1",
                "subject": {"kind": "iri", "value": identifier},
                "predicate": {
                    "kind": "iri",
                    "value": "http://www.w3.org/2000/01/rdf-schema#subClassOf",
                },
                "object": {"kind": "blank_node", "value": "b0"},
            }
        ],
        "next_cursor": None,
        "provenance": {
            "kb_id": "fixture:1",
            "version": "1",
            "source_url": "https://example.org/ontology.owl",
            "source_sha256": "b" * 64,
            "index_sha256": "a" * 64,
            "licences": ["CC-BY-3.0"],
            "licence_urls": ["https://creativecommons.org/licenses/by/3.0/"],
            "attribution": "Fixture author",
            "source_notice": "Complete source notice",
            "parser": "fixture",
            "transformations": ["parse only"],
            "licence_decision": {"admitted": True, "policy_sha256": "c" * 64},
        },
    }


def proposal(
    data=None,
    name="Bicycle",
    parent=PARENT,
    description="Local interpretation: a mechanical entity to maintain.",
):
    data = data or source()
    return build_adoption_proposal(
        data,
        identifier=data["concept"]["term"]["value"],
        local_name=name,
        local_kind="type",
        parent_id=parent,
        local_description=description,
    )


@pytest.fixture
def store(monkeypatch):
    from src.backend.db import mongo_client
    from src.backend.db.repositories.text_value_repository import TextValuesRepository

    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_knowkat_canonical")
    monkeypatch.setattr(mongo_client, "_mongo_client_mock", mongomock.MongoClient())
    # Mongomock lacks MongoDB $text. Emulate only candidate acquisition; exact
    # marker checks, identity resolution, writes, authority and read-back are real.
    original = TextValuesRepository.find

    def find(query, **kwargs):
        if "$text" in query:
            query = {"text": {"$regex": re.escape(query["$text"]["$search"])}}
        return original(query, **kwargs)

    monkeypatch.setattr(TextValuesRepository, "find", staticmethod(find))
    from src.backend.services import concept_service

    with override_current_actor(OWNER, None):
        concept_service.create_concept(
            name="Fixture mechanical entity",
            concept_id=PARENT,
            create_as_instance=False,
            visibility_scope_mode="global_general",
        )
    return mongo_client.get_db()


def adopt_and_verify(data, **local):
    from src.backend.integrations.internal_mcp.catalogue import _create_concepts
    from src.backend.services import concept_service
    from src.backend.services.text_value_service import get_texts_for_concept

    p = proposal(data, **local)
    with override_current_actor(OWNER, None):
        created = _create_concepts(**p["create_concepts_arguments"])
        assert created["success"], created
        cid = created["created_concept_ids"][0]
        repeated = _create_concepts(**p["create_concepts_arguments"])
        assert repeated["success"] and repeated["idempotent_reuse"], repeated
        assert repeated["resolved_concept_ids"] == [cid]
        rows = get_texts_for_concept(cid)
        assert any(x["text"] == p["provenance_text"]["text"] for x in rows)
        exported, count = concept_service.export_concepts(
            cid, include_descendants=False
        )
        assert count == 1
        assert any(
            x["text"] == p["provenance_text"]["text"]
            for x in exported[0]["text_relations"]
        )
        assert exported[0]["relationships"]["is_a_type_of"] == [
            p["create_concepts_arguments"]["parent_id"]
        ]
        assert (
            len([x for x in rows if x.get("context", {}).get("identity_marker")]) == 1
        )
    with override_current_actor("#V#different_actor", None):
        exported, count = concept_service.export_concepts(
            cid, include_descendants=False
        )
        assert count == 0 and exported == []
    return cid, created, repeated, p


def test_governed_adoption_repeat_readback_export_and_homonyms(store):
    first, *_ = adopt_and_verify(source())
    second, *_ = adopt_and_verify(source("https://example.org/AnotherBicycle"))
    assert first != second
    assert store.concepts.count_documents({"concept_id": {"$in": [first, second]}}) == 2


def test_source_evidence_stays_separate_and_partial_is_explicit():
    data = source()
    data["next_cursor"] = "next-page"
    p = proposal(data)
    assert p["effect_status"] == "not_executed"
    assert not p["proposal"]["source_page"]["complete_outgoing_statements"]
    assert p["proposal"]["source_assertions"][0]["object"]["kind"] == "blank_node"
    assert (
        p["create_concepts_arguments"]["concepts"][0]["notes"]
        == p["provenance_text"]["text"]
    )
    bad = deepcopy(data)
    bad["provenance"]["licence_decision"]["admitted"] = False
    with pytest.raises(ValueError, match="attribution"):
        proposal(bad)


def test_unconfigured_private_and_override_rejected_before_launch(monkeypatch):
    monkeypatch.setattr(proxy, "configuration", lambda: {})
    assert tools.call_knowkat("list_knowledge_bases")["success"] is False
    with pytest.raises(PermissionError):
        proxy.get_knowkat_client()
    monkeypatch.setattr(proxy, "configuration", lambda: {proxy.PUBLIC_ENV: "true"})
    with pytest.raises(ValueError, match="not configured"):
        proxy.get_knowkat_client()
    result = tools.call_knowkat(
        "search_concepts", kb_id="x", query="bicycle", policy="allow-anything"
    )
    assert result["error_code"] == "invalid_knowkat_arguments"


def test_public_projection_and_revocation(monkeypatch):
    from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue

    monkeypatch.setattr(proxy, "configuration", lambda: {proxy.PUBLIC_ENV: "true"})
    assert all(d.ordinary_turn_public for d in tools.definitions())
    assert build_default_catalogue().get("knowkat_get_concept").ordinary_turn_public
    monkeypatch.setattr(proxy, "configuration", lambda: {proxy.PUBLIC_ENV: "false"})
    assert not any(d.ordinary_turn_public for d in tools.definitions())
    assert tools.call_knowkat("list_knowledge_bases")["success"] is False


def test_real_configured_source_through_von_and_durable_adoption(store, monkeypatch):
    command = os.environ.get("KNOWKAT_ACCEPTANCE_COMMAND_JSON")
    if not command:
        pytest.skip(
            "Set KNOWKAT_ACCEPTANCE_COMMAND_JSON for the complete real-source acceptance"
        )
    monkeypatch.setenv(proxy.COMMAND_ENV, command)
    monkeypatch.setenv(proxy.PUBLIC_ENV, "true")
    inventory = tools.call_knowkat("list_knowledge_bases")
    assert inventory["knowledge_bases"][0]["statement_count"] == 2413894
    evidence = {"inventory": inventory, "search": {}}
    for query in ("bicycle", "maintenance", "changing bicycle tire"):
        evidence["search"][query] = tools.call_knowkat(
            "search_concepts", kb_id="opencyc:owl-2012", query=query
        )
        assert "matches" in evidence["search"][query]
    iris = [
        "http://sw.opencyc.org/concept/Mx4rvVjo0JwpEbGdrcN5Y29ycA",
        "http://sw.opencyc.org/concept/Mx4ra0BQ1XDWS-WZYzoD0E9HVA",
    ]
    evidence["adoptions"] = []
    for index, iri in enumerate(iris):
        data = tools.call_knowkat(
            "get_concept", kb_id="opencyc:owl-2012", identifier=iri, limit=200
        )
        if index:
            from src.backend.services import concept_service

            with override_current_actor(OWNER, None):
                concept_service.create_concept(
                    name="Fixture maintenance action",
                    concept_id="#V#fixture_maintenance_action",
                    create_as_instance=False,
                    visibility_scope_mode="global_general",
                )
            local = dict(
                name="Changing a bicycle tyre",
                parent="#V#fixture_maintenance_action",
                description="Local interpretation: an action for replacing a bicycle tyre; inspect source assertions for its exact original meaning.",
            )
        else:
            local = dict(name="Bicycle")
        cid, created, repeated, p = adopt_and_verify(data, **local)
        evidence["adoptions"].append(
            {"concept_id": cid, "created": created, "repeated": repeated, "proposal": p}
        )
    evidence["neighbourhood"] = tools.call_knowkat(
        "get_ontology_neighbourhood",
        kb_id="opencyc:owl-2012",
        identifier=iris[0],
        limit=10,
    )
    page = tools.call_knowkat(
        "get_statements", kb_id="opencyc:owl-2012", subject=iris[0], limit=1
    )
    assert page["next_cursor"]
    evidence["next_page"] = tools.call_knowkat(
        "get_statements",
        kb_id="opencyc:owl-2012",
        subject=iris[0],
        limit=1,
        cursor=page["next_cursor"],
    )
    from src.backend.integrations.internal_mcp.catalogue import (
        _create_concepts,
        _add_relationship,
    )
    from src.backend.services import concept_service

    with override_current_actor(OWNER, None):
        local_notes = json.dumps(
            {
                "origin": "local extension, not an OpenCyc assertion",
                "source_adoptions": [
                    {
                        "concept_id": row["concept_id"],
                        "source_provenance": row["proposal"]["proposal"][
                            "source_provenance"
                        ],
                    }
                    for row in evidence["adoptions"]
                ],
            },
            sort_keys=True,
        )
        extension = _create_concepts(
            parent_id="#V#fixture_maintenance_action",
            scope_mode="user_only_default",
            concepts=[
                {
                    "name": "Bicycle maintenance",
                    "kind": "type",
                    "description": "Local maintenance category for keeping a bicycle serviceable, including tyre replacement. No exact OpenCyc equivalence is asserted.",
                    "notes": local_notes,
                }
            ],
        )
        assert extension["success"], extension
        extension_id = extension["created_concept_ids"][0]
        # Fixture vocabulary is existing local infrastructure, not imported taxonomy.
        concept_service.create_concept(
            name="Has maintenance subject",
            concept_id="#V#fixture_has_maintenance_subject",
            create_as_instance=True,
            instance_of_type="#V#predicate",
            visibility_scope_mode="global_general",
        )
        relation = _add_relationship(
            source_id=extension_id,
            predicate="#V#fixture_has_maintenance_subject",
            target=evidence["adoptions"][0]["concept_id"],
        )
        assert relation["success"], relation
        exported, count = concept_service.export_concepts(
            extension_id, include_descendants=False
        )
        assert count == 1 and any(
            row["text"] == local_notes for row in exported[0]["text_relations"]
        )
        assert exported[0]["relationships"]["#V#fixture_has_maintenance_subject"] == [
            evidence["adoptions"][0]["concept_id"]
        ]
        evidence["local_extension"] = {
            "creation": extension,
            "relation": relation,
            "export": exported,
        }
    destination = os.environ.get("KNOWKAT_ACCEPTANCE_EVIDENCE")
    if destination:
        Path(destination).write_text(json.dumps(evidence, indent=2, default=str) + "\n")


def test_exact_agent_delegation_issues_and_rejects_changed_source(store):
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services.ontology_publication_authority_service import (
        bind_ontology_invocation,
    )
    from src.backend.integrations.internal_mcp.catalogue import _create_concepts

    args = proposal()["create_concepts_arguments"]
    with override_current_actor(OWNER, None):
        with bind_ontology_invocation(
            surface="internal_mcp",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
        ):
            denied = _create_concepts(**args)
        assert (
            not denied["success"]
            and denied["error_code"] == "ontology_agent_delegation_required"
        )
        grant = command.issue_same_turn_method_delegation(
            method_name="create_concepts",
            arguments=args,
            actor_concept_id=OWNER,
            organisation_concept_id=None,
            delegate_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id="fixture-adoption",
            turn_id="fixture-turn",
        )
        assert grant.get("delegation_id"), grant
        with bind_ontology_invocation(
            surface="internal_mcp",
            executing_agent_concept_id="#V#von_system",
            audience="adaptive_turn",
            effect_id="fixture-adoption",
            turn_id="fixture-turn",
            delegation_id=grant["delegation_id"],
        ):
            changed = deepcopy(args)
            changed["concepts"][0]["external_identifiers"][0][
                "value"
            ] = "https://example.org/different"
            rejected = _create_concepts(**changed)
            assert not rejected["success"]
            created = _create_concepts(**args)
        assert created["success"], created
        assert created["authority_receipt"]["status"] == "succeeded"


def test_scope_and_marker_collision_cannot_enlarge_adoption_authority(store):
    from src.backend.integrations.internal_mcp.catalogue import _create_concepts
    from src.backend.services.concept_external_identity_service import (
        canonical_concept_id_for_external_identifiers,
        normalise_create_external_identifiers,
    )
    from src.backend.services import concept_service

    args = proposal()["create_concepts_arguments"]
    with override_current_actor(OWNER, None):
        wide = {**args, "scope_mode": "global_general"}
        denied = _create_concepts(**wide)
        assert not denied["success"]
        ids = normalise_create_external_identifiers(
            external_identifiers=args["concepts"][0]["external_identifiers"],
            concept_name="Bicycle",
            kind="type",
        )
        cid = canonical_concept_id_for_external_identifiers(
            ids,
            kind="type",
            parent_id=PARENT,
            scope_mode="user_only_default",
            actor_user_id=OWNER,
        )
        concept_service.create_concept(
            name="Unverified occupant",
            concept_id=cid,
            parent_concept_ids=[PARENT],
            create_as_instance=False,
            created_by_concept_id=OWNER,
            visibility_scope_mode="user_only_default",
        )
        conflict = _create_concepts(**args)
        assert (
            not conflict["success"]
            and conflict["error_code"] == "external_identity_requires_review"
        )


def test_missing_marker_prevents_successful_canonical_postcondition(store, monkeypatch):
    from src.backend.services import concept_external_identity_service as identities
    from src.backend.integrations.internal_mcp.catalogue import _create_concepts

    monkeypatch.setattr(
        identities,
        "persist_external_identity_markers",
        lambda **kw: {
            "success": True,
            "effect_status": "succeeded",
            "changed": True,
            "writes": [],
        },
    )
    with override_current_actor(OWNER, None):
        result = _create_concepts(**proposal()["create_concepts_arguments"])
        assert not result["success"], result
        assert result["authority_receipt"]["status"] != "succeeded"


def test_export_retains_notes_beyond_initial_batch(store):
    from src.backend.services import concept_service
    from src.backend.services.text_value_service import upsert_text_for_concept

    with override_current_actor(OWNER, None):
        cid = concept_service.create_concept(
            name="Versioned source",
            concept_id="#V#fixture_versioned_source",
            create_as_instance=False,
            created_by_concept_id=OWNER,
            visibility_scope_mode="user_only_default",
        )["concept_id"]
        for number in range(105):
            upsert_text_for_concept(
                subject_concept_id=cid,
                predicate="hasNote",
                text=f"Source revision {number}",
                lang="en",
            )
        exported, count = concept_service.export_concepts(
            cid, include_descendants=False
        )
        assert count == 1
        assert {
            row["text"]
            for row in exported[0]["text_relations"]
            if row["predicate"] == "hasNote"
        } == {f"Source revision {n}" for n in range(105)}


def test_unavailable_executable_returns_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        proxy,
        "configuration",
        lambda: {
            proxy.PUBLIC_ENV: "true",
            proxy.COMMAND_ENV: json.dumps([str(tmp_path / "missing-knowkat")]),
        },
    )
    response = tools.call_knowkat("list_knowledge_bases")
    assert (
        response["success"] is False and response["error_code"] == "knowkat_unavailable"
    )
