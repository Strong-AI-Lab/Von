from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from pymongo.errors import ConnectionFailure

from src.backend.db import connection_manager as conn_mgr


class _PingDb:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.pings = 0
        self._lock = threading.Lock()

    def command(self, name: str):
        assert name == "ping"
        with self._lock:
            self.pings += 1
        if self.fail:
            raise ConnectionFailure("simulated ping failure")
        return {"ok": 1}


@pytest.fixture(autouse=True)
def _restore_reconnect_state():
    state_snapshot = dict(conn_mgr._state)
    sequence_snapshot = conn_mgr._reconnect_attempt_sequence
    completed_snapshot = conn_mgr._last_completed_reconnect_attempt
    with conn_mgr._reconnect_condition:
        assert conn_mgr._active_reconnect_attempt is None
        conn_mgr._last_completed_reconnect_attempt = None
    yield
    with conn_mgr._reconnect_condition:
        conn_mgr._active_reconnect_attempt = None
        conn_mgr._last_completed_reconnect_attempt = completed_snapshot
        conn_mgr._reconnect_attempt_sequence = sequence_snapshot
        conn_mgr._reconnect_condition.notify_all()
    conn_mgr._state.clear()
    conn_mgr._state.update(state_snapshot)


def _wait_for_active_followers(expected: int) -> None:
    with conn_mgr._reconnect_condition:
        reached = conn_mgr._reconnect_condition.wait_for(
            lambda: conn_mgr._active_reconnect_attempt is not None
            and conn_mgr._active_reconnect_attempt.followers >= expected,
            timeout=5,
        )
    assert reached, f"reconnect wave did not acquire {expected} followers"


def _run_threads(
    calls: list[Callable[[], dict]],
) -> tuple[list[dict], list[BaseException], list[threading.Thread]]:
    results: list[dict] = []
    errors: list[BaseException] = []
    result_lock = threading.Lock()
    ready = threading.Barrier(len(calls) + 1)

    def _worker(call: Callable[[], dict]) -> None:
        ready.wait()
        try:
            result = call()
            with result_lock:
                results.append(result)
        except BaseException as exc:  # noqa: BLE001 - asserted by caller
            with result_lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(call,), daemon=True) for call in calls
    ]
    for thread in threads:
        thread.start()
    ready.wait()
    return results, errors, threads


def _join_threads(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive(), "reconnect test worker did not finish"


def test_concurrent_forced_calls_share_one_successful_wave(monkeypatch) -> None:
    db = _PingDb()
    get_db_entered = threading.Event()
    release_get_db = threading.Event()
    calls = {"invalidate": 0, "get_db": 0}
    counter_lock = threading.Lock()

    def _invalidate(**_kwargs) -> None:
        with counter_lock:
            calls["invalidate"] += 1

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
        get_db_entered.set()
        assert release_get_db.wait(timeout=5)
        return db

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)
    metric_before = conn_mgr._state["reconnect_attempts_total"]

    call_count = 8
    results, errors, threads = _run_threads(
        [lambda: conn_mgr.attempt_reconnect(force=True)] * call_count
    )
    assert get_db_entered.wait(timeout=5)
    _wait_for_active_followers(call_count - 1)
    release_get_db.set()
    _join_threads(threads)

    assert errors == []
    assert len(results) == call_count
    assert calls == {"invalidate": 1, "get_db": 1}
    assert db.pings == 1
    assert conn_mgr._state["reconnect_attempts_total"] == metric_before + 1
    assert all(result["reconnected"] is True for result in results)
    assert all(result["attempts"] == 1 for result in results)


def test_failed_wave_is_shared_but_a_later_call_retries_independently(
    monkeypatch,
) -> None:
    get_db_entered = threading.Event()
    release_get_db = threading.Event()
    success_db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}
    failure_mode = {"enabled": True}
    counter_lock = threading.Lock()

    def _invalidate(**_kwargs) -> None:
        with counter_lock:
            calls["invalidate"] += 1

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
        if failure_mode["enabled"]:
            get_db_entered.set()
            assert release_get_db.wait(timeout=5)
            raise ConnectionFailure("shared failure")
        return success_db

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)

    call_count = 6
    results, errors, threads = _run_threads(
        [lambda: conn_mgr.attempt_reconnect(force=True, max_attempts=1)] * call_count
    )
    assert get_db_entered.wait(timeout=5)
    _wait_for_active_followers(call_count - 1)
    release_get_db.set()
    _join_threads(threads)

    assert errors == []
    assert len(results) == call_count
    assert calls == {"invalidate": 1, "get_db": 1}
    assert all(result["reconnected"] is False for result in results)
    assert all(result["attempts"] == 1 for result in results)

    failure_mode["enabled"] = False
    later = conn_mgr.attempt_reconnect(force=True, max_attempts=1)

    assert later["reconnected"] is True
    assert calls == {"invalidate": 2, "get_db": 2}
    assert success_db.pings == 1


