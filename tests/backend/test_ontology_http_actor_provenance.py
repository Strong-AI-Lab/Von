from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask

from src.backend.security import access_control
from src.backend.server.routes import concept_routes, vontology_routes
from src.backend.services import ontology_mutation_command_service as command
from src.backend.services import ontology_publication_authority_service as authority


def _client():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(concept_routes.concept_bp, url_prefix="/api/concepts")
    app.register_blueprint(vontology_routes.vontology_bp, url_prefix="/api/vontology")
    return app.test_client()


def _install_global_admin_authority_probe(monkeypatch) -> list[str]:
    """Keep route parsing real while replacing only storage-bound effect work."""

    calls: list[str] = []
    role = authority.AuthorityRoleEvidence(
        role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#existing_global_admin",
        organisation_concept_id=None,
        relation_id="role-existing-global-admin",
        revision="role-revision-1",
    )
    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda concept_id: concept_id,
    )
    monkeypatch.setattr(
        authority,
        "resolve_live_semantic_roles",
        lambda actor_id: (role,) if actor_id == "#V#existing_global_admin" else (),
    )
    monkeypatch.setattr(authority, "_gateway_actor_trust_source", lambda: None)
    monkeypatch.setattr(
        command,
        "resolve_governed_ontology_arguments",
        lambda _method_name, arguments: dict(arguments),
    )

    def execute(
        *,
        method_name: str,
        arguments: dict[str, Any],
        mutate: Callable[[], Any],
        preview: bool = False,
    ) -> dict[str, Any]:
        del arguments, mutate, preview
        calls.append(method_name)
        intent = authority.OntologyMutationIntent(
            operation={
                "add_relationship": "relationship.add",
                "upsert_text_relation": "text.upsert",
                "create_concepts": "concept.create",
            }[method_name],
            publication_context=authority.PublicationContext.global_context(),
            target_concept_ids=("#V#governed_target",),
            tool_name=method_name,
            predicate=(
                "#V#is_a_type_of" if method_name == "add_relationship" else None
            ),
        )
        decision = authority.authorise_ontology_mutation(intent)
        if not decision.allowed:
            return authority.ontology_authority_denial_payload(decision, intent)
        return {
            "success": True,
            "changed": True,
            "relation_created": method_name == "upsert_text_relation",
            "authority_decision": decision.public_projection(),
        }

    monkeypatch.setattr(command, "execute_governed_ontology_method", execute)
    monkeypatch.setattr(
        concept_routes,
        "resolve_text_relation_predicate_for_write",
        lambda predicate: SimpleNamespace(storage_predicate=predicate),
    )
    monkeypatch.setattr(
        concept_routes,
        "maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(concept_routes, "_get_request_namespace", lambda: None)
    return calls


def _relationship_request(client, *, headers: dict[str, str]):
    return client.post(
        "/api/vontology/relationships/add",
        headers=headers,
        json={
            "source_id": "#V#graduate_student",
            "kind": "#V#is_a_type_of",
            "target_id": "#V#university_student",
        },
    )


def _text_request(client, *, headers: dict[str, str]):
    return client.post(
        "/api/concepts/%23V%23governed_target/texts",
        headers=headers,
        json={"predicate": "hasNote", "text": "Governed note"},
    )


def _create_request(client, *, headers: dict[str, str]):
    return client.post(
        "/api/concepts/",
        headers=headers,
        json={
            "name": "Governed HTTP creation probe",
            "concept_id": "#V#governed_http_creation_probe",
            "kind": "type",
            "scope_mode": "global_general",
        },
    )


_GOVERNED_HTTP_REQUESTS = (
    ("add_relationship", _relationship_request),
    ("upsert_text_relation", _text_request),
    ("create_concepts", _create_request),
)


@pytest.mark.parametrize("header_name", ["X-User-Concept-ID", "X-User-Client-ID"])
@pytest.mark.parametrize(("method_name", "request_effect"), _GOVERNED_HTTP_REQUESTS)
def test_governed_http_mutations_reject_spoofed_legacy_global_admin(
    monkeypatch,
    header_name: str,
    method_name: str,
    request_effect: Callable[..., Any],
) -> None:
    calls = _install_global_admin_authority_probe(monkeypatch)

    response = request_effect(
        _client(),
        headers={header_name: "#V#existing_global_admin"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error_code"] == "client_supplied_identity_is_not_authority"
    assert payload["effect_status"] == "not_started"
    assert payload["changed"] is False
    assert payload["authority_decision"]["actor_concept_id"] is None
    assert calls == [method_name]


@pytest.mark.parametrize(("method_name", "request_effect"), _GOVERNED_HTTP_REQUESTS)
def test_governed_http_mutations_accept_authenticated_flask_session(
    monkeypatch,
    method_name: str,
    request_effect: Callable[..., Any],
) -> None:
    calls = _install_global_admin_authority_probe(monkeypatch)
    client = _client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#existing_global_admin"

    response = request_effect(client, headers={})

    assert response.status_code in {200, 201}
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["authority_decision"]["allowed"] is True
    assert payload["authority_decision"]["actor_concept_id"] == (
        "#V#existing_global_admin"
    )
    assert calls == [method_name]
