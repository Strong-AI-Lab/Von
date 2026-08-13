from __future__ import annotations

import pytest
from flask import Flask

from src.backend.security import access_control
from src.backend.server.routes import ontology_authority_routes as routes
from src.backend.services.ontology_publication_authority_service import (
    OntologyMutationIntent,
    PublicationContext,
)


def _app() -> Flask:
    app = Flask(__name__)
    app.secret_key = "ontology-authority-route-tests"
    app.register_blueprint(routes.ontology_authority_bp)
    return app


def _authenticated_client(actor_concept_id: str = "#V#server"):
    client = _app().test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = actor_concept_id
    return client


def _accept_legacy_identity_header_for_regression(monkeypatch) -> None:
    """Make the legacy resolver accept the spoof so route isolation is tested."""

    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda actor_concept_id: actor_concept_id,
    )


def test_authority_summary_uses_server_actor_and_states_operator_separation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes,
        "list_actor_semantic_authority",
        lambda actor: {
            "actor_concept_id": actor,
            "roles": [],
            "von_administrator_semantics": {
                "implies_semantic_ontology_authority": False
            },
        },
    )

    response = _authenticated_client().get("/api/ontology-authority/me")

    assert response.status_code == 200
    assert response.get_json()["actor_concept_id"] == "#V#server"
    assert (
        response.get_json()["von_administrator_semantics"][
            "implies_semantic_ontology_authority"
        ]
        is False
    )


def test_delegation_ignores_payload_grantor_and_requires_exact_effect(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        routes, "get_effective_organisation_concept_id", lambda: "#V#org"
    )
    intent = OntologyMutationIntent(
        operation="text.upsert",
        publication_context=PublicationContext.organisation("#V#org"),
        target_concept_ids=("#V#concept",),
        tool_name="upsert_text_relation",
    )
    monkeypatch.setattr(routes, "build_ontology_mutation_intent", lambda **_: intent)
    captured = {}

    def issue(**kwargs):
        captured.update(kwargs)
        return {"delegation_id": "oag-1", "status": "active"}

    monkeypatch.setattr(routes, "issue_agent_delegation", issue)
    response = (
        _authenticated_client()
        .post(
            "/api/ontology-authority/delegations",
            json={
                "method_name": "upsert_text_relation",
                "arguments": {"concept_id": "#V#concept"},
                "grantor_actor_concept_id": "#V#attacker",
                "delegate_concept_id": "#V#agent",
                "audience": "adaptive_turn",
                "effect_id": "effect-1",
            },
        )
    )

    assert response.status_code == 201
    assert captured["grantor_actor_concept_id"] == "#V#server"
    assert captured["effect_id"] == "effect-1"
    assert captured["tool_name"] == "upsert_text_relation"


@pytest.mark.parametrize(
    "header_name", ["X-User-Concept-ID", "X-User-Client-ID"]
)
def test_delegation_rejects_spoofed_legacy_identity_headers(
    monkeypatch,
    header_name: str,
) -> None:
    _accept_legacy_identity_header_for_regression(monkeypatch)
    intent = OntologyMutationIntent(
        operation="text.upsert",
        publication_context=PublicationContext.global_context(),
        target_concept_ids=("#V#concept",),
        tool_name="upsert_text_relation",
    )
    monkeypatch.setattr(routes, "build_ontology_mutation_intent", lambda **_: intent)
    issued = []
    monkeypatch.setattr(
        routes,
        "issue_agent_delegation",
        lambda **kwargs: issued.append(kwargs) or {"delegation_id": "spoofed"},
    )

    response = _app().test_client().post(
        "/api/ontology-authority/delegations",
        headers={header_name: "#V#spoofed_semantic_admin"},
        json={
            "method_name": "upsert_text_relation",
            "arguments": {"concept_id": "#V#concept"},
            "delegate_concept_id": "#V#agent",
            "audience": "adaptive_turn",
            "effect_id": "effect-spoofed",
        },
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "authenticated_actor_context_required"
    }
    assert issued == []


