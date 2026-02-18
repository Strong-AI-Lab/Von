from __future__ import annotations

import time

import pytest
from pymongo.errors import ConnectionFailure

from src.backend.db import connection_manager as conn_mgr
from src.backend.db import mongo_client as mc


@pytest.fixture(autouse=True)
def _restore_connection_manager_state():
    snapshot = dict(conn_mgr._state)
    yield
    conn_mgr._state.clear()
    conn_mgr._state.update(snapshot)


class _FakeAdmin:
    def __init__(self, fail_ping: bool = False) -> None:
        self.fail_ping = fail_ping
        self.commands: list[tuple[str, dict]] = []

    def command(self, name: str, **kwargs):
        self.commands.append((name, dict(kwargs)))
        if name == "ping" and self.fail_ping:
            raise ConnectionFailure("simulated ping failure")
        return {"ok": 1}


class _FakeClient:
    def __init__(self, label: str, fail_ping: bool = False) -> None:
        self.label = label
        self.admin = _FakeAdmin(fail_ping=fail_ping)

    def __getitem__(self, db_name: str):
        return {"label": self.label, "db_name": db_name}


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
            return {"connected": False}
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
    assert "monitor_loop_error" in str(conn_mgr._state.get("last_error"))
