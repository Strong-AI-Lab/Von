from __future__ import annotations

import json

from flask import Flask

from src.backend.db import connection_manager as conn_mgr
from src.backend.db import mongo_client as mongo_client
from src.backend.db.mongo_uri_redaction import (
    build_safe_mongo_connection_location,
    sanitize_mongo_uri_for_display,
)
from src.backend.server import utils_flask
from src.backend.server.routes import settings_routes
from src.backend.server.routes.settings_routes import (
    _classify_mongo_sanitized_uri,
    _sanitize_mongo_uri_for_display,
)


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


class _FakeDb:
    def command(self, *_args, **_kwargs):
        return {"ok": 1}


def _assert_no_secret_fragments(payload: object) -> None:
    text = json.dumps(payload, sort_keys=True)
    for fragment in SECRET_FRAGMENTS:
        assert fragment not in text


def test_sanitize_mongo_uri_for_display_drops_credentials_path_and_query() -> None:
    assert sanitize_mongo_uri_for_display(FAKE_SECRET_URI) == SAFE_URI
    assert _sanitize_mongo_uri_for_display(FAKE_SECRET_URI) == SAFE_URI


def test_safe_mongo_location_classifies_without_retaining_raw_uri() -> None:
    payload = build_safe_mongo_connection_location(
        FAKE_SECRET_URI,
        using_fallback=False,
    )

    assert payload["sanitized_uri"] == SAFE_URI
    assert payload["classification"] == "atlas"
    assert payload["is_atlas"] is True
    assert payload["using_fallback"] is False
    _assert_no_secret_fragments(payload)


def test_safe_mongo_location_identifies_atlas_through_ssh_tunnel() -> None:
    payload = build_safe_mongo_connection_location(
        "mongodb://appuser:s3cr3t-pass@127.0.0.1:27018/" "?directConnection=true",
        using_fallback=True,
        fallback_kind="ssh_tunnel",
        fallback_target_uri=FAKE_SECRET_URI,
    )

    assert payload["classification"] == "atlas"
    assert payload["endpoint_classification"] == "local"
    assert payload["upstream_classification"] == "atlas"
    assert payload["transport"] == "ssh_tunnel"
    assert payload["is_atlas"] is True
    assert payload["is_local"] is False
    assert payload["fallback_kind"] == "ssh_tunnel"
    _assert_no_secret_fragments(payload)


def test_safe_mongo_location_fails_closed_for_malformed_credential_uri() -> None:
    malformed_uri = (
        "mongodb+srv://appuser:s3cr3t/pass@cluster0.example.mongodb.net/"
        "von_db?authSource=admin"
    )

    assert sanitize_mongo_uri_for_display(malformed_uri) is None
    payload = build_safe_mongo_connection_location(malformed_uri)

    assert payload["available"] is False
    assert payload["hosts"] == []
    assert payload["sanitized_uri"] is None
    _assert_no_secret_fragments(payload)


def test_settings_route_classification_uses_shared_safe_location_helper() -> None:
    assert _classify_mongo_sanitized_uri(SAFE_URI) == "atlas"
    assert _classify_mongo_sanitized_uri("mongodb://localhost:27017") == "local"
    assert _classify_mongo_sanitized_uri("mongodb://db.example.invalid:27017") == "remote"


def test_health_summary_exposes_only_sanitized_effective_uri(monkeypatch) -> None:
    monkeypatch.setattr(conn_mgr, "get_db", lambda: _FakeDb())
    monkeypatch.setattr(
        conn_mgr._mc,
        "get_effective_mongo_uri",
        lambda: FAKE_SECRET_URI,
    )
    monkeypatch.setattr(conn_mgr._mc, "is_using_fallback_uri", lambda: False)
    monkeypatch.setattr(
        conn_mgr._mc,
        "get_mongo_fallback_policy_state",
        lambda: {"active_fallback_kind": None},
    )

    payload = conn_mgr.health_summary()

    assert payload["connected"] is True
    assert payload["effective_uri"] == SAFE_URI
    assert payload["effective_uri_sanitized"] == SAFE_URI
    assert payload["effective_mongo_location"]["classification"] == "atlas"
    _assert_no_secret_fragments(payload)


def test_admin_diag_mongo_payload_redacts_effective_uri(monkeypatch) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(mongo_client, "get_effective_mongo_uri", lambda: FAKE_SECRET_URI)
    monkeypatch.setattr(mongo_client, "is_using_fallback_uri", lambda: False)
    monkeypatch.setattr(
        mongo_client,
        "get_mongo_fallback_policy_state",
        lambda: {"active_fallback_kind": None},
    )

    with app.test_request_context("/diag"):
        response = utils_flask._build_diagnostics_response(app)

    payload = response.get_json()
    assert payload["mongo"]["effective_mongo_uri"] == SAFE_URI
    assert payload["mongo"]["effective_mongo_location"]["sanitized_uri"] == SAFE_URI
    _assert_no_secret_fragments(payload["mongo"])