def test_bootstrap_needs_explicit_opt_in_and_token(monkeypatch) -> None:
    monkeypatch.delenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP", raising=False)
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-secret")
    client = _app().test_client()
    denied = client.post(
        "/api/ontology-authority/bootstrap",
        json={
            "subject_concept_id": "#V#first",
            "role": "global_ontology_administrator",
        },
        headers={"X-Von-Admin-Token": "operator-secret"},
    )
    assert denied.status_code == 403

    monkeypatch.setenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP", "1")
    captured = {}
    monkeypatch.setattr(
        routes,
        "bootstrap_first_semantic_authority",
        lambda **kwargs: captured.update(kwargs) or {"first_grant": kwargs},
    )
    granted = client.post(
        "/api/ontology-authority/bootstrap",
        json={
            "subject_concept_id": "#V#first",
            "role": "global_ontology_administrator",
        },
        headers={"X-Von-Admin-Token": "operator-secret"},
    )
    assert granted.status_code == 201
    assert captured["subject_concept_id"] == "#V#first"


def test_receipt_read_does_not_disclose_another_actors_receipt(monkeypatch) -> None:
    monkeypatch.setattr(routes, "get_mutation_receipt_for_actor", lambda **_: None)

    response = _authenticated_client().get(
        "/api/ontology-authority/receipts/secret"
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "ontology_mutation_receipt_not_found"}


def test_role_route_uses_only_server_identity_not_forged_payload_identity(
    monkeypatch,
) -> None:
    captured = {}

    def grant(**kwargs):
        captured.update(kwargs)
        raise PermissionError("organisation_ontology_admin_authority_required")

    monkeypatch.setattr(routes, "grant_ontology_authority_role", grant)
    response = (
        _authenticated_client("#V#member")
        .post(
            "/api/ontology-authority/roles",
            json={
                "subject_concept_id": "#V#target",
                "role": "organisation_ontology_administrator",
                "organisation_concept_id": "#V#org_a",
                "request_id": "forged-identity",
                "actor_concept_id": "#V#global_admin",
                "grantor_actor_concept_id": "#V#global_admin",
                "organisation_id": "#V#global_org",
            },
        )
    )

    assert response.status_code == 403
    assert (
        response.get_json()["error"] == "organisation_ontology_admin_authority_required"
    )
    assert captured == {
        "subject_concept_id": "#V#target",
        "role": "organisation_ontology_administrator",
        "organisation_concept_id": "#V#org_a",
        "request_id": "forged-identity",
        "reason": None,
    }


@pytest.mark.parametrize(
    ("path", "service_name"),
    [
        ("/api/ontology-authority/roles", "grant_ontology_authority_role"),
        (
            "/api/ontology-authority/roles/revoke",
            "revoke_ontology_authority_role",
        ),
    ],
)
@pytest.mark.parametrize(
    "header_name", ["X-User-Concept-ID", "X-User-Client-ID"]
)
def test_role_writes_reject_spoofed_legacy_identity_headers(
    monkeypatch,
    path: str,
    service_name: str,
    header_name: str,
) -> None:
    _accept_legacy_identity_header_for_regression(monkeypatch)
    calls = []
    monkeypatch.setattr(
        routes,
        service_name,
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )

    response = _app().test_client().post(
        path,
        headers={header_name: "#V#spoofed_semantic_admin"},
        json={
            "subject_concept_id": "#V#target",
            "role": "global_ontology_administrator",
            "request_id": "spoofed-role-write",
        },
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "authenticated_actor_context_required"
    }
    assert calls == []


