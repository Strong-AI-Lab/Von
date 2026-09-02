"""Tests for Phase 2 session and organisation routes (JVNAUTOSCI-789).

These tests validate authentication requirements, namespace derivation, and
role resolution for the organisation switching endpoints exposed via the
`/von/api/session` routes.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest


@pytest.fixture
def app_client(monkeypatch):
    """Provide a Flask test client with side effects stubbed out.

    We stub the DB monitor and prompt concept health check to avoid external
    dependencies during these route tests.
    """

    # Stub Google auth dependencies pulled in by utils_flask -> auth_routes imports.
    import sys
    import types

    fake_flow_module = types.ModuleType("google_auth_oauthlib.flow")

    class _DummyFlow:
        def __init__(self, *args, **kwargs):
            self.credentials = types.SimpleNamespace(id_token="dummy-token")
            self.redirect_uri = kwargs.get("redirect_uri")
            self.client_config = {"web": {"redirect_uris": [self.redirect_uri]}}

        @classmethod
        def from_client_config(cls, *args, **kwargs):
            return cls(**kwargs)

        def authorization_url(self, *args, **kwargs):
            return "https://auth.example", "state-token"

        def fetch_token(self, *args, **kwargs):
            return None

    fake_flow_module.Flow = _DummyFlow  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module

    fake_id_token_module = types.ModuleType("google.oauth2.id_token")
    fake_id_token_module.verify_oauth2_token = lambda *args, **kwargs: {  # type: ignore[attr-defined]
        "sub": "dummy-user"
    }

    fake_credentials_module = types.ModuleType("google.oauth2.credentials")

    class _DummyCredentials:
        def __init__(self, id_token: str = "dummy-token"):
            self.id_token = id_token

    fake_credentials_module.Credentials = _DummyCredentials  # type: ignore[attr-defined]

    fake_service_account_module = types.ModuleType("google.oauth2.service_account")

    class _DummyServiceAccountCredentials:
        def __init__(self, *args, **kwargs):
            self.project_id = kwargs.get("project_id")

    fake_service_account_module.Credentials = _DummyServiceAccountCredentials  # type: ignore[attr-defined]

    fake_oauth2_package = types.ModuleType("google.oauth2")
    fake_oauth2_package.id_token = fake_id_token_module  # type: ignore[attr-defined]
    fake_oauth2_package.credentials = fake_credentials_module  # type: ignore[attr-defined]
    fake_oauth2_package.service_account = fake_service_account_module  # type: ignore[attr-defined]

    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module

    import src.backend.server.utils_flask as utils_flask

    # Avoid starting the DB monitor thread or touching Mongo during tests.
    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    # Keep prompt concept health check lightweight.
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )
    import src.backend.services.organisation_membership_service as memberships

    monkeypatch.setattr(
        memberships,
        "resolve_user_organisation_membership",
        lambda user_concept_id, organisation_concept_id: {
            "user_concept_id": user_concept_id,
            "organisation_concept_id": organisation_concept_id,
            "role": (
                "admin"
                if organisation_concept_id
                == "#V#university_of_auckland_strong_ai_lab"
                else "member"
            ),
        },
    )
    represented_memberships = {
        "#V#michael_witbrock": [
            {
                "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
                "role": "admin",
            }
        ],
        "#V#lu_yunli": [
            {
                "organisation_concept_id": "#V#the_lu_witbrock_household",
                "role": "member",
            }
        ],
    }
    monkeypatch.setattr(
        memberships,
        "get_user_memberships",
        lambda user_concept_id: {
            "user_concept_id": user_concept_id,
            "memberships": represented_memberships.get(user_concept_id, []),
            "total_memberships": len(
                represented_memberships.get(user_concept_id, [])
            ),
        },
    )

    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield app, client


def test_set_organisation_requires_authentication(app_client):
    _, client = app_client

    resp = client.post(
        "/von/api/session/set_organisation", json={"organisation_concept_id": "any_org"}
    )

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_set_organisation_updates_session_and_namespace(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "updated"
    assert data["role"] == "admin"  # From stub role resolver mapping
    assert (
        data["namespace"] == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )

    with client.session_transaction() as sess:
        assert sess["organisation_concept_id"] == "university_of_auckland_strong_ai_lab"
        assert sess["role_in_org"] == "admin"
        assert (
            sess["namespace"]
            == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
        )


def test_legacy_session_user_id_owns_durable_window_binding_canonically(
    monkeypatch,
    app_client,
):
    from src.backend.services import window_session_context_service as window_context

    _, client = app_client
    window_session_id = "legacy-session-owner-window"
    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    selected = client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
        headers={"X-Von-Window-Session": window_session_id},
    )
    assert selected.status_code == 200

    repository = window_context.get_window_session_binding_repository()
    binding = repository.load_owned(window_session_id, "#V#michael_witbrock")
    assert binding is not None
    assert binding.user_id == "#V#michael_witbrock"
    assert repository.load_owned(window_session_id, "michael_witbrock") is None

    monkeypatch.setattr(
        window_context,
        "_window_session_store",
        window_context.WindowSessionStore(binding_repository=repository),
    )
    recovered = client.get(
        "/von/api/session/context",
        headers={"X-Von-Window-Session": window_session_id},
    )

    assert recovered.status_code == 200
    assert recovered.get_json()["context_source"] == "window_session"
    assert recovered.get_json()["organisation_id"] == (
        "#V#university_of_auckland_strong_ai_lab"
    )


def test_set_organisation_rejects_non_member_without_changing_session(
    monkeypatch, app_client
):
    import src.backend.services.organisation_membership_service as memberships

    _, client = app_client
    monkeypatch.setattr(
        memberships,
        "resolve_user_organisation_membership",
        lambda _user_concept_id, _organisation_concept_id: None,
    )
    with client.session_transaction() as sess:
        sess["user_id"] = "outsider"
        sess["user_concept_id"] = "#V#outsider"

    resp = client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
    )

    assert resp.status_code == 403
    assert resp.get_json()["error_code"] == "organisation_membership_required"
    with client.session_transaction() as sess:
        assert "organisation_concept_id" not in sess
        assert "namespace" not in sess


def test_set_organisation_validates_body(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.post("/von/api/session/set_organisation", json={})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "organisation_concept_id required"


def test_set_organisation_reports_scope_coordination_contention_as_retryable(
    monkeypatch,
    app_client,
):
    from src.backend.services import (
        ontology_authority_membership_coordination_service as coordination,
    )
    from src.backend.services.ontology_publication_authority_service import (
        OntologyMutationResourceBusy,
    )

    @contextmanager
    def busy_scope(*_args, **_kwargs):
        raise OntologyMutationResourceBusy("ontology_mutation_resource_busy")
        yield  # pragma: no cover

    monkeypatch.setattr(
        coordination,
        "organisation_membership_scope_barrier",
        busy_scope,
    )
    _, client = app_client
    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    response = client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "error": "window_session_scope_coordination_busy",
        "error_code": "window_session_scope_coordination_busy",
        "retryable": True,
    }


def test_get_session_context_derives_namespace_without_org(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.get("/von/api/session/context")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["organisation_id"] is None
    assert data["role"] is None
    assert data["namespace"] == "#V#michael_witbrock"


def test_get_session_context_derives_role_and_namespace_with_org(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"
        sess["organisation_concept_id"] = "university_of_auckland_strong_ai_lab"

    resp = client.get("/von/api/session/context")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["role"] == "admin"  # Resolved via stub mapping
    assert (
        data["namespace"] == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )


def test_set_user_concept_updates_session_context_and_org_listing(
    app_client,
    monkeypatch,
):
    _, client = app_client
    import src.backend.services.settings_service as settings_service

    monkeypatch.setattr(
        settings_service,
        "_find_user_concept_by_email",
        lambda email: (
            {"concept_id": "#V#lu_yunli"}
            if email == "jeremyluyunli123@gmail.com"
            else None
        ),
    )

    with client.session_transaction() as sess:
        sess["user_email"] = "jeremyluyunli123@gmail.com"

    resp = client.post(
        "/von/api/session/set_user_concept", json={"user_concept_id": "#V#lu_yunli"}
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["user_id"] == "#V#lu_yunli"
    assert data["organisation_id"] is None
    assert data["role"] is None
    assert data["namespace"] == "#V#lu_yunli"

    ctx = client.get("/von/api/session/context")
    assert ctx.status_code == 200
    ctx_data = ctx.get_json()
    assert ctx_data["user_id"] == "#V#lu_yunli"
    assert ctx_data["namespace"] == "#V#lu_yunli"

    orgs = client.get("/von/api/organisations/my_organisations")
    assert orgs.status_code == 200
    orgs_data = orgs.get_json()
    assert orgs_data["total_count"] == 1
    assert orgs_data["organisations"][0]["concept_id"] == "#V#the_lu_witbrock_household"
    with client.session_transaction() as sess:
        assert sess["von_authentication_assurance"] == "hasVonLoginEmail.v1"


def test_set_user_concept_rejects_bare_legacy_header_without_seeding_session(
    app_client,
    monkeypatch,
):
    _, client = app_client

    import src.backend.security.access_control as access_control

    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda concept_id: concept_id,
    )

    resp = client.post(
        "/von/api/session/set_user_concept",
        json={"user_concept_id": "#V#michael_witbrock"},
        headers={"X-User-Concept-ID": "#V#michael_witbrock"},
    )

    assert resp.status_code == 401
    assert resp.get_json() == {"error": "Not authenticated"}

    with client.session_transaction() as sess:
        assert "user_concept_id" not in sess
        assert "user_id" not in sess

    # The rejected header cannot become a signed identity on a later request.
    context = client.get("/von/api/session/context")
    assert context.status_code == 200
    assert context.get_json()["authenticated"] is False


def test_set_user_concept_preserves_preexisting_session_identity_with_legacy_header(
    app_client,
):
    _, client = app_client
    real_user_concept_id = "#V#person_hugues_van_assel_b0cd25a1"

    with client.session_transaction() as sess:
        sess["user_id"] = real_user_concept_id

    resp = client.post(
        "/von/api/session/set_user_concept",
        json={"user_concept_id": real_user_concept_id},
        headers={"X-User-Concept-ID": real_user_concept_id},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["user_id"] == real_user_concept_id
    assert data["namespace"] == real_user_concept_id

    ctx = client.get(
        "/von/api/session/context",
        headers={"X-Von-Window-Session": "ws_real_header"},
    )
    assert ctx.status_code == 200
    ctx_data = ctx.get_json()
    assert ctx_data["authenticated"] is True
    assert ctx_data["user_id"] == real_user_concept_id


def test_set_user_concept_rejects_authenticated_actor_impersonation(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "user_a"
        sess["user_concept_id"] = "#V#user_a"
        sess["organisation_concept_id"] = "org_a"
        sess["role_in_org"] = "owner"
        sess["namespace"] = "#V#user_a@org_a"

    response = client.post(
        "/von/api/session/set_user_concept",
        json={"user_concept_id": "#V#user_b"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "authenticated_user_concept_mismatch",
        "error_code": "authenticated_user_concept_mismatch",
    }
    with client.session_transaction() as sess:
        assert sess["user_concept_id"] == "#V#user_a"
        assert sess["organisation_concept_id"] == "org_a"
        assert sess["role_in_org"] == "owner"
        assert sess["namespace"] == "#V#user_a@org_a"


def test_set_user_concept_preserves_window_scoped_org_namespace(app_client):
    _, client = app_client
    window_session_id = "ws_preserve_org_scope"

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"
        sess["user_concept_id"] = "#V#michael_witbrock"

    org_resp = client.post(
        "/von/api/session/set_organisation",
        json={"organisation_concept_id": "university_of_auckland_strong_ai_lab"},
        headers={"X-Von-Window-Session": window_session_id},
    )
    assert org_resp.status_code == 200

    resp = client.post(
        "/von/api/session/set_user_concept",
        json={"user_concept_id": "#V#michael_witbrock"},
        headers={"X-Von-Window-Session": window_session_id},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["organisation_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert data["role"] == "admin"
    assert (
        data["namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )

    ctx = client.get(
        "/von/api/session/context",
        headers={"X-Von-Window-Session": window_session_id},
    )
    assert ctx.status_code == 200
    ctx_data = ctx.get_json()
    assert ctx_data["organisation_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert (
        ctx_data["namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )

    with client.session_transaction() as sess:
        assert sess["organisation_concept_id"] == "university_of_auckland_strong_ai_lab"
        assert sess["role_in_org"] == "admin"


def test_get_my_organisations_requires_authentication(app_client):
    _, client = app_client

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "Not authenticated"


def test_get_my_organisations_returns_represented_memberships(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"
        sess["user_concept_id"] = "#V#michael_witbrock"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert org["role"] == "admin"
    assert org["name"] == "University Of Auckland Strong Ai Lab"


def test_get_my_organisations_uses_authenticated_user_not_email(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "opaque-oauth-subject"
        sess["user_email"] = "lu.yunli@example.com"
        sess["user_concept_id"] = "#V#michael_witbrock"
        sess["auth_provider"] = "google_oauth"
        sess["von_authentication_assurance"] = "hasVonLoginEmail.v1"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert org["role"] == "admin"
    assert org["name"] == "University Of Auckland Strong Ai Lab"


def test_get_my_organisations_ignores_selected_user_concept_id(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"
        sess["user_concept_id"] = "#V#michael_witbrock"

    resp = client.get(
        "/von/api/organisations/my_organisations?user_concept_id=%23V%23lu_yunli"
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert org["role"] == "admin"
    assert org["name"] == "University Of Auckland Strong Ai Lab"


def test_get_my_organisations_prefers_memberships_from_user_concept_relationships(
    app_client, monkeypatch
):
    _, client = app_client

    import src.backend.services.organisation_membership_service as memberships

    seen_user_ids = []

    def _fake_get_user_memberships(user_concept_id: str):
        seen_user_ids.append(user_concept_id)
        return {
            "user_concept_id": user_concept_id,
            "memberships": [
                {
                    "organisation_concept_id": "#V#the_lu_witbrock_household",
                    "role": "owner",
                }
            ],
            "total_memberships": 1,
        }

    monkeypatch.setattr(
        memberships,
        "get_user_memberships",
        _fake_get_user_memberships,
    )

    with client.session_transaction() as sess:
        sess["user_id"] = "lu_yunli"
        sess["user_concept_id"] = "#V#lu_yunli"
        sess["organisation_concept_id"] = "university_of_auckland_strong_ai_lab"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    assert seen_user_ids == ["#V#lu_yunli"]
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#the_lu_witbrock_household"
    assert org["role"] == "owner"
    assert org["name"] == "The Lu Witbrock Household"
