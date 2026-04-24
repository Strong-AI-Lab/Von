from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.backend.db import mongo_client as mongo_client_module
from src.backend.server.routes import von_routes
from src.backend.server.utils_flask import create_flask_app
from src.backend.services import kb_clone_benchmark_service as benchmark_service


class _DummyLLMClient:
    def list_models(self):
        return ["dummy-model"]

    def generate(self, prompt: str, context=None, model: str | None = None) -> str:
        lowered = (prompt or "").lower()
        if "benchmark-ready" in lowered:
            return "benchmark-ready"
        if "archive-complete" in lowered:
            return "archive-complete"
        return f"echo:{prompt}"


@pytest.fixture(autouse=True)
def _mock_db_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "0")
    monkeypatch.setattr(
        von_routes,
        "get_llm_client",
        lambda *args, **kwargs: _DummyLLMClient(),
    )
    mongo_client_module.invalidate_connection()
    yield
    mongo_client_module.invalidate_connection()


def _seed_source_db(source_db_name: str):
    # Keep VON_DB_NAME aligned with the source DB so service-layer writes and
    # harness clone logic share the same mongomock client/database view.
    import os

    os.environ["VON_DB_NAME"] = source_db_name
    db = mongo_client_module.get_db()
    assert db is not None
    concepts = db["concepts"]
    concepts.delete_many({})
    concepts.insert_many(
        [
            {
                "concept_id": "#V#thing",
                "relationships": {"is_a_type_of": [], "is_an_instance_of": []},
            },
            {
                "concept_id": "#V#person",
                "relationships": {
                    "is_a_type_of": ["#V#thing"],
                    "is_an_instance_of": [],
                },
            },
            {
                "concept_id": "#V#organisation",
                "relationships": {
                    "is_a_type_of": ["#V#thing"],
                    "is_an_instance_of": [],
                },
            },
        ]
    )
    return db.client


def _build_dummy_app():
    def _list_models():
        return ["dummy-model"]

    def _generate(
        prompt: str, context: list[dict[str, str]] | None, model: str | None
    ) -> str:
        lowered = prompt.lower()
        if "benchmark-ready" in lowered:
            return "benchmark-ready"
        if "archive-complete" in lowered:
            return "archive-complete"
        return f"echo:{prompt}"

    return create_flask_app(_list_models, _generate)


def _scenario(source_db_name: str) -> dict[str, Any]:
    return {
        "scenario_id": "kb_clone_test_scenario",
        "source_db_name": source_db_name,
        "mode": "reliability",
        "runs": 2,
        "retry_budget": 0,
        "turns": [
            {
                "prompt": "Reply with benchmark-ready",
                "expected_substrings": ["benchmark-ready"],
            },
            {
                "prompt": "Reply with archive-complete",
                "expected_substrings": ["archive-complete"],
            },
        ],
    }


def test_configure_session_context_marks_benchmark_chat_session() -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    flask_session: dict[str, Any] = {}

    class _Response:
        def __init__(
            self, status_code: int = 200, payload: dict[str, Any] | None = None
        ):
            self.status_code = status_code
            self._payload = payload or {}

        def get_data(self, as_text: bool = False):
            return str(self._payload)

        def get_json(self, silent: bool = False):
            return dict(self._payload)

    class _SessionTransaction:
        def __enter__(self):
            return flask_session

        def __exit__(self, exc_type, exc, tb):
            return False

    class _ApiClient:
        def session_transaction(self):
            return _SessionTransaction()

        def post(self, path: str, json: dict[str, Any] | None = None):
            calls.append((path, json))
            if path == "/von/api/session/create_chat_session":
                return _Response(200, {"session_id": "benchmark-session-id"})
            return _Response(200, {"status": "ok"})

    session_id = benchmark_service._configure_session_context(
        api_client=_ApiClient(),
        user_concept_id="#V#benchmark_user",
        organisation_concept_id="#V#benchmark_org",
    )

    assert session_id == "benchmark-session-id"
    create_call = [
        payload
        for path, payload in calls
        if path == "/von/api/session/create_chat_session"
    ][0]
    assert create_call is not None
    assert str(create_call["session_name"]).startswith("Benchmark session ")
    assert create_call["origin_kind"] == "benchmark_harness"
    assert create_call["is_agent_created"] is True
    assert create_call["test_artifact_kind"] == "kb_clone_benchmark_chat_session"