@pytest.mark.parametrize(
    ("path", "service_name", "expected_status"),
    [
        (
            "/api/ontology-authority/roles",
            "grant_ontology_authority_role",
            201,
        ),
        (
            "/api/ontology-authority/roles/revoke",
            "revoke_ontology_authority_role",
            200,
        ),
    ],
)
def test_role_writes_accept_authenticated_session(
    monkeypatch,
    path: str,
    service_name: str,
    expected_status: int,
) -> None:
    calls = []
    monkeypatch.setattr(
        routes,
        service_name,
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )

    response = _authenticated_client("#V#semantic_admin").post(
        path,
        json={
            "subject_concept_id": "#V#target",
            "role": "global_ontology_administrator",
            "request_id": "authenticated-role-write",
        },
    )

    assert response.status_code == expected_status
    assert calls == [
        {
            "subject_concept_id": "#V#target",
            "role": "global_ontology_administrator",
            "organisation_concept_id": None,
            "request_id": "authenticated-role-write",
            "reason": None,
        }
    ]


def test_scope_read_returns_not_found_without_disclosing_inaccessible_concept(
    monkeypatch,
) -> None:
    monkeypatch.setattr(routes, "can_access_concept", lambda _concept_id: False)
    monkeypatch.setattr(
        routes,
        "scope_read_back",
        lambda _concept_id: AssertionError("must not read inaccessible scope"),
    )

    response = (
        _authenticated_client("#V#member")
        .get("/api/ontology-authority/concepts/%23V%23historical-secret/scope")
    )

    assert response.status_code == 404
    assert response.get_json() == {"error": "ontology_scope_target_not_found"}


def test_scope_preview_and_execute_are_distinct_effects(monkeypatch) -> None:
    calls = []

    def change(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "authority_receipt": {"receipt_id": f"receipt-{kwargs['request_id']}"},
            "canonical_read_back": {"scope_fingerprint": "after"},
        }

    monkeypatch.setattr(routes, "change_concept_publication_scope", change)
    client = _authenticated_client("#V#admin")
    preview = client.post(
        "/api/ontology-authority/concepts/%23V%23legacy/scope",
        json={
            "destination_kind": "organisation",
            "destination_concept_id": "#V#org_a",
            "expected_scope_fingerprint": "before",
            "request_id": "scope-preview",
            "preview": True,
        },
    )
    execute = client.post(
        "/api/ontology-authority/concepts/%23V%23legacy/scope",
        json={
            "destination_kind": "organisation",
            "destination_concept_id": "#V#org_a",
            "expected_scope_fingerprint": "before",
            "request_id": "scope-execute",
            "preview": False,
        },
    )

    assert preview.status_code == 200
    assert execute.status_code == 200
    assert [call["request_id"] for call in calls] == ["scope-preview", "scope-execute"]
    assert [call["preview"] for call in calls] == [True, False]
    assert (
        preview.get_json()["authority_receipt"]
        != execute.get_json()["authority_receipt"]
    )


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize(
    "header_name", ["X-User-Concept-ID", "X-User-Client-ID"]
)
def test_scope_changes_reject_spoofed_legacy_identity_headers(
    monkeypatch,
    header_name: str,
    preview: bool,
) -> None:
    _accept_legacy_identity_header_for_regression(monkeypatch)
    calls = []
    monkeypatch.setattr(
        routes,
        "change_concept_publication_scope",
        lambda **kwargs: calls.append(kwargs) or {"success": True},
    )

    response = _app().test_client().post(
        "/api/ontology-authority/concepts/%23V%23legacy/scope",
        headers={header_name: "#V#spoofed_semantic_admin"},
        json={
            "destination_kind": "global",
            "expected_scope_fingerprint": "before",
            "request_id": f"spoofed-scope-{preview}",
            "preview": preview,
        },
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "authenticated_actor_context_required"
    }
    assert calls == []


def test_scope_route_drops_forged_legacy_authority_payload(monkeypatch) -> None:
    captured = {}

    def change(**kwargs):
        captured.update(kwargs)
        return {
            "success": False,
            "error_code": "global_ontology_admin_authority_required",
        }

    monkeypatch.setattr(routes, "change_concept_publication_scope", change)
    response = (
        _authenticated_client("#V#member")
        .post(
            "/api/ontology-authority/concepts/%23V%23legacy/scope",
            json={
                "destination_kind": "global",
                "expected_scope_fingerprint": "before",
                "request_id": "forged-legacy-scope",
                "preview": False,
                "actor_concept_id": "#V#global_admin",
                "organisation_concept_id": "#V#global_org",
                "legacy_scope_override": True,
            },
        )
    )

    assert response.status_code == 409
    assert (
        response.get_json()["error_code"] == "global_ontology_admin_authority_required"
    )
    assert captured == {
        "concept_id": "#V#legacy",
        "destination_kind": "global",
        "destination_concept_id": None,
        "expected_scope_fingerprint": "before",
        "request_id": "forged-legacy-scope",
        "preview": False,
        "reason": None,
    }


