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
                "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
            },
            {
                "concept_id": "#V#organisation",
                "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
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
