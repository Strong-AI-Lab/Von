from __future__ import annotations

from typing import Any

from bson import ObjectId
from flask import Flask

from src.backend.server.routes import concept_routes
from src.backend.services import ontology_mutation_command_service as command
from src.backend.services import ontology_publication_authority_service as authority


def _client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")
    return app.test_client()


def test_http_text_post_binds_the_exact_canonical_arguments_to_the_write(
    monkeypatch,
) -> None:
    authorised: dict[str, Any] = {}
    mutated: dict[str, Any] = {}

    def execute(**kwargs: Any) -> dict[str, Any]:
        authorised.update(kwargs["arguments"])
        result = dict(kwargs["mutate"]())
        return {"success": True, **result}

    def upsert(**kwargs: Any) -> dict[str, Any]:
        mutated.update(kwargs)
        return {
            "relation_id": "relation-1",
            "relation_created": True,
            "context_updated": False,
        }

    monkeypatch.setattr(concept_routes, "_execute_governed_http_mutation", execute)
    monkeypatch.setattr(concept_routes, "upsert_text_for_concept", upsert)
    monkeypatch.setattr(
        concept_routes,
        "maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(concept_routes, "_get_request_namespace", lambda: None)

    response = _client().post(
        "/api/concepts/%23V%23record/texts",
        json={
            "predicate": "#V#hasName",
            "text": "  Exact name  ",
            "lang": "mi-NZ",
            "context": {"name_type": "abbr", "register": "formal"},
            "provenance": {"source": "curated_import", "record": "17"},
            "request_id": "text-effect-1",
        },
    )

    assert response.status_code == 201
    assert authorised == {
        "concept_id": "#V#record",
        "predicate": "hasName",
        "text": "Exact name",
        "language": "mi-NZ",
        "context": {"name_type": "ABBR", "register": "formal"},
        "provenance": {"source": "curated_import", "record": "17"},
        "request_id": "text-effect-1",
    }
    assert mutated == {
        "subject_concept_id": authorised["concept_id"],
        "predicate": authorised["predicate"],
        "text": authorised["text"],
        "lang": authorised["language"],
        "context": authorised["context"],
        "provenance": authorised["provenance"],
    }


def test_http_text_patch_binds_server_provenance_and_language_to_the_write(
    monkeypatch,
) -> None:
    authorised: dict[str, Any] = {}
    mutated: dict[str, Any] = {}

    def execute(**kwargs: Any) -> dict[str, Any]:
        authorised.update(kwargs["arguments"])
        result = dict(kwargs["mutate"]())
        return {"success": True, **result}

    def update(**kwargs: Any) -> dict[str, Any]:
        mutated.update(kwargs)
        return {"relation_id": kwargs["relation_id"], "updated": True}

    monkeypatch.setattr(concept_routes, "_execute_governed_http_mutation", execute)
    monkeypatch.setattr(concept_routes, "update_text_relation_text", update)
    monkeypatch.setattr(
        concept_routes,
        "maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(concept_routes, "_get_request_namespace", lambda: None)

    response = _client().patch(
        "/api/concepts/%23V%23record/texts/relation-2",
        json={
            "text": "  Revised text  ",
            "lang": "en-NZ",
            "provenance": {"source": "forged-client-source"},
            "request_id": "text-effect-2",
        },
    )

    assert response.status_code == 200
    assert authorised == {
        "concept_id": "#V#record",
        "relation_id": "relation-2",
        "new_text": "Revised text",
        "language": "en-NZ",
        "provenance": {"source": "update_text_relation"},
        "request_id": "text-effect-2",
    }
    assert mutated == {
        "subject_concept_id": authorised["concept_id"],
        "relation_id": authorised["relation_id"],
        "new_text": authorised["new_text"],
        "lang": authorised["language"],
        "provenance": authorised["provenance"],
    }


def test_http_text_delete_by_predicate_and_text_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        concept_routes,
        "_execute_governed_http_mutation",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous deletion must not reach the mutation boundary")
        ),
    )

    response = _client().delete(
        "/api/concepts/%23V%23record/texts",
        json={
            "predicate": "hasNote",
            "text": "duplicate text",
            "lang": "en",
            "context": {"position": 1},
        },
    )

    assert response.status_code == 410
    payload = response.get_json()
    assert payload["error_code"] == "exact_text_relation_id_required"
    assert payload["effect_status"] == "not_started"
    assert payload["changed"] is False
    assert payload["recovery_affordances"][1]["action_type"] == (
        "delete_exact_text_relation"
    )


