"""Tests for Phase 2 session and organisation routes (JVNAUTOSCI-789).

These tests validate authentication requirements, namespace derivation, and
role resolution for the organisation switching endpoints exposed via the
`/von/api/session` routes.
"""

from __future__ import annotations

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


def test_set_organisation_validates_body(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.post("/von/api/session/set_organisation", json={})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "organisation_concept_id required"


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


def test_set_user_concept_updates_session_context_and_org_listing(app_client):
    _, client = app_client

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


def test_set_user_concept_accepts_header_authenticated_identity(app_client, monkeypatch):
    _, client = app_client

    import src.backend.security.access_control as access_control

    monkeypatch.setattr(
        access_control,
        "get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )

    resp = client.post(
        "/von/api/session/set_user_concept",
        json={"user_concept_id": "#V#michael_witbrock"},
        headers={"X-User-Concept-ID": "#V#michael_witbrock"},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["user_id"] == "#V#michael_witbrock"
    assert data["namespace"] == "#V#michael_witbrock"

    with client.session_transaction() as sess:
        assert sess["user_concept_id"] == "#V#michael_witbrock"
        assert sess["user_id"] == "#V#michael_witbrock"


def test_set_user_concept_accepts_real_header_authenticated_identity(app_client):
    _, client = app_client
    real_user_concept_id = "#V#person_hugues_van_assel_b0cd25a1"

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


def test_get_my_organisations_returns_stubbed_memberships(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "michael_witbrock"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#university_of_auckland_strong_ai_lab"
    assert org["role"] == "admin"
    assert org["name"] == "University Of Auckland Strong Ai Lab"


def test_get_my_organisations_uses_user_email_for_stub_memberships(app_client):
    _, client = app_client

    with client.session_transaction() as sess:
        sess["user_id"] = "opaque-oauth-subject"
        sess["user_email"] = "lu.yunli@example.com"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#the_lu_witbrock_household"
    assert org["role"] == "member"
    assert org["name"] == "The Lu Witbrock Household"


def test_get_my_organisations_allows_selected_user_concept_id_override(app_client):
    _, client = app_client

    # Authenticated session identity does not match stub mapping.
    with client.session_transaction() as sess:
        sess["user_id"] = "jeremyluyunli123@gmail.com"

    resp = client.get(
        "/von/api/organisations/my_organisations?user_concept_id=%23V%23lu_yunli"
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#the_lu_witbrock_household"
    assert org["role"] == "member"
    assert org["name"] == "The Lu Witbrock Household"


def test_get_my_organisations_prefers_memberships_from_user_concept_relationships(
    app_client, monkeypatch
):
    _, client = app_client

    # Stub concept lookup via the normal concept service path.
    import src.backend.services.concept_service as concept_service

    def _fake_get_concept_by_concept_id(concept_id: str, **_kwargs):
        if concept_id == "#V#lu_yunli":
            return {
                "concept_id": "#V#lu_yunli",
                "relationships": {
                    "#V#member_of_organisation": ["#V#the_lu_witbrock_household"]
                },
            }
        return None

    monkeypatch.setattr(
        concept_service, "get_concept_by_concept_id", _fake_get_concept_by_concept_id
    )

    with client.session_transaction() as sess:
        sess["user_id"] = "lu_yunli"
        sess["user_concept_id"] = "#V#lu_yunli"

    resp = client.get("/von/api/organisations/my_organisations")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    org = data["organisations"][0]
    assert org["concept_id"] == "#V#the_lu_witbrock_household"
    assert org["role"] == "member"
    assert org["name"] == "The Lu Witbrock Household"