def test_clone_database_ontology_slice_excludes_non_ontology_collections() -> None:
    source_db_name = "test_von_db_slice_source"
    clone_db_name = "test_von_db_slice_clone"
    mongo_client = _seed_source_db(source_db_name)
    source_db = mongo_client[source_db_name]

    source_db["text_values"].insert_one({"text": "seed", "lang": "en-NZ"})
    source_db["text_relations"].insert_one(
        {
            "subject_concept_id": "#V#thing",
            "predicate": "hasDescription",
            "text": "seed",
            "lang": "en-NZ",
        }
    )
    source_db["interaction_sessions"].insert_one(
        {"session_id": "noise", "history": [{"role": "user", "content": "noise"}]}
    )
    source_db["chat_history"].insert_one({"session_id": "noise-chat"})

    counts = benchmark_service.clone_database_ontology_slice(
        mongo_client,
        source_db_name=source_db_name,
        clone_db_name=clone_db_name,
    )

    assert set(counts.keys()) == {"concepts", "text_relations", "text_values"}
    clone_db = mongo_client[clone_db_name]
    clone_collections = set(clone_db.list_collection_names())
    assert "interaction_sessions" not in clone_collections
    assert "chat_history" not in clone_collections
    assert clone_db["concepts"].count_documents({}) == source_db[
        "concepts"
    ].count_documents({})
    assert clone_db["text_relations"].count_documents({}) == source_db[
        "text_relations"
    ].count_documents({})
    assert clone_db["text_values"].count_documents({}) == source_db[
        "text_values"
    ].count_documents({})


def test_kb_clone_benchmark_runs_end_to_end_and_cleans_up(tmp_path: Path) -> None:
    source_db_name = "test_von_db"
    mongo_client = _seed_source_db(source_db_name)
    app = _build_dummy_app()

    manifest = benchmark_service.run_kb_clone_benchmark(
        scenario=_scenario(source_db_name),
        output_root=tmp_path,
        app=app,
        mongo_client=mongo_client,
    )

    assert manifest["status"]["complete"] is True
    assert manifest["status"]["scenario_success"] is True
    assert manifest["reliability_metrics"]["pass_at_k"] == 1.0
    assert manifest["reliability_metrics"]["completion_consistency"] == 1.0
    assert manifest["teardown"]["deleted"] is True
    assert manifest["clone_db_name"] not in mongo_client.list_database_names()

    bundle_root = Path(str(manifest["bundle_root"]))
    assert (bundle_root / "manifest.json").exists()
    assert (bundle_root / "metrics.json").exists()
    assert (bundle_root / "archives" / "pre_run_snapshot.tar.gz").exists()
    assert (bundle_root / "archives" / "post_run_snapshot.tar.gz").exists()


def test_archive_verification_failure_retains_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db_name = "test_von_db"
    mongo_client = _seed_source_db(source_db_name)
    app = _build_dummy_app()

    monkeypatch.setattr(
        benchmark_service,
        "_verify_snapshot_archive",
        lambda _path, _sha: (False, "forced_archive_failure"),
    )

    manifest = benchmark_service.run_kb_clone_benchmark(
        scenario=_scenario(source_db_name),
        output_root=tmp_path,
        app=app,
        mongo_client=mongo_client,
    )

    assert manifest["status"]["complete"] is False
    assert manifest["teardown"]["attempted"] is False
    assert manifest["teardown"]["retained"] is True
    assert manifest["teardown"]["reason"] == "archive_verification_failed"
    assert manifest["clone_db_name"] in mongo_client.list_database_names()


def test_teardown_failure_marks_run_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db_name = "test_von_db"
    mongo_client = _seed_source_db(source_db_name)
    app = _build_dummy_app()

    def _raise_drop_failure(_client, _db_name):
        raise RuntimeError("forced_drop_failure")

    monkeypatch.setattr(benchmark_service, "_drop_database", _raise_drop_failure)

    manifest = benchmark_service.run_kb_clone_benchmark(
        scenario=_scenario(source_db_name),
        output_root=tmp_path,
        app=app,
        mongo_client=mongo_client,
    )

    assert manifest["status"]["complete"] is False
    assert manifest["teardown"]["attempted"] is True
    assert manifest["teardown"]["deleted"] is False
    assert manifest["teardown"]["retained"] is True
    assert manifest["teardown"]["reason"] == "teardown_failed"
    assert "forced_drop_failure" in str(manifest["teardown"]["error"])
    assert manifest["clone_db_name"] in mongo_client.list_database_names()