def test_larger_follower_budget_gets_one_follow_up_after_small_wave_fails(
    monkeypatch,
) -> None:
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    db = _PingDb()
    calls = {"get_db": 0}
    counter_lock = threading.Lock()

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
            call_number = calls["get_db"]
        if call_number == 1:
            first_get_db_entered.set()
            assert release_first_get_db.wait(timeout=5)
            raise ConnectionFailure("small wave exhausted")
        return db

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)

    leader_result: list[dict] = []
    follower_result: list[dict] = []
    leader = threading.Thread(
        target=lambda: leader_result.append(
            conn_mgr.attempt_reconnect(force=True, max_attempts=1)
        ),
        daemon=True,
    )
    leader.start()
    assert first_get_db_entered.wait(timeout=5)
    follower = threading.Thread(
        target=lambda: follower_result.append(
            conn_mgr.attempt_reconnect(force=True, max_attempts=3)
        ),
        daemon=True,
    )
    follower.start()
    _wait_for_active_followers(1)
    release_first_get_db.set()
    _join_threads([leader, follower])

    assert leader_result[0]["reconnected"] is False
    assert leader_result[0]["attempts"] == 1
    assert follower_result[0]["reconnected"] is True
    assert follower_result[0]["attempts"] == 2
    assert calls["get_db"] == 2
    assert db.pings == 1


def test_smaller_follower_budget_returns_after_one_shared_failure(monkeypatch) -> None:
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    third_get_db_entered = threading.Event()
    release_third_get_db = threading.Event()
    calls = {"get_db": 0}
    counter_lock = threading.Lock()

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
            call_number = calls["get_db"]
        if call_number == 1:
            first_get_db_entered.set()
            assert release_first_get_db.wait(timeout=5)
        elif call_number == 3:
            third_get_db_entered.set()
            assert release_third_get_db.wait(timeout=5)
        raise ConnectionFailure(f"failure {call_number}")

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", lambda **_kwargs: None)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)
    monkeypatch.setattr(conn_mgr.time, "sleep", lambda _delay: None)

    long_result: list[dict] = []
    short_result: list[dict] = []
    leader = threading.Thread(
        target=lambda: long_result.append(
            conn_mgr.attempt_reconnect(force=True, max_attempts=5)
        ),
        daemon=True,
    )
    leader.start()
    assert first_get_db_entered.wait(timeout=5)
    follower = threading.Thread(
        target=lambda: short_result.append(
            conn_mgr.attempt_reconnect(force=True, max_attempts=1)
        ),
        daemon=True,
    )
    follower.start()
    _wait_for_active_followers(1)
    release_first_get_db.set()

    assert third_get_db_entered.wait(timeout=5)
    follower.join(timeout=5)
    assert not follower.is_alive()
    assert short_result[0]["reconnected"] is False
    assert short_result[0]["attempts"] == 1
    assert calls["get_db"] == 3

    release_third_get_db.set()
    _join_threads([leader])
    assert long_result[0]["reconnected"] is False
    assert long_result[0]["attempts"] == 5
    assert calls["get_db"] == 5