def test_system_db_status_uses_safe_effective_host(monkeypatch) -> None:
    app = Flask(__name__)
    monkeypatch.setattr(mongo_client, "get_effective_mongo_uri", lambda: FAKE_SECRET_URI)
    monkeypatch.setattr(mongo_client, "is_using_fallback_uri", lambda: False)
    monkeypatch.setattr(
        mongo_client,
        "get_mongo_fallback_policy_state",
        lambda: {"active_fallback_kind": None},
    )

    with app.test_request_context("/api/system/db_status"):
        response = utils_flask._build_db_status_response()

    payload = response.get_json()
    assert payload["effective_host"] == "cluster0.example.mongodb.net"
    assert payload["atlas_detected"] is True
    assert payload["fallback_kind"] is None
    _assert_no_secret_fragments(payload)


def test_db_info_snapshots_route_after_ping_recovery(monkeypatch) -> None:
    app = Flask(__name__)
    state = {
        "uri": FAKE_SECRET_URI,
        "using_fallback": False,
        "kind": None,
    }

    def _get_db_after_route_switch():
        state.update(
            {
                "uri": (
                    "mongodb://appuser:s3cr3t-pass@127.0.0.1:27019/"
                    "?directConnection=true"
                ),
                "using_fallback": True,
                "kind": "ssh_tunnel",
            }
        )
        return _FakeDb()

    monkeypatch.setattr(settings_routes, "_read_cached_db_info", lambda **_kwargs: None)
    monkeypatch.setattr(settings_routes, "_write_cached_db_info", lambda _payload: None)
    monkeypatch.setattr(settings_routes, "get_db", _get_db_after_route_switch)
    monkeypatch.setattr(
        settings_routes, "get_effective_mongo_uri", lambda: state["uri"]
    )
    monkeypatch.setattr(
        settings_routes, "is_using_fallback_uri", lambda: state["using_fallback"]
    )
    monkeypatch.setattr(
        settings_routes,
        "get_mongo_fallback_policy_state",
        lambda: {"active_fallback_kind": state["kind"]},
    )
    monkeypatch.setattr(settings_routes, "MONGO_URI", FAKE_SECRET_URI)
    monkeypatch.setattr(settings_routes, "DATABASE_NAME", "test_database")

    with app.test_request_context("/api/settings/db/info?nocache=1"):
        response, status = settings_routes.get_db_location_info()

    payload = response.get_json()
    assert status == 200
    assert payload["sanitized_uri"] == "mongodb://127.0.0.1:27019"
    assert payload["using_fallback"] is True
    assert payload["fallback_kind"] == "ssh_tunnel"
    assert payload["classification"] == "atlas"
    assert payload["connection_location"]["upstream_sanitized_uri"] == SAFE_URI
    assert isinstance(payload["mongo_ping_latency_ms"], float)
    assert payload["mongo_ping_latency_ms"] >= 0.0
    _assert_no_secret_fragments(payload)


def test_db_info_public_ip_probe_does_not_change_global_socket_timeout(
    monkeypatch,
) -> None:
    app = Flask(__name__)
    socket_timeout_calls: list[float | None] = []
    urlopen_timeouts: list[float | None] = []

    class _FakeIpResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b"203.0.113.42"

    def _urlopen(_url: str, *, timeout: float | None = None):
        urlopen_timeouts.append(timeout)
        return _FakeIpResponse()

    monkeypatch.setattr(settings_routes, "_read_cached_db_info", lambda **_kwargs: None)
    monkeypatch.setattr(settings_routes, "_write_cached_db_info", lambda _payload: None)
    monkeypatch.setattr(settings_routes, "get_db", lambda: _FakeDb())
    monkeypatch.setattr(
        settings_routes, "get_effective_mongo_uri", lambda: FAKE_SECRET_URI
    )
    monkeypatch.setattr(settings_routes, "is_using_fallback_uri", lambda: True)
    monkeypatch.setattr(
        settings_routes,
        "get_mongo_fallback_policy_state",
        lambda: {"active_fallback_kind": "local"},
    )
    monkeypatch.setattr(settings_routes, "MONGO_URI", FAKE_SECRET_URI)
    monkeypatch.setattr(settings_routes, "DATABASE_NAME", "test_database")

    import socket
    import urllib.request

    monkeypatch.setattr(
        socket,
        "setdefaulttimeout",
        lambda value: socket_timeout_calls.append(value),
    )
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    with app.test_request_context("/api/settings/db/info?nocache=1"):
        response, status = settings_routes.get_db_location_info()

    assert status == 200
    assert response.get_json()["server_public_ip"] == "203.0.113.42"
    assert urlopen_timeouts == [1.5]
    assert socket_timeout_calls == []
