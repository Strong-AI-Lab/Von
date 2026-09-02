from __future__ import annotations

import hashlib
import json

from flask import Flask


class _FakeSocket:
    def connect(self, _address: object) -> None:
        return None

    def getsockname(self) -> tuple[str, int]:
        return ("127.0.0.1", 12345)

    def close(self) -> None:
        return None


def test_health_response_exposes_agent_test_instance_marker(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.db import mongo_client, mongo_uri_redaction

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    network_calls = {"socket": 0, "urlopen": 0}

    def _socket(*_args: object, **_kwargs: object) -> _FakeSocket:
        network_calls["socket"] += 1
        return _FakeSocket()

    def _raise_urlopen(*_args: object, **_kwargs: object) -> object:
        network_calls["urlopen"] += 1
        raise OSError("network disabled in test")

    monkeypatch.setattr("socket.socket", _socket)
    monkeypatch.setattr("urllib.request.urlopen", _raise_urlopen)
    monkeypatch.setattr(
        utils_flask,
        "get_runtime_code_version_info",
        lambda: {"version": "test", "git_branch": "main"},
    )
    monkeypatch.setattr(
        mongo_client,
        "get_effective_mongo_uri",
        lambda: "mongodb://secret-user:secret-password@db.example/von",
    )
    monkeypatch.setattr(mongo_client, "is_using_fallback_uri", lambda: False)
    monkeypatch.setattr(mongo_client, "get_configured_database_name", lambda: "von")
    safe_location = {
        "classification": "atlas",
        "sanitized_uri": "mongodb+srv://db.example/von",
        "using_fallback": False,
    }
    monkeypatch.setattr(
        mongo_uri_redaction,
        "build_safe_mongo_connection_location",
        lambda *_args, **_kwargs: safe_location,
    )

    app = Flask(__name__)
    app.config["SERVER_START_TIME"] = "test-start"

    with app.app_context():
        payload = utils_flask._build_health_check_response(app).get_json()

    assert payload["agent_test_instance"] is True
    assert payload["agent_test_environment_marker"] == "VON_AGENT_TEST_INSTANCE"
    assert payload["represented_postcondition_critic_enabled"] is False
    assert payload["local_ip"] == "127.0.0.1"
    assert payload["public_ip"] is None
    runtime_authority = payload["runtime_authority"]
    assert runtime_authority["schema_version"] == (
        "health_runtime_authority_projection.v1"
    )
    assert runtime_authority["mongo"] == {
        "effective_mongo_location_sha256": hashlib.sha256(
            json.dumps(
                safe_location,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
        "effective_database_name_sha256": hashlib.sha256(b"von").hexdigest(),
    }
    assert runtime_authority["durable_workflows"]["worker_running"] is False
    assert runtime_authority["startup_seed_materialisations"]["state"] == (
        "not_checked"
    )
    assert "secret-user" not in json.dumps(payload)
    assert "secret-password" not in json.dumps(payload)
    assert network_calls == {"socket": 0, "urlopen": 0}


def test_health_response_reports_represented_agent_test_critic(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setenv("VON_AGENT_TEST_REAL_POSTCONDITION_CRITIC", "1")
    monkeypatch.setattr(
        utils_flask,
        "get_runtime_code_version_info",
        lambda: {"version": "test", "git_branch": "main"},
    )

    app = Flask(__name__)
    app.config["SERVER_START_TIME"] = "test-start"

    with app.app_context():
        payload = utils_flask._build_health_check_response(app).get_json()

    assert payload["agent_test_instance"] is True
    assert payload["represented_postcondition_critic_enabled"] is True


def test_agent_test_instance_skips_durable_workflow_startup(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask
    from src.backend.services import workflow_capability_service

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    startup_calls: list[float] = []

    def _start_memory_only_capability_index(*, timeout_seconds: float) -> dict:
        startup_calls.append(timeout_seconds)
        return {
            "success": False,
            "ready": False,
            "status": "building",
        }

    monkeypatch.setattr(
        workflow_capability_service,
        "run_workflow_capability_index_startup_check",
        _start_memory_only_capability_index,
    )

    app = Flask(__name__)

    utils_flask._configure_durable_workflow_startup(app)

    status = app.config["DURABLE_WORKFLOW_STARTUP_STATUS"]
    assert status["state"] == "skipped_agent_test"
    assert status["ready"] is False
    assert app.config["DURABLE_WORKFLOW_COMPONENTS"] is None
    assert startup_calls == [0.0]
    assert app.config["WORKFLOW_CAPABILITY_INDEX_STARTUP_REPORT"] == {
        "success": False,
        "ready": False,
        "status": "building",
    }


def test_agent_test_diagnostics_reuse_skipped_durable_startup_status(
    monkeypatch,
) -> None:
    import src.backend.server.utils_flask as utils_flask
    import src.backend.workflows.durable.startup as durable_startup

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")

    def _raise_get_system_status() -> object:
        raise AssertionError("AgentTest diagnostics must not query durable status")

    monkeypatch.setattr(durable_startup, "get_system_status", _raise_get_system_status)

    app = Flask(__name__)
    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {
        "state": "skipped_agent_test",
        "ready": False,
        "reason": "agent_test_instance",
    }

    payload = utils_flask._build_diagnostics_durable_workflow_status(app)

    assert payload["available"] is False
    assert payload["state"] == "skipped_agent_test"
    assert payload["worker_running"] is False
    assert payload["scheduler_running"] is False
    assert payload["source"] == "agent_test_startup_status"
    assert payload["startup_status"] == {
        "state": "skipped_agent_test",
        "ready": False,
        "reason": "agent_test_instance",
    }


def test_diagnostics_use_live_durable_workflow_status_by_default(
    monkeypatch,
) -> None:
    import src.backend.server.utils_flask as utils_flask
    import src.backend.workflows.durable.startup as durable_startup

    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    monkeypatch.setattr(
        durable_startup,
        "get_system_status",
        lambda: {"available": True, "worker_running": True},
    )

    app = Flask(__name__)

    payload = utils_flask._build_diagnostics_durable_workflow_status(app)

    assert payload == {"available": True, "worker_running": True}


def test_health_runtime_authority_uses_count_free_durable_status(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask
    import src.backend.services.model_registry_service as model_registry_service
    import src.backend.workflows.durable.startup as durable_startup

    monkeypatch.delenv("VON_AGENT_TEST_INSTANCE", raising=False)
    calls: list[bool] = []

    def _status(*, include_counts: bool = True) -> dict[str, object]:
        calls.append(include_counts)
        return {
            "database_connected": True,
            "worker_running": True,
            "scheduler_running": True,
            "worker_id": "must-not-be-public",
        }

    monkeypatch.setattr(durable_startup, "get_system_status", _status)
    monkeypatch.setattr(
        model_registry_service,
        "get_model_registry_snapshot_status",
        lambda: {
            "schema_version": "model_registry_snapshot_status.v1",
            "ready": True,
            "source": "vontology_graph",
            "cache_state": "stale_refreshing",
            "age_seconds": 3612.5,
            "refresh_in_progress": True,
            "last_refresh_succeeded": True,
        },
    )
    app = Flask(__name__)
    app.config["DURABLE_WORKFLOW_STARTUP_STATUS"] = {"state": "ready"}

    payload = utils_flask._build_health_runtime_authority_projection(app)

    assert calls == [False]
    assert payload["durable_workflows"] == {
        "available": True,
        "state": "ready",
        "database_connected": True,
        "worker_running": True,
        "scheduler_running": True,
    }
    assert payload["model_registry"] == {
        "schema_version": "model_registry_snapshot_status.v1",
        "ready": True,
        "source": "vontology_graph",
        "cache_state": "stale_refreshing",
        "age_seconds": 3612.5,
        "refresh_in_progress": True,
        "last_refresh_succeeded": True,
    }
    assert "worker_id" not in json.dumps(payload)


def test_health_projects_startup_seed_family_readiness_without_raw_report() -> None:
    import src.backend.server.utils_flask as utils_flask

    app = Flask(__name__)
    app.config["CONCEPT_SUMMARY_FIELD_BOOTSTRAP_REPORT"] = {
        "success": True,
        "ready": True,
        "state": "ready",
        "reason": "dependency_receipt_current",
        "duration_ms": 12,
        "freshness_receipt": {
            "reason": "dependency_receipt_current",
            "private_checkpoint": "must-not-be-public",
        },
    }
    app.config["PUBLICATION_SCOPE_PROFILE_BOOTSTRAP_REPORT"] = {
        "success": False,
        "ready": False,
        "state": "unavailable",
        "reason": "startup_seed_reconciliation_required",
        "reconciliation_required": True,
        "duration_ms": 8,
        "freshness_receipt": {
            "reason": "source_digest_mismatch",
            "private_checkpoint": "must-not-be-public",
        },
    }

    payload = utils_flask._build_startup_seed_materialisations_projection(app)

    assert payload == {
        "schema_version": "startup_seed_materialisation_status.v1",
        "ready": False,
        "state": "unavailable",
        "families": {
            "concept_summary_fields": {
                "ready": True,
                "state": "ready",
                "reason": "dependency_receipt_current",
                "reconciliation_required": False,
                "receipt_reason": "dependency_receipt_current",
                "duration_ms": 12,
            },
            "publication_scope_profiles": {
                "ready": False,
                "state": "unavailable",
                "reason": "startup_seed_reconciliation_required",
                "reconciliation_required": True,
                "receipt_reason": "source_digest_mismatch",
                "duration_ms": 8,
            },
            "constitutive_relation_requirements": {
                "ready": False,
                "state": "not_checked",
                "reason": None,
                "reconciliation_required": False,
                "receipt_reason": None,
                "duration_ms": None,
            },
        },
    }
    assert "private_checkpoint" not in json.dumps(payload)
