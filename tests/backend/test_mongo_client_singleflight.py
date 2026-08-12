from __future__ import annotations

import threading
import time

import pytest
from pymongo.errors import ConnectionFailure

from src.backend.db import mongo_client as mc


class _FakeAdmin:
    def __init__(
        self,
        *,
        ping_started: threading.Event | None = None,
        release_ping: threading.Event | None = None,
        fail_ping: bool = False,
    ) -> None:
        self.ping_started = ping_started
        self.release_ping = release_ping
        self.fail_ping = fail_ping
        self.ping_count = 0

    def command(self, name: str, **_kwargs):
        if name == "hello":
            return {"ok": 1, "isWritablePrimary": True}
        if name == "ping":
            self.ping_count += 1
            if self.ping_started is not None:
                self.ping_started.set()
            if self.release_ping is not None:
                assert self.release_ping.wait(timeout=2)
            if self.fail_ping:
                raise ConnectionFailure("simulated health failure")
        return {"ok": 1}


class _FakeClient:
    def __init__(self, label: str, admin: _FakeAdmin | None = None) -> None:
        self.label = label
        self.admin = admin or _FakeAdmin()
        self.closed = False

    def __getitem__(self, db_name: str):
        return {"label": self.label, "db_name": db_name, "client": self}

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolated_real_client_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mc, "is_mock_db_enabled", lambda: False)
    monkeypatch.setattr(mc, "assert_safe_database_name_for_pytest", lambda _: None)
    monkeypatch.setattr(mc, "MONGO_URI", "mongodb://primary.example/von_db")
    monkeypatch.setattr(mc, "MONGO_DNS_FALLBACK_URI", None)
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS", ())
    monkeypatch.setattr(mc, "MONGO_ALLOW_LOCAL_FALLBACK", False)
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY", "0")
    with mc._CONNECTION_CONDITION:
        snapshot = {
            "client": mc._mongo_client_real,
            "mock": mc._mongo_client_mock,
            "using_fallback": mc._using_fallback_real,
            "active_fallback_kind": mc._active_fallback_kind_real,
            "effective_uri": mc._effective_uri_real,
            "preferred_fallback_kind": mc._preferred_fallback_kind,
            "preferred_until": mc._dns_fallback_preferred_until_monotonic,
            "preference_reason": mc._dns_fallback_preference_reason,
            "last_check": mc._last_auto_recovery_check_at,
            "generation": mc._connection_generation,
            "waves": dict(mc._connection_waves),
        }
        mc._mongo_client_real = None
        mc._mongo_client_mock = None
        mc._using_fallback_real = False
        mc._active_fallback_kind_real = None
        mc._effective_uri_real = mc.MONGO_URI
        mc._preferred_fallback_kind = None
        mc._dns_fallback_preferred_until_monotonic = 0.0
        mc._dns_fallback_preference_reason = None
        mc._last_auto_recovery_check_at = 0.0
        mc._connection_generation += 1
        mc._connection_waves.clear()
    yield
    assert not mc._HEALTH_CHECK_LOCK.locked()
    with mc._CONNECTION_CONDITION:
        mc._mongo_client_real = snapshot["client"]
        mc._mongo_client_mock = snapshot["mock"]
        mc._using_fallback_real = snapshot["using_fallback"]
        mc._active_fallback_kind_real = snapshot["active_fallback_kind"]
        mc._effective_uri_real = snapshot["effective_uri"]
        mc._preferred_fallback_kind = snapshot["preferred_fallback_kind"]
        mc._dns_fallback_preferred_until_monotonic = snapshot["preferred_until"]
        mc._dns_fallback_preference_reason = snapshot["preference_reason"]
        mc._last_auto_recovery_check_at = snapshot["last_check"]
        mc._connection_generation = snapshot["generation"]
        mc._connection_waves.clear()
        mc._connection_waves.update(snapshot["waves"])


def _wait_for_wave_waiters(expected: int) -> None:
    with mc._CONNECTION_CONDITION:
        assert mc._CONNECTION_CONDITION.wait_for(
            lambda: any(
                wave.waiter_count >= expected for wave in mc._connection_waves.values()
            ),
            timeout=2,
        )


def _run_concurrently(count: int, operation):
    start = threading.Barrier(count + 1)
    results: list[object] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            start.wait(timeout=2)
            results.append(operation())
        except BaseException as exc:  # noqa: BLE001 - asserted by the test thread
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    start.wait(timeout=2)
    return threads, results, errors


def _join(threads: list[threading.Thread], errors: list[BaseException]) -> None:
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not errors


