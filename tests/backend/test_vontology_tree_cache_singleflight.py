from __future__ import annotations

from collections.abc import Iterator
import threading
import time
from typing import Any

import pytest
from flask import Flask

from src.backend.security import access_control
from src.backend.server.routes import vontology_routes as routes
from src.backend.services import concept_service
from src.backend.vontology import utils_vontology


@pytest.fixture
def app(monkeypatch) -> Iterator[Flask]:
    flask_app = Flask(__name__)
    flask_app.secret_key = "tree-singleflight-test"
    flask_app.config["TESTING"] = True
    flask_app.register_blueprint(routes.vontology_bp, url_prefix="/api/vontology")
    monkeypatch.setattr(routes, "record_tree_build_performance", lambda _secs: None)
    monkeypatch.setattr(routes.tracemalloc, "is_tracing", lambda: False)
    with routes._TREE_CACHE_LOCK:
        routes._TREE_CACHE.clear()
        routes._TREE_BUILDS_IN_FLIGHT.clear()
        routes._TREE_BUILD_PROGRESS.clear()
    yield flask_app
    with routes._TREE_CACHE_LOCK:
        routes._TREE_CACHE.clear()
        routes._TREE_BUILDS_IN_FLIGHT.clear()
        routes._TREE_BUILD_PROGRESS.clear()


def _set_actor(client, user: str | None, organisation: str | None) -> None:
    with client.session_transaction() as flask_session:
        flask_session.clear()
        if user is not None:
            flask_session["user_concept_id"] = user
        if organisation is not None:
            flask_session["organisation_concept_id"] = organisation


def _poll(client, job_id: str) -> dict[str, Any]:
    response = client.get(f"/api/vontology/tree_progress/{job_id}")
    assert response.status_code == 200
    return response.get_json()