def test_role_migration_dry_run_requires_explicit_operator_bootstrap_token(
    monkeypatch,
) -> None:
    monkeypatch.delenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP", raising=False)
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-secret")
    monkeypatch.setattr(
        routes,
        "plan_ontology_authority_role_migration",
        lambda: {"mode": "dry_run", "ready_to_apply": True},
    )

    response = _app().test_client().get(
        "/api/ontology-authority/bootstrap/role-migration",
        headers={"X-Von-Admin-Token": "operator-secret"},
    )

    assert response.status_code == 403


def test_role_migration_apply_is_fixed_to_michael_and_inventory_fingerprint(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP", "true")
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-secret")
    captured = {}
    invalidated: list[str] = []

    def apply(**kwargs):
        captured.update(kwargs)
        return {
            "mode": "apply",
            "success": True,
            "target_concept_id": routes.ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        }

    monkeypatch.setattr(routes, "apply_ontology_authority_role_migration", apply)
    monkeypatch.setattr(
        routes,
        "delete_all_window_contexts_owned_by",
        lambda user_id: invalidated.append(user_id) or 2,
    )
    client = _app().test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = (
            routes.ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
        )
        flask_session["role_in_org"] = "admin"
    response = client.post(
        "/api/ontology-authority/bootstrap/role-migration",
        json={
            "mode": "apply",
            "expected_inventory_fingerprint": "dry-run-fingerprint",
            "target_concept_id": "#V#attacker_selected_target",
            "actor_concept_id": "#V#attacker",
        },
        headers={"X-Von-Admin-Token": "operator-secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["target_concept_id"] == (
        routes.ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET
    )
    assert captured == {
        "expected_inventory_fingerprint": "dry-run-fingerprint"
    }
    assert invalidated == [routes.ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET]
    assert response.get_json()["derived_role_cache_invalidation"] == {
        "flask_session_role_cleared": True,
        "window_contexts_deleted": 2,
    }
    with client.session_transaction() as flask_session:
        assert "role_in_org" not in flask_session


def test_role_migration_apply_rejects_non_target_actor_before_service(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP", "true")
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-secret")
    monkeypatch.setattr(
        routes,
        "apply_ontology_authority_role_migration",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("wrong actor must not reach migration service")
        ),
    )

    client = _app().test_client()
    with client.session_transaction() as flask_session:
        flask_session["user_concept_id"] = "#V#other_person"
    response = client.post(
        "/api/ontology-authority/bootstrap/role-migration",
        json={"mode": "apply", "expected_inventory_fingerprint": "current"},
        headers={"X-Von-Admin-Token": "operator-secret"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == (
        "ontology_authority_role_migration_target_actor_required"
    )


def test_role_migration_apply_does_not_accept_legacy_identity_header(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_ALLOW_ONTOLOGY_AUTHORITY_BOOTSTRAP", "true")
    monkeypatch.setenv("VON_ADMIN_TOKEN", "operator-secret")
    monkeypatch.setattr(
        routes,
        "apply_ontology_authority_role_migration",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity header must not authorise migration")
        ),
    )

    response = _app().test_client().post(
        "/api/ontology-authority/bootstrap/role-migration",
        json={"mode": "apply", "expected_inventory_fingerprint": "current"},
        headers={
            "X-Von-Admin-Token": "operator-secret",
            "X-User-Concept-ID": routes.ONTOLOGY_AUTHORITY_ROLE_MIGRATION_TARGET,
        },
    )

    assert response.status_code == 403