def test_staggered_retry_callers_share_exactly_two_physical_attempts(
    monkeypatch,
) -> None:
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    second_get_db_entered = threading.Event()
    release_second_get_db = threading.Event()
    calls = {"invalidate": 0, "get_db": 0}
    counter_lock = threading.Lock()

    def _invalidate(**_kwargs) -> None:
        with counter_lock:
            calls["invalidate"] += 1

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
            call_number = calls["get_db"]
        if call_number == 1:
            first_get_db_entered.set()
            assert release_first_get_db.wait(timeout=5), "first get_db not released"
        elif call_number == 2:
            second_get_db_entered.set()
            assert release_second_get_db.wait(timeout=5), "second get_db not released"
        raise ConnectionFailure(f"failure {call_number}")

    backoff_releases = [threading.Event() for _ in range(6)]
    backoff_started = threading.Condition()
    backoff_call_count = {"value": 0}

    def _coordinated_sleep(_delay: float) -> None:
        with backoff_started:
            slot = backoff_call_count["value"]
            backoff_call_count["value"] += 1
            backoff_started.notify_all()
        assert backoff_releases[slot].wait(timeout=5), f"extra backoff slot {slot}"

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)
    monkeypatch.setattr(conn_mgr.time, "sleep", _coordinated_sleep)

    caller_count = 6
    results, errors, threads = _run_threads(
        [lambda: conn_mgr.attempt_reconnect(force=True, max_attempts=2)] * caller_count
    )
    assert first_get_db_entered.wait(timeout=5)
    _wait_for_active_followers(caller_count - 1)
    release_first_get_db.set()

    with backoff_started:
        assert backoff_started.wait_for(
            lambda: backoff_call_count["value"] == 1,
            timeout=5,
        )
    backoff_releases[0].set()
    assert second_get_db_entered.wait(timeout=5)
    # Serially releasing the remaining per-caller controls would have produced
    # one retry per caller before coordinated backoff was introduced. They are
    # intentionally left unset: only the one attempt leader may sleep.
    release_second_get_db.set()
    _join_threads(threads)

    assert errors == []
    assert len(results) == caller_count
    assert all(result["reconnected"] is False for result in results)
    assert all(result["attempts"] == 2 for result in results)
    assert calls == {"invalidate": 2, "get_db": 2}
    assert backoff_call_count["value"] == 1


def test_fresh_caller_racing_retry_joins_the_newer_attempt(monkeypatch) -> None:
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    retry_backoff_entered = threading.Event()
    release_retry_backoff = threading.Event()
    db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}

    def _invalidate(**_kwargs) -> None:
        calls["invalidate"] += 1

    def _get_db():
        calls["get_db"] += 1
        if calls["get_db"] == 1:
            first_get_db_entered.set()
            assert release_first_get_db.wait(timeout=5)
            raise ConnectionFailure("first failure")
        return db

    def _backoff(_delay: float) -> None:
        retry_backoff_entered.set()
        assert release_retry_backoff.wait(timeout=5)

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)
    monkeypatch.setattr(conn_mgr.time, "sleep", _backoff)

    retrying: list[dict] = []
    fresh: list[dict] = []
    first = threading.Thread(
        target=lambda: retrying.append(
            conn_mgr.attempt_reconnect(force=True, max_attempts=2)
        ),
        daemon=True,
    )
    first.start()
    assert first_get_db_entered.wait(timeout=5)
    release_first_get_db.set()
    assert retry_backoff_entered.wait(timeout=5)

    second = threading.Thread(
        target=lambda: fresh.append(
            conn_mgr.attempt_reconnect(force=True, max_attempts=1)
        ),
        daemon=True,
    )
    second.start()
    _wait_for_active_followers(1)
    release_retry_backoff.set()
    _join_threads([first, second])

    assert retrying[0]["reconnected"] is True
    assert retrying[0]["attempts"] == 2
    assert fresh[0]["reconnected"] is True
    assert fresh[0]["attempts"] == 1
    assert calls == {"invalidate": 2, "get_db": 2}
    assert db.pings == 1


def test_nonforced_caller_reuses_reconnect_completed_during_health_preflight(
    monkeypatch,
) -> None:
    health_entered = threading.Event()
    release_health = threading.Event()
    db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}

    def _health_summary():
        health_entered.set()
        assert release_health.wait(timeout=5)
        return {"connected": False, "route_degraded": False}

    def _invalidate(**_kwargs) -> None:
        calls["invalidate"] += 1

    def _get_db():
        calls["get_db"] += 1
        return db

    monkeypatch.setattr(conn_mgr, "health_summary", _health_summary)
    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)

    waiting_result: list[dict] = []
    waiting = threading.Thread(
        target=lambda: waiting_result.append(
            conn_mgr.attempt_reconnect(force=False, max_attempts=1)
        ),
        daemon=True,
    )
    waiting.start()
    assert health_entered.wait(timeout=5)

    completed = conn_mgr.attempt_reconnect(force=True, max_attempts=1)
    release_health.set()
    _join_threads([waiting])

    assert completed["reconnected"] is True
    assert waiting_result[0]["reconnected"] is True
    assert waiting_result[0]["attempts"] == 1
    assert calls == {"invalidate": 1, "get_db": 1}
    assert db.pings == 1


