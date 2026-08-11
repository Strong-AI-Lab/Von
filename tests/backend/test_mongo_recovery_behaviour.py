from __future__ import annotations

import time

import pytest
from pymongo.errors import (
    ConnectionFailure,
    NetworkTimeout,
    NotPrimaryError,
    PyMongoError,
)

from src.backend.db import connection_manager as conn_mgr
from src.backend.db import mongo_client as mc
from src.backend.db import transient_errors


@pytest.fixture(autouse=True)
def _restore_connection_manager_state():
    snapshot = dict(conn_mgr._state)
    yield
    conn_mgr._state.clear()
    conn_mgr._state.update(snapshot)


class _FakeAdmin:
    def __init__(
        self,
        fail_ping: bool = False,
        hello: dict[str, object] | None = None,
    ) -> None:
        self.fail_ping = fail_ping
        self.hello = hello or {"ok": 1}
        self.commands: list[tuple[str, dict]] = []

    def command(self, name: str, **kwargs):
        self.commands.append((name, dict(kwargs)))
        if name == "ping" and self.fail_ping:
            raise ConnectionFailure("simulated ping failure")
        if name == "hello":
            return dict(self.hello)
        return {"ok": 1}


class _FakeClient:
    def __init__(
        self,
        label: str,
        fail_ping: bool = False,
        hello: dict[str, object] | None = None,
    ) -> None:
        self.label = label
        self.admin = _FakeAdmin(fail_ping=fail_ping, hello=hello)

    def __getitem__(self, db_name: str):
        return {"label": self.label, "db_name": db_name}


def test_new_client_bounds_connection_but_not_operation_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, dict]] = []
    sentinel = object()

    def _fake_mongo_client(uri: str, **kwargs):
        created.append((uri, dict(kwargs)))
        return sentinel

    monkeypatch.setattr(mc, "MongoClient", _fake_mongo_client)
    monkeypatch.setenv("MONGO_CONNECT_TIMEOUT_MS", "4321")
    monkeypatch.setenv("MONGO_SOCKET_TIMEOUT_MS", "7")

    result = mc._new_mongo_client(
        "mongodb://example.invalid/",
        server_selection_timeout_ms=1234,
    )

    assert result is sentinel
    assert created == [
        (
            "mongodb://example.invalid/",
            {
                "serverSelectionTimeoutMS": 1234,
                "connectTimeoutMS": 4321,
                "retryWrites": True,
                "retryReads": True,
            },
        )
    ]


def test_get_db_rebuilds_client_after_periodic_ping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _FakeClient(label="stale", fail_ping=True)
    refreshed = _FakeClient(label="fresh", fail_ping=False)
    created: list[tuple[str, dict]] = []

    def _fake_mongo_client(uri: str, **kwargs):
        created.append((uri, dict(kwargs)))
        return refreshed

    monkeypatch.setattr(mc, "MongoClient", _fake_mongo_client)
    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ())
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(mc, "MONGO_URI", "mongodb://cluster0.mongodb.net/von_db?tls=true")
    monkeypatch.setattr(mc, "_mongo_client_real", existing)
    monkeypatch.setattr(mc, "_mongo_client_mock", None)
    monkeypatch.setattr(mc, "_last_auto_recovery_check_at", 0.0)

    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY", "1")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "0.001")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_PING_TIMEOUT_MS", "25")

    db = mc.get_db()

    assert db["label"] == "fresh"
    assert created, "Expected a fresh MongoClient to be created after ping failure."
    assert any(name == "ping" for name, _ in existing.admin.commands)


