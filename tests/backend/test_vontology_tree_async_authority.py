from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from flask import Flask

from src.backend.security import access_control
from src.backend.server.routes import vontology_routes as routes


class _DeferredThread:
    created: ClassVar[list[_DeferredThread]] = []

    def __init__(
        self,
        *,
        target: Callable[..., Any],
        args: tuple[Any, ...] = (),
        daemon: bool | None = None,
        **_kwargs: Any,
    ) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.joined = False
        self.__class__.created.append(self)

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.joined = True

    def run(self) -> Any:
        return self.target(*self.args)


class _FailingThread:
    def __init__(self, **_kwargs: Any) -> None:
        raise RuntimeError("thread construction failed")


class _ConceptCollection:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self._documents = {document["concept_id"]: document for document in documents}

    def find_one(self, query: dict[str, Any], projection: dict | None = None):
        del projection
        return self._documents.get(query.get("concept_id"))


@pytest.fixture(autouse=True)
def _clear_tree_job_state():
    _DeferredThread.created.clear()
    with routes._TREE_BUILD_PROGRESS_LOCK:
        routes._TREE_BUILD_PROGRESS.clear()
        routes._TREE_CACHE.clear()
        routes._TREE_BUILDS_IN_FLIGHT.clear()
    yield
    with routes._TREE_BUILD_PROGRESS_LOCK:
        routes._TREE_BUILD_PROGRESS.clear()
        routes._TREE_CACHE.clear()
        routes._TREE_BUILDS_IN_FLIGHT.clear()


@pytest.fixture
def app_client(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "tree-authority-test"
    app.config["TESTING"] = True
    app.register_blueprint(routes.vontology_bp, url_prefix="/api/vontology")
    monkeypatch.setattr(routes.threading, "Thread", _DeferredThread)
    return app, app.test_client()


def _set_actor(
    client,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
) -> None:
    with client.session_transaction() as flask_session:
        flask_session.clear()
        if user_concept_id is not None:
            flask_session["user_concept_id"] = user_concept_id
        if organisation_concept_id is not None:
            flask_session["organisation_concept_id"] = organisation_concept_id


def _install_visible_tree(
    monkeypatch,
) -> list[tuple[str, str | None, str | None, bool]]:
    documents = [
        {"concept_id": "#V#global", "relationships": {}},
        {
            "concept_id": "#V#alice_private",
            "relationships": {"specific_to_user": ["#V#alice"]},
        },
        {
            "concept_id": "#V#org_a_private",
            "relationships": {"specific_to_org": ["#V#org_a"]},
        },
        {
            "concept_id": "#V#bob_private",
            "relationships": {"specific_to_user": ["#V#bob"]},
        },
        {
            "concept_id": "#V#org_b_private",
            "relationships": {"specific_to_org": ["#V#org_b"]},
        },
    ]
    collection = _ConceptCollection(documents)
    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: collection,
    )
    observations: list[tuple[str, str | None, str | None, bool]] = []

    def _visible_concept_ids() -> list[str]:
        return [
            document["concept_id"]
            for document in documents
            if access_control.can_access_concept(document["concept_id"])
        ]

    def _find(_query: dict, _projection: dict):
        observations.append(
            (
                "progress_scan",
                access_control.get_effective_user_concept_id(),
                access_control.get_effective_organisation_concept_id(),
                access_control.should_enforce_access_control(),
            )
        )
        return [{"concept_id": concept_id} for concept_id in _visible_concept_ids()]

    def _tree(_root: str):
        observations.append(
            (
                "tree_build",
                access_control.get_effective_user_concept_id(),
                access_control.get_effective_organisation_concept_id(),
                access_control.should_enforce_access_control(),
            )
        )
        return {"tree": [{"id": concept_id} for concept_id in _visible_concept_ids()]}

    monkeypatch.setattr(routes.ConceptsRepository, "find", _find)
    monkeypatch.setattr(routes, "get_vontology_tree", _tree)
    monkeypatch.setattr(routes, "record_tree_build_performance", lambda _secs: None)
    return observations


def _run_route_worker() -> None:
    assert _DeferredThread.created
    route_thread = _DeferredThread.created[0]
    assert route_thread.target is routes._build_tree_job
    assert route_thread.started is True
    route_thread.run()


def _tree_ids(payload: dict[str, Any]) -> set[str]:
    return {node["id"] for node in payload["result"]["tree"]}


