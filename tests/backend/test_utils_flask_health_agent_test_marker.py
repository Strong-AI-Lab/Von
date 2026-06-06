from __future__ import annotations

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

    app = Flask(__name__)
    app.config["SERVER_START_TIME"] = "test-start"

    with app.app_context():
        payload = utils_flask._build_health_check_response(app).get_json()

    assert payload["agent_test_instance"] is True
    assert payload["agent_test_environment_marker"] == "VON_AGENT_TEST_INSTANCE"
    assert payload["local_ip"] == "127.0.0.1"
    assert payload["public_ip"] is None
    assert network_calls == {"socket": 0, "urlopen": 0}


def test_agent_test_instance_skips_durable_workflow_startup(monkeypatch) -> None:
    import src.backend.server.utils_flask as utils_flask

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)

    app = Flask(__name__)

    utils_flask._configure_durable_workflow_startup(app)

    status = app.config["DURABLE_WORKFLOW_STARTUP_STATUS"]
    assert status["state"] == "skipped_agent_test"
    assert status["ready"] is False
    assert app.config["DURABLE_WORKFLOW_COMPONENTS"] is None


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
