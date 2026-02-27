"""Regression coverage for non-blocking internal orchestrator startup.

JVNAUTOSCI-1304: Server startup must not block HTTP bind for minutes while the
internal MCP orchestrator constructs heavy workflow registries.
"""

from __future__ import annotations

import sys
import time
import types


def _install_google_oauth_stubs() -> None:
    """Provide lightweight google auth modules for route import side effects."""

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

    sys.modules["google_auth_oauthlib"] = types.ModuleType("google_auth_oauthlib")
    sys.modules["google_auth_oauthlib"].flow = fake_flow_module  # type: ignore[attr-defined]
    sys.modules["google_auth_oauthlib.flow"] = fake_flow_module
    sys.modules["google.oauth2"] = fake_oauth2_package
    sys.modules["google.oauth2.id_token"] = fake_id_token_module
    sys.modules["google.oauth2.credentials"] = fake_credentials_module
    sys.modules["google.oauth2.service_account"] = fake_service_account_module


def _install_internal_mcp_stubs(monkeypatch, internal_mcp_module, orchestrator_cls) -> None:
    """Install lightweight internal MCP implementations for startup tests."""

    class _StubCatalogue:
        def list_methods(self):
            return ["dummy.method"]

    class _StubGateway:
        def __init__(self, *, catalogue, transport, enabled):
            self.catalogue = catalogue
            self.transport = transport
            self.enabled = enabled

    class _StubTransport:
        pass

    monkeypatch.setattr(
        internal_mcp_module, "build_default_catalogue", lambda: _StubCatalogue()
    )
    monkeypatch.setattr(internal_mcp_module, "InternalMCPGateway", _StubGateway)
    monkeypatch.setattr(internal_mcp_module, "InternalMCPTransport", _StubTransport)
    monkeypatch.setattr(
        internal_mcp_module, "InternalMCPChatOrchestrator", orchestrator_cls
    )


def test_create_flask_app_defers_orchestrator_initialisation(monkeypatch):
    _install_google_oauth_stubs()

    import src.backend.integrations.internal_mcp as internal_mcp
    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.setenv("VON_INTERNAL_MCP_ORCHESTRATOR_BLOCKING_STARTUP", "0")
    monkeypatch.setenv("VON_PREWARM_DISABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "0")

    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    class _SlowOrchestrator:
        def __init__(self, *args, **kwargs):
            time.sleep(1.0)

    _install_internal_mcp_stubs(monkeypatch, internal_mcp, _SlowOrchestrator)

    started = time.perf_counter()
    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    elapsed = time.perf_counter() - started

    # If orchestration startup blocks request-serving startup, this takes >= 1s.
    assert elapsed < 0.8

    status = app.config.get("INTERNAL_MCP_ORCHESTRATOR_STATUS")
    assert isinstance(status, dict)
    assert status.get("state") in {"pending", "initialising", "ready"}

    deadline = time.time() + 4.0
    while time.time() < deadline and app.config.get("INTERNAL_MCP_ORCHESTRATOR") is None:
        time.sleep(0.02)

    assert app.config.get("INTERNAL_MCP_ORCHESTRATOR") is not None
    final_status = app.config.get("INTERNAL_MCP_ORCHESTRATOR_STATUS") or {}
    assert final_status.get("state") == "ready"
    assert final_status.get("ready") is True


def test_create_flask_app_defers_durable_workflow_startup(monkeypatch):
    _install_google_oauth_stubs()

    import src.backend.integrations.internal_mcp as internal_mcp
    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "0")
    monkeypatch.setenv("VON_PREWARM_DISABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP", "0")

    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    class _FastOrchestrator:
        def __init__(self, *args, **kwargs):
            return None

    _install_internal_mcp_stubs(monkeypatch, internal_mcp, _FastOrchestrator)

    def _slow_durable_startup(_app_logger):
        time.sleep(1.0)
        return {"worker_running": True, "scheduler_running": True}

    monkeypatch.setattr(
        utils_flask, "_start_durable_workflow_system", _slow_durable_startup
    )
    monkeypatch.setattr(utils_flask, "_stop_durable_workflow_system", lambda: None)

    started = time.perf_counter()
    app = utils_flask.create_flask_app(
        list_models_func=lambda: ["dummy-model"],
        generate_func=lambda prompt, context, model: "ok",
    )
    elapsed = time.perf_counter() - started

    # If durable workflow startup blocks request-serving startup, this takes >= 1s.
    assert elapsed < 0.8

    status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS")
    assert isinstance(status, dict)
    assert status.get("state") in {"pending", "initialising", "ready"}

    deadline = time.time() + 4.0
    while (
        time.time() < deadline and app.config.get("DURABLE_WORKFLOW_COMPONENTS") is None
    ):
        time.sleep(0.02)

    assert app.config.get("DURABLE_WORKFLOW_COMPONENTS") is not None
    final_status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
    assert final_status.get("state") == "ready"
    assert final_status.get("ready") is True