def test_direct_ping_failure_selects_ssh_even_when_direct_hello_would_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _FakeClient(label="direct", fail_ping=True)
    tunneled = _FakeClient(
        label="tunneled",
        hello={"ok": 1, "setName": "atlas-set", "isWritablePrimary": True},
    )
    primary_uri = "mongodb+srv://user:pass@cluster.example/"
    created: list[str] = []

    def _fake_new_client(uri: str, **_kwargs):
        created.append(uri)
        if uri == primary_uri:
            raise AssertionError("degraded direct route must not be retried first")
        return tunneled

    monkeypatch.setattr(mc, "_new_mongo_client", _fake_new_client)
    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "MONGO_URI", primary_uri)
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(
        mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ("127.0.0.1:27018",)
    )
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET", "atlas-set")
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setattr(mc, "_mongo_client_real", existing)
    monkeypatch.setattr(mc, "_mongo_client_mock", None)
    monkeypatch.setattr(mc, "_using_fallback_real", False)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", None)
    monkeypatch.setattr(mc, "_preferred_fallback_kind", None)
    monkeypatch.setattr(mc, "_dns_fallback_preferred_until_monotonic", 0.0)
    monkeypatch.setattr(mc, "_last_auto_recovery_check_at", 0.0)
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY", "1")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "0.001")

    db = mc.get_db()

    assert db is not None
    assert db["label"] == "tunneled"
    assert created and "127.0.0.1:27018" in created[0]
    assert mc.get_mongo_fallback_policy_state()["active_fallback_kind"] == "ssh_tunnel"


def test_get_db_skips_health_ping_when_interval_not_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    healthy = _FakeClient(label="healthy", fail_ping=False)

    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "_mongo_client_real", healthy)
    monkeypatch.setattr(mc, "_mongo_client_mock", None)
    monkeypatch.setattr(mc, "_last_auto_recovery_check_at", time.monotonic())
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY", "1")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "3600")

    def _unexpected_constructor(*args, **kwargs):
        raise AssertionError("MongoClient constructor should not run in this path.")

    monkeypatch.setattr(mc, "MongoClient", _unexpected_constructor)

    db = mc.get_db()

    assert db["label"] == "healthy"
    assert all(name != "ping" for name, _ in healthy.admin.commands)


def test_direct_route_degradation_prefers_ssh_tunnel(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_using_fallback_real", False)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", None)
    monkeypatch.setattr(
        mc, "MONGO_URI", "mongodb+srv://user:pass@cluster.example/"
    )
    monkeypatch.setattr(
        mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ("127.0.0.1:27018",)
    )
    monkeypatch.setattr(
        mc,
        "MONGO_DNS_FALLBACK_URI",
        "mongodb://direct.example:27017/?tls=true",
    )
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)

    selected = mc.prefer_alternate_mongo_route("test transport degradation")

    assert selected == "ssh_tunnel"
    assert mc.get_mongo_fallback_policy_state()["preferred_fallback_kind"] == (
        "ssh_tunnel"
    )


def test_dns_route_degradation_rotates_to_ssh_tunnel(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_using_fallback_real", True)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", "dns")
    monkeypatch.setattr(
        mc, "MONGO_URI", "mongodb+srv://user:pass@cluster.example/"
    )
    monkeypatch.setattr(
        mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ("127.0.0.1:27018",)
    )
    monkeypatch.setattr(
        mc,
        "MONGO_DNS_FALLBACK_URI",
        "mongodb://direct.example:27017/?tls=true",
    )
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)

    assert (
        mc.prefer_alternate_mongo_route("test DNS route degradation")
        == "ssh_tunnel"
    )


