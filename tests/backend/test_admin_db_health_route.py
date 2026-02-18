from __future__ import annotations

from flask import Flask

import src.backend.server.routes.admin_routes as admin_routes
from src.backend.server.routes.admin_routes import admin_bp


def _build_test_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(admin_bp)
    return app.test_client()


def test_db_health_probe_rw_success(monkeypatch) -> None:
    client = _build_test_client()
    monkeypatch.setattr(
        admin_routes.conn_mgr,
        "health_summary",
        lambda: {"connected": True, "using_fallback": False},
    )
    monkeypatch.setattr(
        admin_routes,
        "run_mongo_startup_probe",
        lambda: {"ok": True, "read_ok": True, "write_ok": True},
    )

    response = client.get("/admin/db/health?probe=rw")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["connected"] is True
    assert payload["probe"]["ok"] is True


def test_db_health_probe_rw_failure_returns_503(monkeypatch) -> None:
    client = _build_test_client()
    monkeypatch.setattr(
        admin_routes.conn_mgr,
        "health_summary",
        lambda: {"connected": True},
    )

    def _raise_probe_error() -> dict:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(admin_routes, "run_mongo_startup_probe", _raise_probe_error)

    response = client.get("/admin/db/health?probe=rw")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["probe"]["ok"] is False
    assert "probe exploded" in payload["probe"]["error"]
