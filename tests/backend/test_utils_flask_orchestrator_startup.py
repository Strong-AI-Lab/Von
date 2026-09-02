"""Regression coverage for retired controller and deferred workflow startup."""

from __future__ import annotations

import sys
import threading
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


def _install_internal_mcp_gateway_stubs(monkeypatch, internal_mcp_module) -> None:
    """Install lightweight internal MCP gateway implementations."""

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


def test_retired_orchestrator_startup_does_not_build_or_spawn(monkeypatch):
    _install_google_oauth_stubs()

    import src.backend.server.utils_flask as utils_flask

    def _forbidden_thread(*_args, **_kwargs):
        raise AssertionError("retired controller must not start a thread")

    monkeypatch.setattr(utils_flask.threading, "Thread", _forbidden_thread)
    monkeypatch.setenv("VON_INTERNAL_MCP_ORCHESTRATOR_BLOCKING_STARTUP", "1")
    app = types.SimpleNamespace(config={})

    utils_flask._configure_internal_mcp_orchestrator_startup(
        app,
        gateway_instance=object(),
    )

    assert app.config["INTERNAL_MCP_ORCHESTRATOR"] is None
    status = app.config["INTERNAL_MCP_ORCHESTRATOR_STATUS"]
    assert status["state"] == "retired"
    assert status["ready"] is False
    assert status["replacement"] == "direct_adaptive_turn"
    assert isinstance(status["started_at"], str)


def test_optional_prewarm_keeps_large_vontology_tree_demand_loaded(monkeypatch):
    import src.backend.server.utils_flask as utils_flask

    listed_models: list[bool] = []
    log_messages: list[str] = []

    class _Logger:
        def info(self, message, *_args):
            log_messages.append(message)

        def warning(self, *_args, **_kwargs):
            return None

    class _SynchronousThread:
        def __init__(self, *, target, name=None, daemon=None):
            self._target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self._target()

    class _App:
        config = {"LIST_MODELS_FUNC": lambda: listed_models.append(True) or ["model"]}
        logger = _Logger()

        def test_client(self):
            raise AssertionError("tree endpoint must not be called by default")

    monkeypatch.delenv("VON_PREWARM_DISABLE", raising=False)
    monkeypatch.delenv("VON_PREWARM_TREE_ENABLE", raising=False)
    monkeypatch.setattr(utils_flask.threading, "Thread", _SynchronousThread)

    utils_flask._start_prewarm(_App())

    assert listed_models == [True]
    assert any("demand-loaded" in message for message in log_messages)


def test_create_flask_app_defers_durable_workflow_startup(monkeypatch):
    _install_google_oauth_stubs()

    import src.backend.integrations.internal_mcp as internal_mcp
    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "0")
    monkeypatch.setenv("VON_PREWARM_DISABLE", "1")
    monkeypatch.setenv("VON_CONCEPT_SUMMARY_FIELD_BOOTSTRAP_ENABLE", "0")
    monkeypatch.setenv("VON_PUBLICATION_SCOPE_PROFILE_BOOTSTRAP_ENABLE", "0")
    monkeypatch.setenv(
        "VON_CONSTITUTIVE_RELATION_REQUIREMENT_BOOTSTRAP_ENABLE", "0"
    )
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP", "0")

    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "ensure_monitor_started", lambda: None)
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: {"available": True, "source_field": "stub"},
    )

    _install_internal_mcp_gateway_stubs(monkeypatch, internal_mcp)

    maintenance_finished = threading.Event()

    def _slow_durable_startup(_app_logger, *, queue_ready_callback=None):
        components = {"worker_running": True, "scheduler_running": True}
        assert queue_ready_callback is not None
        queue_ready_callback(components)
        time.sleep(1.0)
        maintenance_finished.set()
        return components

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
    core_ready_status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
    assert core_ready_status.get("state") == "ready"
    assert core_ready_status.get("ready") is True
    assert core_ready_status.get("startup_phase") == "canonical_maintenance"
    assert core_ready_status.get("maintenance_in_progress") is True
    assert maintenance_finished.wait(3.0)

    final_status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
    assert final_status.get("state") == "ready"
    assert final_status.get("ready") is True
    assert final_status.get("startup_phase") == "complete"
    assert final_status.get("maintenance_in_progress") is False


def test_durable_workflow_startup_keeps_core_ready_when_maintenance_fails(
    monkeypatch,
):
    from flask import Flask

    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP", "0")
    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(utils_flask, "_stop_durable_workflow_system", lambda: None)

    def _maintenance_failure(_app_logger, *, queue_ready_callback=None):
        assert queue_ready_callback is not None
        queue_ready_callback(
            {"worker_running": True, "scheduler_running": True}
        )
        return None

    monkeypatch.setattr(
        utils_flask,
        "_start_durable_workflow_system",
        _maintenance_failure,
    )

    app = Flask(__name__)
    utils_flask._configure_durable_workflow_startup(app)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
        if status.get("startup_phase") == "canonical_maintenance_failed":
            break
        time.sleep(0.01)

    status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
    assert status.get("state") == "ready"
    assert status.get("ready") is True
    assert status.get("startup_phase") == "canonical_maintenance_failed"
    assert status.get("maintenance_in_progress") is False
    assert status.get("maintenance_error") == "startup_maintenance_failed"
    assert app.config.get("DURABLE_WORKFLOW_COMPONENTS") == {
        "worker_running": True,
        "scheduler_running": True,
    }


