from __future__ import annotations

import base64
import os
import types

import pytest


def _fernet_key() -> str:
    # Fernet keys are urlsafe base64-encoded 32-byte keys.
    return base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8")


@pytest.fixture
def app_client(monkeypatch):
    # Use mongomock so tests do not require a running Mongo.
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_ROOM_DEVICE_TOKEN_KEY", _fernet_key())
    monkeypatch.setenv("VON_ADMIN_TOKEN", "test-admin-token")

    # Stub Google auth deps pulled in by utils_flask -> auth_routes imports.
    import sys

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

    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
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


def test_admin_provision_requires_admin_token(app_client):
    _, client = app_client

    resp = client.post("/admin/room_devices/provision", json={})

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorised"


def test_device_login_and_whoami_happy_path(app_client):
    _, client = app_client

    provision = client.post(
        "/admin/room_devices/provision",
        headers={"X-Admin-Token": "test-admin-token"},
        json={
            "device_name": "Meeting Room 1",
            "organisation_concept_id": "#V#university_of_auckland_strong_ai_lab",
        },
    )

    assert provision.status_code == 201
    p = provision.get_json()
    assert "device_id" in p
    assert "device_secret" in p
    assert p["organisation_id"] == "university_of_auckland_strong_ai_lab"

    login = client.post(
        "/api/room_devices/login",
        json={"device_id": p["device_id"], "device_secret": p["device_secret"]},
    )
    assert login.status_code == 200
    token = login.get_json()["access_token"]

    whoami = client.get(
        "/api/room_devices/whoami", headers={"Authorization": f"Bearer {token}"}
    )
    assert whoami.status_code == 200
    data = whoami.get_json()
    assert data["device_id"] == p["device_id"]
    assert data["organisation_id"] == "university_of_auckland_strong_ai_lab"


def test_device_login_rejects_bad_secret(app_client):
    _, client = app_client

    provision = client.post(
        "/admin/room_devices/provision",
        headers={"X-Admin-Token": "test-admin-token"},
        json={},
    )
    p = provision.get_json()

    login = client.post(
        "/api/room_devices/login",
        json={"device_id": p["device_id"], "device_secret": "wrong"},
    )

    assert login.status_code == 401
    assert login.get_json()["error"] == "invalid_credentials"