def test_concurrent_cold_get_db_runs_one_connection_wave(monkeypatch) -> None:
    connect_started = threading.Event()
    release_connect = threading.Event()
    client = _FakeClient("shared")
    connect_count = 0
    count_lock = threading.Lock()

    def _connect(_uri: str, **_kwargs):
        nonlocal connect_count
        with count_lock:
            connect_count += 1
        connect_started.set()
        assert release_connect.wait(timeout=2)
        return client

    monkeypatch.setattr(mc, "_try_connect_uri", _connect)
    threads, results, errors = _run_concurrently(12, mc.get_db)
    assert connect_started.wait(timeout=2)
    _wait_for_wave_waiters(11)
    assert connect_count == 1
    release_connect.set()
    _join(threads, errors)

    assert len(results) == 12
    assert {result["client"] for result in results} == {client}
    assert connect_count == 1


def test_failed_wave_is_shared_and_a_later_call_retries(monkeypatch) -> None:
    connect_started = threading.Event()
    release_failure = threading.Event()
    recovered = _FakeClient("recovered")
    connect_count = 0
    count_lock = threading.Lock()

    def _connect(_uri: str, **_kwargs):
        nonlocal connect_count
        with count_lock:
            connect_count += 1
            attempt = connect_count
        if attempt == 1:
            connect_started.set()
            assert release_failure.wait(timeout=2)
            raise ConnectionFailure("shared simulated outage")
        return recovered

    monkeypatch.setattr(mc, "_try_connect_uri", _connect)
    threads, results, errors = _run_concurrently(10, mc.get_db)
    assert connect_started.wait(timeout=2)
    _wait_for_wave_waiters(9)
    release_failure.set()
    _join(threads, errors)

    assert results == [None] * 10
    assert connect_count == 1
    retry = mc.get_db()
    assert retry is not None
    assert retry["client"] is recovered
    assert connect_count == 2


def test_cancelled_connection_wave_releases_waiters_and_later_call_retries(
    monkeypatch,
) -> None:
    class _CancelledBuild(BaseException):
        pass

    build_started = threading.Event()
    release_build = threading.Event()
    recovered = _FakeClient("recovered-after-cancellation")
    build_count = 0
    count_lock = threading.Lock()

    def _build_candidate():
        nonlocal build_count
        with count_lock:
            build_count += 1
            attempt = build_count
        if attempt == 1:
            build_started.set()
            assert release_build.wait(timeout=2)
            raise _CancelledBuild("cancelled connection construction")
        return (
            mc._MongoConnectionCandidate(
                client=recovered,
                uri=mc.MONGO_URI,
                fallback_kind=None,
            ),
            False,
        )

    monkeypatch.setattr(mc, "_build_real_connection_candidate", _build_candidate)
    threads, results, errors = _run_concurrently(2, mc.get_db)
    assert build_started.wait(timeout=2)
    _wait_for_wave_waiters(1)
    release_build.set()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    assert results == []
    assert len(errors) == 2
    assert all(isinstance(error, _CancelledBuild) for error in errors)
    with mc._CONNECTION_CONDITION:
        assert mc._connection_waves == {}

    retry = mc.get_db()
    assert retry is not None
    assert retry["client"] is recovered
    assert build_count == 2


def test_invalidation_fences_and_closes_late_unpublished_candidate(
    monkeypatch,
) -> None:
    old_published = _FakeClient("old-published")
    with mc._CONNECTION_CONDITION:
        mc._mongo_client_real = old_published
    mc.invalidate_connection()
    assert old_published.closed is False

    first_started = threading.Event()
    release_first = threading.Event()
    stale_candidate = _FakeClient("stale-candidate")
    replacement = _FakeClient("replacement")
    connect_count = 0
    count_lock = threading.Lock()

    def _connect(_uri: str, **_kwargs):
        nonlocal connect_count
        with count_lock:
            connect_count += 1
            attempt = connect_count
        if attempt == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
            return stale_candidate
        return replacement

    monkeypatch.setattr(mc, "_try_connect_uri", _connect)
    worker_result: list[object] = []
    worker = threading.Thread(target=lambda: worker_result.append(mc.get_db()))
    worker.start()
    assert first_started.wait(timeout=2)

    mc.invalidate_connection()
    current = mc.get_db()
    assert current is not None
    assert current["client"] is replacement
    release_first.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert worker_result[0]["client"] is replacement
    assert stale_candidate.closed is True
    assert replacement.closed is False
    assert mc._mongo_client_real is replacement