def test_backoff_exception_releases_all_attempt_followers(monkeypatch) -> None:
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    backoff_entered = threading.Event()
    release_backoff = threading.Event()

    class _BackoffAbort(BaseException):
        pass

    def _get_db():
        first_get_db_entered.set()
        assert release_first_get_db.wait(timeout=5)
        raise ConnectionFailure("first failure")

    def _abort_backoff(_delay: float) -> None:
        backoff_entered.set()
        assert release_backoff.wait(timeout=5)
        raise _BackoffAbort("cancelled backoff")

    monkeypatch.setattr(
        conn_mgr._mc,
        "invalidate_connection",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)
    monkeypatch.setattr(conn_mgr.time, "sleep", _abort_backoff)

    caller_count = 5
    _results, errors, threads = _run_threads(
        [lambda: conn_mgr.attempt_reconnect(force=True, max_attempts=2)] * caller_count
    )
    assert first_get_db_entered.wait(timeout=5)
    _wait_for_active_followers(caller_count - 1)
    release_first_get_db.set()
    assert backoff_entered.wait(timeout=5)
    _wait_for_active_followers(caller_count - 1)
    release_backoff.set()
    _join_threads(threads)

    assert len(errors) == caller_count
    assert all(isinstance(error, _BackoffAbort) for error in errors)
    with conn_mgr._reconnect_condition:
        assert conn_mgr._active_reconnect_attempt is None


def test_late_force_follows_a_healthy_nonforced_skip(monkeypatch) -> None:
    health_entered = threading.Event()
    release_health = threading.Event()
    db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}

    def _health_summary():
        health_entered.set()
        assert release_health.wait(timeout=5)
        return {"connected": True, "route_degraded": False}

    def _invalidate(**_kwargs) -> None:
        calls["invalidate"] += 1

    def _get_db():
        calls["get_db"] += 1
        return db

    monkeypatch.setattr(conn_mgr, "health_summary", _health_summary)
    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)

    skipped: list[dict] = []
    forced: list[dict] = []
    first = threading.Thread(
        target=lambda: skipped.append(conn_mgr.attempt_reconnect(force=False)),
        daemon=True,
    )
    first.start()
    assert health_entered.wait(timeout=5)
    second = threading.Thread(
        target=lambda: forced.append(conn_mgr.attempt_reconnect(force=True)),
        daemon=True,
    )
    second.start()
    release_health.set()
    _join_threads([first, second])

    assert skipped[0]["skipped"] is True
    assert forced[0]["reconnected"] is True
    assert calls == {"invalidate": 1, "get_db": 1}
    assert db.pings == 1


def test_late_force_joins_an_active_nonforced_reconnect(monkeypatch) -> None:
    health_checked = threading.Event()
    release_health = threading.Event()
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}
    counter_lock = threading.Lock()

    def _health_summary():
        health_checked.set()
        assert release_health.wait(timeout=5)
        return {"connected": False, "route_degraded": False}

    def _invalidate(**_kwargs) -> None:
        with counter_lock:
            calls["invalidate"] += 1

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
            call_number = calls["get_db"]
        if call_number == 1:
            first_get_db_entered.set()
            assert release_first_get_db.wait(timeout=5)
        return db

    monkeypatch.setattr(conn_mgr, "health_summary", _health_summary)
    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)

    ordinary: list[dict] = []
    forced: list[dict] = []
    first = threading.Thread(
        target=lambda: ordinary.append(conn_mgr.attempt_reconnect(force=False)),
        daemon=True,
    )
    first.start()
    assert health_checked.wait(timeout=5)
    release_health.set()
    assert first_get_db_entered.wait(timeout=5)

    second = threading.Thread(
        target=lambda: forced.append(conn_mgr.attempt_reconnect(force=True)),
        daemon=True,
    )
    second.start()
    _wait_for_active_followers(1)
    release_first_get_db.set()
    _join_threads([first, second])

    assert ordinary[0]["reconnected"] is True
    assert forced[0]["reconnected"] is True
    assert forced[0]["attempts"] == 1
    assert calls == {"invalidate": 1, "get_db": 1}
    assert db.pings == 1


