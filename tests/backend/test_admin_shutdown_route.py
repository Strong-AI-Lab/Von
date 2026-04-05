from __future__ import annotations

import sys
import threading
import time
import types

import pytest


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


@pytest.fixture
def app_client(monkeypatch):
    _install_google_oauth_stubs()

    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )
    monkeypatch.setenv("VON_ADMIN_TOKEN", "test-admin-token")

    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    app.config["TESTING"] = True

    with app.test_client() as client:
        yield utils_flask, app, client


def test_admin_shutdown_requires_matching_token(app_client):
    _, _, client = app_client

    resp = client.post("/admin/shutdown", headers={"X-Admin-Token": "wrong-token"})

    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_admin_shutdown_acknowledges_before_drain_finishes(app_client, monkeypatch):
    utils_flask, _, client = app_client
    drain_started = threading.Event()
    allow_drain_to_finish = threading.Event()
    drain_finished = threading.Event()
    server_shutdown_called = threading.Event()

    def _slow_stop() -> None:
        drain_started.set()
        allow_drain_to_finish.wait(timeout=2.0)
        drain_finished.set()

    def _fake_werkzeug_shutdown() -> None:
        server_shutdown_called.set()

    monkeypatch.setattr(utils_flask, "_stop_durable_workflow_system", _slow_stop)

    started = time.perf_counter()
    resp = client.post(
        "/admin/shutdown",
        headers={"X-Admin-Token": "test-admin-token"},
        environ_overrides={"werkzeug.server.shutdown": _fake_werkzeug_shutdown},
    )
    elapsed = time.perf_counter() - started

    assert resp.status_code == 202
    assert resp.get_json() == {"success": True, "status": "shutting_down"}
    assert elapsed < 0.2
    assert drain_started.wait(timeout=1.0)
    assert not drain_finished.is_set()
    assert not server_shutdown_called.is_set()

    allow_drain_to_finish.set()

    assert drain_finished.wait(timeout=1.0)
    assert server_shutdown_called.wait(timeout=1.0)
