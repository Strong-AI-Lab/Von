from __future__ import annotations

from flask import Flask

from src.backend.server import utils_flask
from src.backend.services import relationship_extent_index_service as index_service


def test_startup_relationship_extent_reconciliation_runs_in_background(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    calls: list[dict] = []

    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "_is_agent_test_instance", lambda: False)
    monkeypatch.setattr(utils_flask, "_env_bool", lambda _name, _default: True)
    monkeypatch.setattr(
        index_service,
        "reconcile_relationship_extent_index_if_needed",
        lambda **kwargs: calls.append(kwargs)
        or {"success": True, "status": "ready", "changed": True},
    )

    class _ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(utils_flask.threading, "Thread", _ImmediateThread)

    utils_flask._maybe_start_relationship_extent_index_reconciliation(app)

    assert calls == [{"reason": "server_startup_reconciliation"}]
    assert app.config["RELATIONSHIP_EXTENT_INDEX_RECONCILIATION"] == {
        "success": True,
        "status": "ready",
        "changed": True,
    }


def test_startup_relationship_extent_reconciliation_skips_agent_test(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(utils_flask, "_is_running_under_pytest", lambda: False)
    monkeypatch.setattr(utils_flask, "_is_agent_test_instance", lambda: True)
    monkeypatch.setattr(
        utils_flask.threading,
        "Thread",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("agent-test startup must not launch reconciliation")
        ),
    )

    utils_flask._maybe_start_relationship_extent_index_reconciliation(app)

    assert "RELATIONSHIP_EXTENT_INDEX_RECONCILIATION" not in app.config
