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
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: _FakeSocket())

    def _raise_urlopen(*_args: object, **_kwargs: object) -> object:
        raise OSError("network disabled in test")

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