def test_http_relation_id_cannot_patch_an_ontology_role_relation(monkeypatch) -> None:
    mutation_called = False

    def relation_find_one(_query: dict[str, Any]) -> dict[str, Any]:
        return {
            "_id": "role-relation",
            "subject_concept_id": "#V#administrator",
            "predicate": "#V#has_ontology_authority_role",
            "object_text_id": "role-text",
            "context": {},
        }

    def update(**_kwargs: Any) -> dict[str, Any]:
        nonlocal mutation_called
        mutation_called = True
        return {"updated": True}

    monkeypatch.setattr(command, "can_access_concept", lambda _concept_id: True)
    monkeypatch.setattr(command.TextRelationsRepository, "find_one", relation_find_one)
    monkeypatch.setattr(
        command.TextValuesRepository,
        "find_one",
        lambda _query: {"_id": "role-text", "text": "global", "lang": "en"},
    )
    monkeypatch.setattr(concept_routes, "update_text_relation_text", update)

    response = _client().patch(
        "/api/concepts/%23V%23administrator/texts/role-relation",
        json={"text": "no longer an administrator", "lang": "en"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error_code"] == ("dedicated_ontology_governance_operation_required")
    assert payload["effect_status"] == "not_started"
    assert mutation_called is False


def test_text_postcondition_requires_exact_language_context_and_provenance() -> None:
    intent = authority.OntologyMutationIntent(
        operation="text.upsert",
        publication_context=authority.PublicationContext(
            authority.PublicationContextKind.ORGANISATION,
            "#V#organisation_a",
        ),
        target_concept_ids=("#V#record",),
        tool_name="upsert_text_relation",
        predicate="hasDescription",
        delta={
            "text_sha256": command._text_sha256("A description"),
            "language": "en-NZ",
            "context_sha256": command._canonical_json_sha256({"kind": "summary"}),
            "provenance_sha256": command._canonical_json_sha256({"source": "curated"}),
        },
    )
    exact_state = {
        "relation_present": True,
        "predicate": "hasDescription",
        "text_sha256": intent.delta["text_sha256"],
        "language": "en-NZ",
        "context_sha256": intent.delta["context_sha256"],
        "provenance_sha256": intent.delta["provenance_sha256"],
    }

    assert command._verify_method_postcondition(
        method_name="upsert_text_relation",
        intent=intent,
        result={"success": True},
        canonical_state=exact_state,
    )
    for changed_field in ("language", "context_sha256", "provenance_sha256"):
        mismatched = {**exact_state, changed_field: "not-the-authorised-value"}
        assert not command._verify_method_postcondition(
            method_name="upsert_text_relation",
            intent=intent,
            result={"success": True},
            canonical_state=mismatched,
        )


def test_text_readback_proves_exact_relation_metadata(monkeypatch) -> None:
    relation_id = ObjectId()
    text_value_id = ObjectId()
    relation_queries: list[dict[str, Any]] = []
    value_queries: list[dict[str, Any]] = []

    def relation_find_one(query: dict[str, Any]) -> dict[str, Any]:
        relation_queries.append(query)
        return {
            "_id": relation_id,
            "subject_concept_id": "#V#record",
            "predicate": "hasDescription",
            "object_text_id": str(text_value_id),
            "context": {"kind": "summary"},
        }

    def value_find_one(query: dict[str, Any]) -> dict[str, Any]:
        value_queries.append(query)
        return {
            "_id": text_value_id,
            "text": "A description",
            "lang": "en-NZ",
            "provenance": {"source": "curated"},
        }

    monkeypatch.setattr(command.TextRelationsRepository, "find_one", relation_find_one)
    monkeypatch.setattr(command.TextValuesRepository, "find_one", value_find_one)
    monkeypatch.setattr(
        command,
        "concept_publication_context",
        lambda _concept_id: authority.PublicationContext(
            authority.PublicationContextKind.ORGANISATION,
            "#V#organisation_a",
        ),
    )

    state = command.canonical_read_back_for_method(
        method_name="upsert_text_relation",
        arguments={
            "concept_id": "#V#record",
            "predicate": "hasDescription",
        },
        result={"relation_id": str(relation_id)},
    )

    assert relation_queries == [{"_id": relation_id, "subject_concept_id": "#V#record"}]
    assert value_queries == [{"_id": text_value_id}]
    assert state == {
        "concept_id": "#V#record",
        "predicate": "hasDescription",
        "relation_id": str(relation_id),
        "relation_present": True,
        "text_sha256": command._text_sha256("A description"),
        "language": "en-NZ",
        "context_sha256": command._canonical_json_sha256({"kind": "summary"}),
        "provenance_sha256": command._canonical_json_sha256({"source": "curated"}),
        "publication_context": {
            "kind": "organisation",
            "concept_id": "#V#organisation_a",
            "source": "resolved",
        },
    }
