from __future__ import annotations

import copy
from typing import Any


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)

    def skip(self, n: int):
        self._docs = self._docs[int(n) :]
        return self

    def limit(self, n: int):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


class _ExperimentRunCollection:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = [copy.deepcopy(doc) for doc in docs]
        self.last_query: dict[str, Any] | None = None

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        namespace = query.get("namespace")
        if isinstance(namespace, str) and doc.get("namespace") != namespace:
            return False

        experiment_spec_id = query.get("experiment_spec_id")
        if isinstance(experiment_spec_id, str) and doc.get("experiment_spec_id") != experiment_spec_id:
            return False

        theory_id = query.get("theory_id")
        if isinstance(theory_id, str) and doc.get("theory_id") != theory_id:
            return False

        workflow_id = query.get("target_workflow_ids")
        if isinstance(workflow_id, str) and workflow_id not in (doc.get("target_workflow_ids") or []):
            return False

        benchmark_tier = query.get("benchmark_tier")
        if isinstance(benchmark_tier, str) and doc.get("benchmark_tier") != benchmark_tier:
            return False

        verdict_filter = query.get("verdict")
        verdict = doc.get("verdict")
        if isinstance(verdict_filter, str) and verdict != verdict_filter:
            return False
        if isinstance(verdict_filter, dict):
            options = verdict_filter.get("$in")
            if isinstance(options, list) and verdict not in options:
                return False

        created_range = query.get("created_at_utc")
        if isinstance(created_range, dict):
            created_at = str(doc.get("created_at_utc") or "")
            gte = created_range.get("$gte")
            if isinstance(gte, str) and created_at < gte:
                return False
            lte = created_range.get("$lte")
            if isinstance(lte, str) and created_at > lte:
                return False

        return True

    def find(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        self.last_query = dict(query)
        return _Cursor([doc for doc in self._docs if self._matches(doc, query)])

    def find_one(self, query: dict[str, Any], _projection: dict[str, Any] | None = None):
        for doc in self.find(query, _projection):
            return copy.deepcopy(doc)
        return None

    def count_documents(self, query: dict[str, Any]) -> int:
        return len(list(self.find(query)))


class _DB:
    def __init__(self, docs: list[dict[str, Any]]):
        self._collection = _ExperimentRunCollection(docs)

    def __getitem__(self, key: str) -> _ExperimentRunCollection:
        if key != "experiment_runs":
            raise KeyError(key)
        return self._collection


def test_rag_list_collections_includes_experiment_runs():
    from src.backend.integrations.internal_mcp import catalogue as cat

    result = cat._rag_list_collections(namespace="#V#user@org")

    assert result["success"] is True
    by_name = {row["collection"]: row for row in result["collections"]}
    assert "experiment_runs" in by_name
    assert by_name["experiment_runs"]["list_supported"] is True
    assert by_name["experiment_runs"]["get_supported"] is True
    assert by_name["experiment_runs"]["source_system"] == "mongo.experiment_runs"


def test_experiment_run_list_wrapper_supports_filters_and_provenance(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "run_id": "#V#run_pass",
            "experiment_spec_id": "#V#spec_meeting",
            "theory_id": "#V#theory_meeting",
            "created_at_utc": "2026-03-18T00:00:00Z",
            "updated_at_utc": "2026-03-18T00:05:00Z",
            "namespace": "#V#user@org",
            "status": "completed",
            "verdict": "pass",
            "benchmark_tier": "tier1",
            "target_workflow_ids": ["#V#wf_meeting"],
            "metrics": {"observation_total": 2},
            "promotion_recommendation": {"recommended": True},
            "replay_case": {"case_id": "case-pass"},
        },
        {
            "run_id": "#V#run_fail",
            "experiment_spec_id": "#V#spec_meeting",
            "theory_id": "#V#theory_meeting",
            "created_at_utc": "2026-03-19T00:00:00Z",
            "updated_at_utc": "2026-03-19T00:05:00Z",
            "namespace": "#V#user@org",
            "status": "failed",
            "verdict": "fail",
            "benchmark_tier": "tier2",
            "target_workflow_ids": ["#V#wf_other"],
            "metrics": {"observation_total": 1},
            "promotion_recommendation": {"recommended": False},
            "replay_case": {"case_id": "case-fail"},
        },
    ]
    db = _DB(docs)
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: db,
    )

    result = cat._experiment_run_list(
        namespace="#V#user@org",
        limit=10,
        offset=0,
        verdict="pass",
        workflow_id="#V#wf_meeting",
        from_utc="2026-03-17T00:00:00Z",
        to_utc="2026-03-18T23:59:59Z",
    )

    assert result["success"] is True
    assert result["collection"] == "experiment_runs"
    assert result["requested_collection"] == "experiment_runs"
    assert result["effective_collection"] == "experiment_runs"
    assert result["provenance"]["item_kind"] == "experiment_run_list"
    assert result["total"] == 1
    assert result["items"] == [
        {
            "collection": "experiment_runs",
            "session_id": "#V#run_pass",
            "run_id": "#V#run_pass",
            "experiment_spec_id": "#V#spec_meeting",
            "theory_id": "#V#theory_meeting",
            "created_at_utc": "2026-03-18T00:00:00Z",
            "updated_at_utc": "2026-03-18T00:05:00Z",
            "namespace": "#V#user@org",
            "status": "completed",
            "verdict": "pass",
            "benchmark_tier": "tier1",
            "target_workflow_ids": ["#V#wf_meeting"],
            "observation_total": 2,
            "replay_case_id": "case-pass",
            "promotion_recommended": True,
            "item_kind": "experiment_run",
            "source_system": "mongo.experiment_runs",
            "namespace_source": "request.namespace",
        }
    ]


def test_experiment_run_get_wrapper_returns_full_projection(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue as cat

    docs = [
        {
            "run_id": "#V#run_pass",
            "experiment_spec_id": "#V#spec_meeting",
            "theory_id": "#V#theory_meeting",
            "created_at_utc": "2026-03-18T00:00:00Z",
            "updated_at_utc": "2026-03-18T00:05:00Z",
            "completed_at_utc": "2026-03-18T00:06:00Z",
            "namespace": "#V#user@org",
            "status": "completed",
            "verdict": "pass",
            "benchmark_tier": "tier1",
            "target_workflow_ids": ["#V#wf_meeting"],
            "candidate_workflow_ids": ["#V#wf_meeting", "#V#wf_backup"],
            "turn_execution_request_ids": ["req-1"],
            "observations": [{"label": "structured_fields", "verdict": "pass"}],
            "metrics": {"observation_total": 1},
            "verdict_summary": {"reason": "all_recorded_observations_passed"},
            "promotion_recommendation": {"recommended": True},
            "learning_signal": {"selection_outcome": "completed"},
            "replay_case": {"case_id": "case-pass"},
        }
    ]
    monkeypatch.setattr(
        "src.backend.db.connection_manager.get_db",
        lambda: _DB(docs),
    )

    result = cat._experiment_run_get(
        namespace="#V#user@org",
        run_id="#V#run_pass",
    )

    assert result["success"] is True
    assert result["collection"] == "experiment_runs"
    assert result["run_id"] == "#V#run_pass"
    assert result["candidate_workflow_ids"] == ["#V#wf_meeting", "#V#wf_backup"]
    assert result["verdict_summary"]["reason"] == "all_recorded_observations_passed"
    assert result["learning_signal"]["selection_outcome"] == "completed"
    assert result["provenance"]["item_kind"] == "experiment_run_item"
