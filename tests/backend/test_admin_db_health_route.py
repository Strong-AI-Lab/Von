from __future__ import annotations

import json

from flask import Flask

from src.backend.db import connection_manager as conn_mgr
import src.backend.server.routes.admin_routes as admin_routes
from src.backend.server.routes.admin_routes import admin_bp

FAKE_SECRET_URI = (
    "mongodb+srv://appuser:s3cr3t-pass@cluster0.example.mongodb.net/"
    "von_db?authSource=admin&appName=VonSecret&tls=true&retryWrites=true"
)
SAFE_URI = "mongodb+srv://cluster0.example.mongodb.net"
SECRET_FRAGMENTS = (
    "appuser",
    "s3cr3t-pass",
    "admin",
    "authSource",
    "appName",
    "VonSecret",
    "tls=true",
    "retryWrites",
    "von_db",
    FAKE_SECRET_URI,
)


def _build_test_client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(admin_bp)
    return app.test_client()


class _FakeDb:
    def command(self, *_args, **_kwargs):
        return {"ok": 1}


def _patch_secret_mongo_health(monkeypatch) -> None:
    monkeypatch.setattr(conn_mgr, "get_db", lambda: _FakeDb())
    monkeypatch.setattr(conn_mgr._mc, "get_effective_mongo_uri", lambda: FAKE_SECRET_URI)
    monkeypatch.setattr(conn_mgr._mc, "is_using_fallback_uri", lambda: False)


def _assert_no_secret_fragments(payload: object) -> None:
    text = json.dumps(payload, sort_keys=True)
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in text


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


def test_db_health_probe_rw_redacts_effective_mongo_uri(monkeypatch) -> None:
    client = _build_test_client()
    _patch_secret_mongo_health(monkeypatch)
    monkeypatch.setattr(
        admin_routes,
        "run_mongo_startup_probe",
        lambda: {"ok": True, "read_ok": True, "write_ok": True},
    )

    response = client.get("/admin/db/health?probe=rw")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["effective_uri"] == SAFE_URI
    assert payload["effective_uri_sanitized"] == SAFE_URI
    assert payload["effective_mongo_location"]["sanitized_uri"] == SAFE_URI
    assert payload["effective_mongo_location"]["classification"] == "atlas"
    _assert_no_secret_fragments(payload)


def test_db_reconnect_redacts_effective_mongo_uri(monkeypatch) -> None:
    client = _build_test_client()
    _patch_secret_mongo_health(monkeypatch)

    response = client.post("/admin/db/reconnect", json={"force": False})
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["skipped"] is True
    assert payload["reason"] == "already_connected"
    assert payload["effective_uri"] == SAFE_URI
    _assert_no_secret_fragments(payload)
