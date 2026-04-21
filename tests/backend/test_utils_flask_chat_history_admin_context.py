from __future__ import annotations

import sys
import types

from flask import Flask


def _install_google_oauth_stubs() -> None:
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


def test_resolve_chat_history_admin_context_derives_namespace():
    _install_google_oauth_stubs()

    import src.backend.server.utils_flask as utils_flask

    app = Flask(__name__)
    session_payload = {
        "user_concept_id": "#V#michael_witbrock",
        "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
        "role_in_org": "member",
    }

    with app.app_context():
        context, error = utils_flask._resolve_chat_history_admin_context(
            session_payload,
            enforce_namespace_match=False,
        )

    assert error is None
    assert context is not None
    assert context["user_concept_id"] == "#V#michael_witbrock"
    assert (
        context["target_namespace"]
        == "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
    )
    assert context["organisation_concept_id"] == "university_of_auckland_strong_ai_lab"
    assert context["role_in_org"] == "member"


def test_resolve_chat_history_admin_context_rejects_namespace_mismatch():
    _install_google_oauth_stubs()

    import src.backend.server.utils_flask as utils_flask

    app = Flask(__name__)
    session_payload = {
        "user_concept_id": "#V#michael_witbrock",
        "namespace": "#V#michael_witbrock@university_of_auckland_strong_ai_lab",
    }

    with app.app_context():
        context, error = utils_flask._resolve_chat_history_admin_context(
            session_payload,
            requested_namespace="#V#michael_witbrock@some_other_org",
            enforce_namespace_match=True,
        )

    assert context is None
    assert error is not None
    response, status_code = error
    assert status_code == 403
    assert response.get_json()["error"] == "namespace_mismatch"