def test_durable_workflow_startup_reports_failure_before_queue_ready(
    monkeypatch,
):
    from flask import Flask

    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP", "0")
    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "_is_agent_test_instance", lambda: False)

    def _critical_failure(_app_logger, *, queue_ready_callback=None):
        assert queue_ready_callback is not None
        raise RuntimeError("critical_email_paper_policy_not_ready")

    monkeypatch.setattr(
        utils_flask,
        "_start_durable_workflow_system",
        _critical_failure,
    )

    app = Flask(__name__)
    utils_flask._configure_durable_workflow_startup(app)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
        if status.get("state") == "failed":
            break
        time.sleep(0.01)

    status = app.config.get("DURABLE_WORKFLOW_STARTUP_STATUS") or {}
    assert status.get("state") == "failed"
    assert status.get("ready") is False
    assert status.get("error") == "critical_email_paper_policy_not_ready"
    assert app.config.get("DURABLE_WORKFLOW_COMPONENTS") is None


def test_build_durable_workflow_registry_uses_shared_deferred_read_only_builder(
    monkeypatch,
):
    import src.backend.server.utils_flask as utils_flask
    import src.backend.workflows.durable.registry_factory as registry_factory

    sentinel_registry = object()
    observed: dict[str, object] = {}

    def _stub_get_shared_workflow_registry_read_only(
        *,
        defer_parity_work: bool = False,
        start_deferred_registry_work: bool = False,
    ):
        observed["defer_parity_work"] = defer_parity_work
        observed["start_deferred_registry_work"] = start_deferred_registry_work
        return sentinel_registry

    monkeypatch.setattr(
        registry_factory,
        "get_shared_workflow_registry_read_only",
        _stub_get_shared_workflow_registry_read_only,
    )

    result = utils_flask._build_durable_workflow_registry()

    assert result is sentinel_registry
    assert observed == {
        "defer_parity_work": True,
        "start_deferred_registry_work": True,
    }


def test_agent_test_startup_helpers_skip_remote_infrastructure(monkeypatch):
    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            raise AssertionError("AgentTest startup skip should not warn")

    app = types.SimpleNamespace(logger=_Logger(), config={}, testing=False)

    monkeypatch.setattr(
        utils_flask,
        "ensure_monitor_started",
        lambda: (_ for _ in ()).throw(AssertionError("DB monitor should not start")),
    )
    monkeypatch.setattr(
        utils_flask,
        "prompt_concept_health_status",
        lambda: (_ for _ in ()).throw(
            AssertionError("Prompt concept health should not touch Vontology")
        ),
    )
    monkeypatch.setattr(
        utils_flask,
        "_startup_requeue_unindexed_interaction_sessions",
        lambda _logger: (_ for _ in ()).throw(
            AssertionError("RAG startup requeue should not start")
        ),
    )
    monkeypatch.setattr(
        utils_flask,
        "_start_prewarm",
        lambda _app: (_ for _ in ()).throw(
            AssertionError("Prewarm should not start")
        ),
    )

    utils_flask._ensure_db_monitor_started(app)
    utils_flask._maybe_start_startup_rag_requeue(app)
    utils_flask._bootstrap_concept_summary_fields_for_startup(app)
    utils_flask._bootstrap_publication_scope_profiles_for_startup(app)
    utils_flask._bootstrap_constitutive_relation_requirements_for_startup(app)
    utils_flask._log_prompt_concept_health(app)
    utils_flask._register_optional_prewarm(app)

    assert app.config["CONCEPT_SUMMARY_FIELD_BOOTSTRAP_REPORT"] == {
        "success": True,
        "skipped": True,
        "reason": "agent_test_instance",
    }
    assert app.config["PUBLICATION_SCOPE_PROFILE_BOOTSTRAP_REPORT"] == {
        "success": True,
        "skipped": True,
        "reason": "agent_test_instance",
    }
    assert app.config["CONSTITUTIVE_RELATION_REQUIREMENT_BOOTSTRAP_REPORT"] == {
        "success": True,
        "skipped": True,
        "reason": "agent_test_instance",
    }


def test_publication_scope_profile_startup_records_bootstrap_report(
    monkeypatch,
) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.services import publication_scope_profile_vontology_service

    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "_is_agent_test_instance", lambda: False)
    monkeypatch.setenv("VON_PUBLICATION_SCOPE_PROFILE_BOOTSTRAP_ENABLE", "1")
    expected = {"success": True, "seed_version": "test"}
    monkeypatch.setattr(
        publication_scope_profile_vontology_service,
        "ensure_publication_scope_profiles_current_for_startup",
        lambda: expected,
    )
    app = types.SimpleNamespace(
        logger=types.SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        config={},
    )

    utils_flask._bootstrap_publication_scope_profiles_for_startup(app)

    assert app.config["PUBLICATION_SCOPE_PROFILE_BOOTSTRAP_REPORT"] == expected


def test_constitutive_requirement_startup_records_freshness_report(
    monkeypatch,
) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.services import constitutive_relation_requirement_service

    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "_is_agent_test_instance", lambda: False)
    monkeypatch.setenv(
        "VON_CONSTITUTIVE_RELATION_REQUIREMENT_BOOTSTRAP_ENABLE", "1"
    )
    expected = {"success": True, "ready": True, "seed_version": "test"}
    monkeypatch.setattr(
        constitutive_relation_requirement_service,
        "ensure_constitutive_relation_requirement_profiles_current_for_startup",
        lambda: expected,
    )
    app = types.SimpleNamespace(
        logger=types.SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        config={},
    )

    utils_flask._bootstrap_constitutive_relation_requirements_for_startup(app)

    assert (
        app.config["CONSTITUTIVE_RELATION_REQUIREMENT_BOOTSTRAP_REPORT"]
        == expected
    )