def test_atomic_degrade_invalidate_preserves_route_intent_across_late_builder(
    monkeypatch,
) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    stale_primary = _FakeClient("stale-primary")
    alternate = _FakeClient("alternate")
    primary_uri = mc.MONGO_URI
    attempted_uris: list[str] = []

    monkeypatch.setattr(
        mc,
        "MONGO_SSH_TUNNEL_FALLBACK_ENDPOINTS",
        ("127.0.0.1:27018",),
    )
    monkeypatch.setattr(mc, "MONGO_SSH_TUNNEL_EXPECTED_REPLICA_SET", None)

    def _connect(uri: str, **_kwargs):
        attempted_uris.append(uri)
        if uri == primary_uri:
            first_started.set()
            assert release_first.wait(timeout=2)
            return stale_primary
        assert "127.0.0.1:27018" in uri
        return alternate

    monkeypatch.setattr(mc, "_try_connect_uri", _connect)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(mc.get_db()))
    worker.start()
    assert first_started.wait(timeout=2)

    mc.invalidate_connection(prefer_alternate_reason="simulated route failure")
    assert mc.get_mongo_fallback_policy_state()["preferred_fallback_kind"] == (
        "ssh_tunnel"
    )
    release_first.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert result[0]["client"] is alternate
    assert attempted_uris[0] == primary_uri
    assert "127.0.0.1:27018" in attempted_uris[1]
    assert len(attempted_uris) == 2
    assert stale_primary.closed is True
    assert alternate.closed is False
    policy = mc.get_mongo_fallback_policy_state()
    assert policy["preferred_fallback_kind"] == "ssh_tunnel"
    assert policy["active_fallback_kind"] == "ssh_tunnel"


def test_fallback_route_metadata_is_published_with_its_client(monkeypatch) -> None:
    build_started = threading.Event()
    release_build = threading.Event()
    fallback = _FakeClient("dns-fallback")
    fallback_uri = "mongodb://direct.example:27017/?tls=true"

    def _build_candidate():
        build_started.set()
        assert release_build.wait(timeout=2)
        return (
            mc._MongoConnectionCandidate(
                client=fallback,
                uri=fallback_uri,
                fallback_kind="dns",
                preference_reason="test fallback",
            ),
            False,
        )

    monkeypatch.setattr(mc, "_build_real_connection_candidate", _build_candidate)
    result: list[object] = []
    worker = threading.Thread(target=lambda: result.append(mc.get_db()))
    worker.start()
    assert build_started.wait(timeout=2)

    assert mc._mongo_client_real is None
    assert mc.get_effective_mongo_uri() == mc.MONGO_URI
    assert mc.is_using_fallback_uri() is False
    release_build.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert result[0]["client"] is fallback
    assert mc._mongo_client_real is fallback
    assert mc.get_effective_mongo_uri() == fallback_uri
    assert mc.is_using_fallback_uri() is True
    assert mc.get_mongo_fallback_policy_state()["active_fallback_kind"] == "dns"


def test_late_health_failure_cannot_erase_newer_client_and_probes_coalesce(
    monkeypatch,
) -> None:
    ping_started = threading.Event()
    release_ping = threading.Event()
    stale = _FakeClient(
        "stale",
        _FakeAdmin(
            ping_started=ping_started,
            release_ping=release_ping,
            fail_ping=True,
        ),
    )
    replacement = _FakeClient("replacement")
    with mc._CONNECTION_CONDITION:
        mc._mongo_client_real = stale
        mc._last_auto_recovery_check_at = 0.0
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY", "1")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "0.001")
    monkeypatch.setattr(mc, "_try_connect_uri", lambda _uri, **_kwargs: replacement)

    probing_result: list[object] = []
    probing = threading.Thread(target=lambda: probing_result.append(mc.get_db()))
    probing.start()
    assert ping_started.wait(timeout=2)

    coalesced = mc.get_db()
    assert coalesced is not None
    assert coalesced["client"] is stale
    assert stale.admin.ping_count == 1

    mc.invalidate_connection()
    current = mc.get_db()
    assert current is not None
    assert current["client"] is replacement
    release_ping.set()
    probing.join(timeout=3)

    assert not probing.is_alive()
    assert probing_result[0]["client"] is replacement
    assert mc._mongo_client_real is replacement
    assert replacement.closed is False


def test_ordinary_healthy_get_db_does_not_enter_connection_condition(
    monkeypatch,
) -> None:
    healthy = _FakeClient("healthy")
    mc._mongo_client_real = healthy
    mc._last_auto_recovery_check_at = time.monotonic()
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY", "1")
    monkeypatch.setenv("VON_MONGO_AUTO_RECOVERY_CHECK_INTERVAL_SECONDS", "3600")

    class _UnexpectedCondition:
        def __enter__(self):
            raise AssertionError(
                "healthy fast path must not enter connection condition"
            )

        def __exit__(self, *_args):
            return False

    with monkeypatch.context() as context:
        context.setattr(mc, "_CONNECTION_CONDITION", _UnexpectedCondition())
        db = mc.get_db()

    assert db is not None
    assert db["client"] is healthy
    assert healthy.admin.ping_count == 0