def test_transient_transport_retry_marks_route_degraded(monkeypatch) -> None:
    calls: list[dict] = []
    attempts = 0

    def _operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NetworkTimeout("simulated operation timeout")
        return "ok"

    monkeypatch.setattr(
        transient_errors,
        "attempt_reconnect",
        lambda **kwargs: calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(transient_errors.time, "sleep", lambda _delay: None)

    result = transient_errors.run_with_transient_mongo_retry(
        _operation,
        operation_name="test-operation",
        max_attempts=2,
    )

    assert result == "ok"
    assert calls[0]["route_degraded"] is True
    assert calls[0]["degradation_reason"] == "transient operation NetworkTimeout"


def test_non_transport_pymongo_retry_does_not_rotate_route(monkeypatch) -> None:
    calls: list[dict] = []
    attempts = 0

    def _operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PyMongoError("semantic command failure")
        return "ok"

    monkeypatch.setattr(
        transient_errors,
        "attempt_reconnect",
        lambda **kwargs: calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(transient_errors.time, "sleep", lambda _delay: None)

    assert (
        transient_errors.run_with_transient_mongo_retry(
            _operation,
            operation_name="test-operation",
            max_attempts=2,
        )
        == "ok"
    )
    assert calls[0]["route_degraded"] is False


def test_replica_election_retry_does_not_rotate_route(monkeypatch) -> None:
    calls: list[dict] = []
    attempts = 0

    def _operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise NotPrimaryError("simulated replica election")
        return "ok"

    monkeypatch.setattr(
        transient_errors,
        "attempt_reconnect",
        lambda **kwargs: calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(transient_errors.time, "sleep", lambda _delay: None)

    assert (
        transient_errors.run_with_transient_mongo_retry(
            _operation,
            operation_name="test-operation",
            max_attempts=2,
        )
        == "ok"
    )
    assert calls[0]["route_degraded"] is False


def test_route_degradation_without_alternate_keeps_primary(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_using_fallback_real", False)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", None)
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ())
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)

    assert mc.prefer_alternate_mongo_route("test") is None


@pytest.mark.parametrize("active_kind", ("dns", "ssh_tunnel"))
def test_route_degradation_without_alternate_reprobes_primary(
    monkeypatch,
    active_kind: str,
) -> None:
    monkeypatch.setattr(mc, "_using_fallback_real", True)
    monkeypatch.setattr(mc, "_active_fallback_kind_real", active_kind)
    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018",) if active_kind == "ssh_tunnel" else (),
    )
    monkeypatch.setattr(
        mc,
        "MONGO_DNS_FALLBACK_URI",
        "mongodb://direct.example:27017/" if active_kind == "dns" else None,
    )
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setattr(mc, "_preferred_fallback_kind", active_kind)
    monkeypatch.setattr(
        mc, "_dns_fallback_preferred_until_monotonic", time.monotonic() + 60
    )

    assert mc.prefer_alternate_mongo_route("test") is None
    assert mc.get_mongo_fallback_policy_state()["preferred_fallback_kind"] is None


def test_monitor_loop_survives_health_summary_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health_calls = {"count": 0}
    reconnect_calls: list[dict] = []

    def _fake_health_summary():
        health_calls["count"] += 1
        if health_calls["count"] == 1:
            raise RuntimeError("simulated monitor error")
        if health_calls["count"] == 2:
            return {
                "connected": False,
                "route_degraded": True,
                "health_error_type": "NetworkTimeout",
            }
        return {"connected": True}

    def _fake_attempt_reconnect(**kwargs):
        reconnect_calls.append(dict(kwargs))
        raise StopIteration("stop test loop")

    sleep_calls = {"count": 0}

    def _fake_sleep(_seconds: float) -> None:
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 3:
            raise StopIteration("stop test loop")

    monkeypatch.setattr(conn_mgr, "health_summary", _fake_health_summary)
    monkeypatch.setattr(conn_mgr, "attempt_reconnect", _fake_attempt_reconnect)
    monkeypatch.setattr(conn_mgr.time, "sleep", _fake_sleep)

    with pytest.raises(StopIteration):
        conn_mgr._monitor_loop(interval_sec=1, base_ms=100, max_ms=1000, max_attempts=2)

    assert reconnect_calls, "Expected reconnect attempt after transient monitor error."
    assert reconnect_calls[0]["route_degraded"] is True
    assert reconnect_calls[0]["degradation_reason"] == (
        "monitor ping NetworkTimeout"
    )
    assert "monitor_loop_error" in str(conn_mgr._state.get("last_error"))