def _await_completed(client, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        payload = _poll(client, job_id)
        if payload["done"]:
            return payload
        time.sleep(0.01)
    raise AssertionError("tree job did not complete")


def test_sync_and_async_share_anonymous_global_cache_despite_residual_org(
    app: Flask,
    monkeypatch,
) -> None:
    observations: list[tuple[str | None, str | None, bool]] = []

    def build(_root: str) -> dict[str, Any]:
        observations.append(
            (
                access_control.get_effective_user_concept_id(),
                access_control.get_effective_organisation_concept_id(),
                access_control.should_enforce_access_control(),
            )
        )
        return {"tree": [{"id": "#V#global", "children": []}]}

    monkeypatch.setattr(routes, "get_vontology_tree", build)
    client = app.test_client()
    _set_actor(client, None, "#V#stale_org")

    synchronous = client.get("/api/vontology/tree")
    created = client.post("/api/vontology/tree_async")
    completed = _poll(client, created.get_json()["job_id"])

    assert synchronous.status_code == 200
    assert created.status_code == 202
    assert synchronous.get_json()["_cache"]["hit"] is False
    assert completed["result"]["_cache"]["hit"] is True
    assert completed["progress"] == 100
    assert completed["done"] is True
    assert observations == [(None, None, True)]
    with routes._TREE_CACHE_LOCK:
        assert list(routes._TREE_CACHE) == [(None, None, "Thing")]


def test_tree_cache_lookup_prunes_expired_records_for_other_scopes(
    app: Flask,
    monkeypatch,
) -> None:
    del app
    monkeypatch.setattr(routes, "_TREE_CACHE_TTL_SECONDS", 60)
    current_key = ("#V#current", "#V#org", "Thing")
    stale_key = ("#V#stale", "#V#org", "Thing")
    with routes._TREE_CACHE_LOCK:
        routes._TREE_CACHE[current_key] = (100.0, {"tree": []}, 1.0, 0.0, 0)
        routes._TREE_CACHE[stale_key] = (1.0, {"tree": []}, 1.0, 0.0, 0)
        record = routes._fresh_tree_cache_record_locked(current_key, now=120.0)

    assert record is not None
    with routes._TREE_CACHE_LOCK:
        assert stale_key not in routes._TREE_CACHE


def test_overlapping_async_and_sync_requests_share_one_physical_build(
    app: Flask,
    monkeypatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    build_calls = 0

    def build(_root: str) -> dict[str, Any]:
        nonlocal build_calls
        build_calls += 1
        entered.set()
        assert release.wait(timeout=3.0)
        return {"tree": [{"id": "#V#thing", "children": []}]}

    monkeypatch.setattr(routes, "get_vontology_tree", build)
    first_client = app.test_client()
    first = first_client.post("/api/vontology/tree_async")
    assert first.status_code == 202
    assert entered.wait(timeout=1.0)
    job_id = first.get_json()["job_id"]

    second = first_client.post("/api/vontology/tree_async")
    assert second.status_code == 202
    assert second.get_json()["job_id"] == job_id
    running = _poll(first_client, job_id)
    assert running["done"] is False
    assert 0 <= running["progress"] < 100

    sync_result: dict[str, Any] = {}

    def sync_request() -> None:
        with app.test_client() as sync_client:
            response = sync_client.get("/api/vontology/tree")
            sync_result["status"] = response.status_code
            sync_result["payload"] = response.get_json()

    sync_thread = threading.Thread(target=sync_request)
    sync_thread.start()
    release.set()
    sync_thread.join(timeout=3.0)

    assert sync_thread.is_alive() is False
    assert sync_result["status"] == 200
    assert sync_result["payload"]["_cache"]["hit"] is False
    assert build_calls == 1
    completed = _poll(first_client, job_id)
    assert completed["done"] is True
    assert completed["progress"] == 100


def test_tree_cache_is_partitioned_by_exact_user_and_organisation(
    app: Flask,
    monkeypatch,
) -> None:
    observations: list[tuple[str | None, str | None]] = []

    def build(_root: str) -> dict[str, Any]:
        actor = (
            access_control.get_effective_user_concept_id(),
            access_control.get_effective_organisation_concept_id(),
        )
        observations.append(actor)
        return {"tree": [{"id": "|".join(value or "none" for value in actor)}]}

    monkeypatch.setattr(routes, "get_vontology_tree", build)
    scopes = [
        ("#V#alice", "#V#org_a"),
        ("#V#alice", "#V#org_b"),
        ("#V#bob", "#V#org_a"),
        ("#V#a|org:#V#b", "#V#c"),
        ("#V#a", "#V#b|org:#V#c"),
    ]
    clients = []
    for user, organisation in scopes:
        client = app.test_client()
        _set_actor(client, user, organisation)
        clients.append(client)
        assert client.get("/api/vontology/tree").status_code == 200

    assert observations == scopes
    for client in clients:
        response = client.get("/api/vontology/tree")
        assert response.status_code == 200
        assert response.get_json()["_cache"]["hit"] is True
    assert observations == scopes


def test_invalidation_fences_an_older_builder_from_overwriting_new_cache(
    app: Flask,
    monkeypatch,
) -> None:
    del app
    old_entered = threading.Event()
    release_old = threading.Event()
    call_count = 0

    def build(_root: str) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            old_entered.set()
            assert release_old.wait(timeout=3.0)
            return {"tree": [{"id": "old"}]}
        return {"tree": [{"id": "new"}]}

    monkeypatch.setattr(routes, "get_vontology_tree", build)
    scope = (None, None)
    with routes._TREE_CACHE_LOCK:
        old_state, old_owner = routes._claim_tree_build_locked(scope)
    assert old_owner is True
    old_thread = threading.Thread(target=routes._execute_tree_build, args=(old_state,))
    old_thread.start()
    assert old_entered.wait(timeout=1.0)

    monkeypatch.setattr(
        "src.backend.services.vontology_concept_stats_service.invalidate_vontology_concept_stats_cache",
        lambda **_kwargs: None,
    )
    utils_vontology.invalidate_vontology_caches(["#V#changed"], "tree-test")
    with routes._TREE_CACHE_LOCK:
        new_state, new_owner = routes._claim_tree_build_locked(scope)
    assert new_owner is True
    routes._execute_tree_build(new_state)
    release_old.set()
    old_thread.join(timeout=3.0)

    assert old_thread.is_alive() is False
    assert old_state.result == {"tree": [{"id": "old"}]}
    assert new_state.result == {"tree": [{"id": "new"}]}
    with routes._TREE_CACHE_LOCK:
        assert routes._TREE_CACHE[routes._tree_cache_key(scope)][1] == new_state.result
        assert routes._TREE_BUILDS_IN_FLIGHT == {}


def test_async_refresh_bypasses_completed_cache(app: Flask, monkeypatch) -> None:
    versions = iter(["first", "refreshed"])
    build_calls = 0

    def build(_root: str) -> dict[str, Any]:
        nonlocal build_calls
        build_calls += 1
        return {"tree": [{"id": next(versions), "children": []}]}

    monkeypatch.setattr(routes, "get_vontology_tree", build)
    client = app.test_client()
    assert client.get("/api/vontology/tree").status_code == 200

    cached_job = client.post("/api/vontology/tree_async").get_json()["job_id"]
    assert _poll(client, cached_job)["result"]["tree"][0]["id"] == "first"
    assert build_calls == 1

    refreshed = client.post("/api/vontology/tree_async?refresh=1")
    assert refreshed.status_code == 202
    refreshed_payload = _await_completed(client, refreshed.get_json()["job_id"])
    assert refreshed_payload["result"]["tree"][0]["id"] == "refreshed"
    assert build_calls == 2


def test_snapshot_failure_releases_waiters_and_allows_retry(
    app: Flask,
    monkeypatch,
) -> None:
    client = app.test_client()
    monkeypatch.setattr(routes.tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(
        routes.tracemalloc,
        "take_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )

    failed = client.get("/api/vontology/tree")

    assert failed.status_code == 500
    with routes._TREE_CACHE_LOCK:
        assert routes._TREE_BUILDS_IN_FLIGHT == {}

    monkeypatch.setattr(routes.tracemalloc, "is_tracing", lambda: False)
    monkeypatch.setattr(
        routes,
        "get_vontology_tree",
        lambda _root: {"tree": [{"id": "retry", "children": []}]},
    )
    assert client.get("/api/vontology/tree").status_code == 200


def test_progress_event_construction_interrupt_releases_waiters(
    app: Flask,
    monkeypatch,
) -> None:
    del app
    with routes._TREE_CACHE_LOCK:
        state, is_owner = routes._claim_tree_build_locked((None, None))
    assert is_owner is True
    monkeypatch.setattr(
        routes.threading,
        "Event",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        routes._execute_tree_build(state)

    assert state.completion.is_set()
    assert state.error == "KeyboardInterrupt"
    with routes._TREE_CACHE_LOCK:
        assert routes._TREE_BUILDS_IN_FLIGHT == {}


def test_progress_stop_interrupt_releases_waiters(
    app: Flask,
    monkeypatch,
) -> None:
    del app

    class InterruptingEvent:
        def set(self) -> None:
            raise KeyboardInterrupt

    class NoOpThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.5

    with routes._TREE_CACHE_LOCK:
        state, is_owner = routes._claim_tree_build_locked((None, None))
    assert is_owner is True
    monkeypatch.setattr(routes.threading, "Event", InterruptingEvent)
    monkeypatch.setattr(routes.threading, "Thread", NoOpThread)
    monkeypatch.setattr(
        routes,
        "get_vontology_tree",
        lambda _root: {"tree": [{"id": "discarded", "children": []}]},
    )

    with pytest.raises(KeyboardInterrupt):
        routes._execute_tree_build(state)

    assert state.completion.is_set()
    assert state.error == "KeyboardInterrupt"
    with routes._TREE_CACHE_LOCK:
        assert routes._TREE_BUILDS_IN_FLIGHT == {}
        assert routes._TREE_CACHE == {}


def test_progress_cleanup_interrupt_releases_waiters_and_allows_retry(
    app: Flask,
    monkeypatch,
) -> None:
    del app

    class InterruptingJoinThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def join(self, *, timeout: float) -> None:
            assert timeout == 0.5
            raise KeyboardInterrupt

    monkeypatch.setattr(routes.threading, "Thread", InterruptingJoinThread)
    monkeypatch.setattr(
        routes,
        "get_vontology_tree",
        lambda _root: {"tree": [{"id": "discarded", "children": []}]},
    )
    with routes._TREE_CACHE_LOCK:
        state, is_owner = routes._claim_tree_build_locked((None, None))
    assert is_owner is True

    with pytest.raises(KeyboardInterrupt):
        routes._execute_tree_build(state)

    assert state.completion.is_set()
    assert state.error == "KeyboardInterrupt"
    with routes._TREE_CACHE_LOCK:
        assert routes._TREE_BUILDS_IN_FLIGHT == {}
        assert routes._TREE_CACHE == {}


def test_async_control_flow_start_failure_cleans_all_state(
    app: Flask,
    monkeypatch,
) -> None:
    class InterruptingThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(routes.threading, "Thread", InterruptingThread)

    with pytest.raises(KeyboardInterrupt):
        app.test_client().post("/api/vontology/tree_async")

    with routes._TREE_CACHE_LOCK:
        assert routes._TREE_BUILDS_IN_FLIGHT == {}
        assert routes._TREE_BUILD_PROGRESS == {}


def test_canonical_concept_mutation_invalidation_fences_tree_builds(
    app: Flask,
    monkeypatch,
) -> None:
    del app
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(routes, "_invalidate_tree_cache", invalidate)
    monkeypatch.setattr(concept_service, "invalidate_phrase_cache", lambda: None)
    monkeypatch.setattr(
        concept_service,
        "get_workflow_discovery_cache_invalidation_enabled",
        lambda **_kwargs: False,
    )

    concept_service._invalidate_concept_mutation_caches()

    assert invalidations == 1