def test_authenticated_tree_worker_reapplies_exact_actor_scope(
    app_client,
    monkeypatch,
) -> None:
    _, client = app_client
    observations = _install_visible_tree(monkeypatch)
    _set_actor(client, "#V#alice", "#V#org_a")

    created = client.post("/api/vontology/tree_async")

    assert created.status_code == 202
    job_id = created.get_json()["job_id"]
    _run_route_worker()
    completed = client.get(f"/api/vontology/tree_progress/{job_id}")

    assert completed.status_code == 200
    payload = completed.get_json()
    assert payload["done"] is True
    assert _tree_ids(payload) == {
        "#V#global",
        "#V#alice_private",
        "#V#org_a_private",
    }
    assert observations == [("tree_build", "#V#alice", "#V#org_a", True)]
    assert access_control.get_effective_user_concept_id() is None
    assert access_control.get_effective_organisation_concept_id() is None
    assert access_control.should_enforce_access_control() is False


def test_anonymous_tree_worker_discards_residual_org_and_is_global_only(
    app_client,
    monkeypatch,
) -> None:
    _, client = app_client
    observations = _install_visible_tree(monkeypatch)
    _set_actor(client, None, "#V#org_a")

    created = client.post("/api/vontology/tree_async")

    assert created.status_code == 202
    job_id = created.get_json()["job_id"]
    state = _DeferredThread.created[0].args[0]
    assert state.job_id == job_id
    assert (state.user_concept_id, state.organisation_concept_id) == (None, None)
    _run_route_worker()
    completed = client.get(f"/api/vontology/tree_progress/{job_id}")

    assert completed.status_code == 200
    assert _tree_ids(completed.get_json()) == {"#V#global"}
    assert observations == [("tree_build", None, None, True)]


def test_route_preserves_scope_in_a_real_background_thread(monkeypatch) -> None:
    app = Flask(__name__)
    app.secret_key = "tree-real-thread-test"
    app.config["TESTING"] = True
    app.register_blueprint(routes.vontology_bp, url_prefix="/api/vontology")
    client = app.test_client()
    observations = _install_visible_tree(monkeypatch)
    worker_finished = threading.Event()
    original_worker = routes._build_tree_job

    def _observed_worker(*args: Any) -> None:
        try:
            original_worker(*args)
        finally:
            worker_finished.set()

    monkeypatch.setattr(routes, "_build_tree_job", _observed_worker)
    _set_actor(client, "#V#alice", "#V#org_a")

    created = client.post("/api/vontology/tree_async")

    assert created.status_code == 202
    assert worker_finished.wait(timeout=2.0) is True
    job_id = created.get_json()["job_id"]
    completed = client.get(f"/api/vontology/tree_progress/{job_id}")
    assert completed.status_code == 200
    assert _tree_ids(completed.get_json()) == {
        "#V#global",
        "#V#alice_private",
        "#V#org_a_private",
    }
    assert observations == [("tree_build", "#V#alice", "#V#org_a", True)]


def test_validated_legacy_read_header_remains_bound_to_its_job(
    app_client,
    monkeypatch,
) -> None:
    _, client = app_client
    observations = _install_visible_tree(monkeypatch)
    monkeypatch.setattr(
        access_control,
        "_validate_person_concept",
        lambda concept_id: concept_id,
    )
    headers = {"X-User-Concept-ID": "#V#alice"}

    created = client.post("/api/vontology/tree_async", headers=headers)

    assert created.status_code == 202
    job_id = created.get_json()["job_id"]
    _run_route_worker()
    completed = client.get(
        f"/api/vontology/tree_progress/{job_id}",
        headers=headers,
    )
    assert completed.status_code == 200
    assert _tree_ids(completed.get_json()) == {
        "#V#global",
        "#V#alice_private",
    }
    assert observations == [("tree_build", "#V#alice", None, True)]
    assert client.get(f"/api/vontology/tree_progress/{job_id}").status_code == 404