def test_late_route_degraded_callers_share_one_follow_up(monkeypatch) -> None:
    first_get_db_entered = threading.Event()
    release_first_get_db = threading.Event()
    second_get_db_entered = threading.Event()
    release_second_get_db = threading.Event()
    db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}
    preferences: list[str] = []
    counter_lock = threading.Lock()

    def _invalidate(*, prefer_alternate_reason=None) -> None:
        with counter_lock:
            calls["invalidate"] += 1
        if prefer_alternate_reason is not None:
            preferences.append(prefer_alternate_reason)

    def _get_db():
        with counter_lock:
            calls["get_db"] += 1
            call_number = calls["get_db"]
        if call_number == 1:
            first_get_db_entered.set()
            assert release_first_get_db.wait(timeout=5)
        elif call_number == 2:
            second_get_db_entered.set()
            assert release_second_get_db.wait(timeout=5)
        return db

    monkeypatch.setattr(conn_mgr._mc, "invalidate_connection", _invalidate)
    monkeypatch.setattr(conn_mgr._mc, "get_db", _get_db)

    ordinary: list[dict] = []
    leader = threading.Thread(
        target=lambda: ordinary.append(conn_mgr.attempt_reconnect(force=True)),
        daemon=True,
    )
    leader.start()
    assert first_get_db_entered.wait(timeout=5)

    degraded_count = 5
    degraded_results, errors, followers = _run_threads(
        [
            lambda: conn_mgr.attempt_reconnect(
                route_degraded=True,
                degradation_reason="operation route failed",
            )
        ]
        * degraded_count
    )
    _wait_for_active_followers(degraded_count)
    release_first_get_db.set()
    assert second_get_db_entered.wait(timeout=5)
    _wait_for_active_followers(degraded_count - 1)
    release_second_get_db.set()
    _join_threads([leader, *followers])

    assert errors == []
    assert ordinary[0]["reconnected"] is True
    assert len(degraded_results) == degraded_count
    assert all(result["reconnected"] is True for result in degraded_results)
    assert calls == {"invalidate": 2, "get_db": 2}
    assert db.pings == 2
    assert preferences == ["operation route failed"]


def test_route_degraded_request_does_not_use_healthy_skip(monkeypatch) -> None:
    db = _PingDb()
    calls = {"invalidate": 0, "get_db": 0}
    preferences: list[str] = []

    def _unexpected_health_summary():
        raise AssertionError("route-degraded recovery must not preflight-skip")

    monkeypatch.setattr(conn_mgr, "health_summary", _unexpected_health_summary)
    monkeypatch.setattr(
        conn_mgr._mc,
        "invalidate_connection",
        lambda *, prefer_alternate_reason=None: (
            calls.__setitem__("invalidate", calls["invalidate"] + 1),
            (
                preferences.append(prefer_alternate_reason)
                if prefer_alternate_reason is not None
                else None
            ),
        ),
    )
    monkeypatch.setattr(
        conn_mgr._mc,
        "get_db",
        lambda: calls.__setitem__("get_db", calls["get_db"] + 1) or db,
    )
    result = conn_mgr.attempt_reconnect(
        route_degraded=True,
        degradation_reason="known degraded route",
    )

    assert result["reconnected"] is True
    assert calls == {"invalidate": 1, "get_db": 1}
    assert db.pings == 1
    assert preferences == ["known degraded route"]


def test_final_failure_does_not_probe_beyond_wave_retry_budget(monkeypatch) -> None:
    db = _PingDb(fail=True)
    calls = {"invalidate": 0, "get_db": 0}

    def _unexpected_health_summary():
        raise AssertionError("final failure must not issue a health probe")

    monkeypatch.setattr(conn_mgr, "health_summary", _unexpected_health_summary)
    monkeypatch.setattr(
        conn_mgr._mc,
        "invalidate_connection",
        lambda **_kwargs: calls.__setitem__("invalidate", calls["invalidate"] + 1),
    )
    monkeypatch.setattr(
        conn_mgr._mc,
        "get_db",
        lambda: calls.__setitem__("get_db", calls["get_db"] + 1) or db,
    )
    monkeypatch.setattr(conn_mgr.time, "sleep", lambda _delay: None)

    result = conn_mgr.attempt_reconnect(force=True, max_attempts=3)

    assert result["reconnected"] is False
    assert result["attempts"] == 3
    assert result["connected"] is False
    assert result["health_error_type"] == "ConnectionFailure"
    assert calls == {"invalidate": 3, "get_db": 3}
    assert db.pings == 3