def test_tree_progress_conceals_job_from_incompatible_actor_scopes(
    app_client,
) -> None:
    _, client = app_client
    _set_actor(client, "#V#alice", "#V#org_a")
    created = client.post("/api/vontology/tree_async")
    job_id = created.get_json()["job_id"]
    with routes._TREE_BUILD_PROGRESS_LOCK:
        job = routes._TREE_BUILD_PROGRESS[job_id]
        job["result"] = {"tree": [{"id": "#V#alice_private"}]}
        job["completed_at_epoch"] = routes.time.time()

    owner_response = client.get(f"/api/vontology/tree_progress/{job_id}")
    assert owner_response.status_code == 200
    assert not any("owner" in key for key in owner_response.get_json())

    _set_actor(client, "#V#bob", "#V#org_a")
    other_user = client.get(f"/api/vontology/tree_progress/{job_id}")
    unknown = client.get("/api/vontology/tree_progress/not-a-job")
    assert other_user.status_code == unknown.status_code == 404
    assert other_user.get_json() == unknown.get_json() == {"error": "unknown job"}

    _set_actor(client, "#V#alice", "#V#org_b")
    other_org = client.get(f"/api/vontology/tree_progress/{job_id}")
    assert other_org.status_code == 404
    assert other_org.get_json() == {"error": "unknown job"}

    _set_actor(client, "#V#alice", "#V#org_a")
    assert client.get(f"/api/vontology/tree_progress/{job_id}").status_code == 200


def test_tree_job_is_removed_when_worker_thread_cannot_start(
    app_client,
    monkeypatch,
) -> None:
    _, client = app_client
    monkeypatch.setattr(routes.threading, "Thread", _FailingThread)
    _set_actor(client, "#V#alice", "#V#org_a")

    response = client.post("/api/vontology/tree_async")

    assert response.status_code == 500
    assert response.get_json() == {"error": "tree build could not be started"}
    with routes._TREE_BUILD_PROGRESS_LOCK:
        assert routes._TREE_BUILD_PROGRESS == {}


@pytest.mark.parametrize("failure_mode", ["returned", "raised"])
def test_tree_worker_failures_are_terminal(
    app_client,
    monkeypatch,
    failure_mode: str,
) -> None:
    _, client = app_client
    if failure_mode == "returned":
        monkeypatch.setattr(
            routes,
            "get_vontology_tree",
            lambda _root: {"error": "returned tree failure", "tree": []},
        )
    else:

        def _raise(_root: str):
            raise RuntimeError("raised tree failure")

        monkeypatch.setattr(routes, "get_vontology_tree", _raise)
    _set_actor(client, "#V#alice", "#V#org_a")
    created = client.post("/api/vontology/tree_async")
    job_id = created.get_json()["job_id"]

    _run_route_worker()
    completed = client.get(f"/api/vontology/tree_progress/{job_id}")

    assert completed.status_code == 200
    payload = completed.get_json()
    assert payload["done"] is True
    if failure_mode == "returned":
        assert failure_mode in payload["error"]
    else:
        assert payload["error"] == (
            "Failed to retrieve Vontology structure due to an internal server error."
        )
    assert "result" not in payload
    with routes._TREE_BUILD_PROGRESS_LOCK:
        assert isinstance(
            routes._TREE_BUILD_PROGRESS[job_id]["completed_at_epoch"],
            float,
        )
    assert _DeferredThread.created[1].joined is True


def test_tree_result_cleanup_expires_only_old_terminal_jobs(
    app_client,
    monkeypatch,
) -> None:
    _, client = app_client
    monkeypatch.setattr(routes, "_TREE_BUILD_RESULT_TTL_SECONDS", 10)
    monkeypatch.setattr(routes.time, "time", lambda: 100.0)
    base_job = {
        "total": 1,
        "processed": 1,
        "owner_user_concept_id": None,
        "owner_organisation_concept_id": None,
    }
    with routes._TREE_BUILD_PROGRESS_LOCK:
        routes._TREE_BUILD_PROGRESS.update(
            {
                "old_success": {
                    **base_job,
                    "result": {"tree": []},
                    "error": None,
                    "completed_at_epoch": 90.0,
                },
                "old_failure": {
                    **base_job,
                    "result": None,
                    "error": "failed",
                    "completed_at_epoch": 89.0,
                },
                "recent_success": {
                    **base_job,
                    "result": {"tree": []},
                    "error": None,
                    "completed_at_epoch": 91.0,
                },
                "old_running": {
                    **base_job,
                    "processed": 0,
                    "result": None,
                    "error": None,
                    "completed_at_epoch": None,
                },
            }
        )

    expired = client.get("/api/vontology/tree_progress/old_success")

    assert expired.status_code == 404
    with routes._TREE_BUILD_PROGRESS_LOCK:
        assert set(routes._TREE_BUILD_PROGRESS) == {
            "recent_success",
            "old_running",
        }
    recent = client.get("/api/vontology/tree_progress/recent_success")
    running = client.get("/api/vontology/tree_progress/old_running")
    assert recent.status_code == running.status_code == 200
    assert recent.get_json()["done"] is True
    assert running.get_json()["done"] is False
