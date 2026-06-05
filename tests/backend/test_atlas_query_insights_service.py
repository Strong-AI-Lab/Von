from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.services.atlas_query_insights_service import (
    AtlasAdminApiClient,
    AtlasQueryInsightsConfig,
    AtlasQueryInsightsFilters,
    AtlasTypedBlocker,
    build_atlas_query_insights_report,
    build_query_insights_summary_params,
    load_atlas_query_insights_config_from_env,
    parse_query_insights_summaries,
    rank_query_insights_rows,
    redact_query_shape_text,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return FakeResponse(200, {"access_token": "token-from-atlas"})


def _config(**overrides: Any) -> AtlasQueryInsightsConfig:
    values = {
        "group_id": "group 1",
        "cluster_name": "Cluster A",
        "base_url": "https://cloud.mongodb.com",
        "access_token": "secret-token",
    }
    values.update(overrides)
    return AtlasQueryInsightsConfig(**values)


def test_summary_params_repeat_filters_and_cap_result_count() -> None:
    filters = AtlasQueryInsightsFilters(
        since="2026-06-05T00:00:00Z",
        until="2026-06-05T01:00:00Z",
        namespaces=("von_db.workflow_instances", "von_db.workflow_use_episodes"),
        commands=("find", "aggregate"),
        query_shape_hashes=("abc",),
        series=("TOTAL_EXECUTION_TIME", "P99_EXECUTION_TIME"),
        max_results=500,
    )

    params = build_query_insights_summary_params(filters)

    assert params[0] == ("nSummaries", "100")
    assert ("since", "2026-06-05T00:00:00Z") in params
    assert ("until", "2026-06-05T01:00:00Z") in params
    assert params.count(("namespaces", "von_db.workflow_instances")) == 1
    assert params.count(("commands", "aggregate")) == 1
    assert params.count(("queryShapeHashes", "abc")) == 1
    assert params.count(("series", "P99_EXECUTION_TIME")) == 1


def test_client_builds_encoded_summary_url_and_redacted_bearer_request() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "queryShapeHash": "shape-1",
                            "namespace": "von_db.workflow_instances",
                            "command": "find",
                            "totalExecutionTimeMs": 12,
                            "executionCount": 3,
                            "docsExamined": 30,
                            "docsReturned": 3,
                        }
                    ]
                },
            )
        ]
    )
    config = _config()
    report = build_atlas_query_insights_report(
        config,
        AtlasQueryInsightsFilters(max_results=10),
        client=AtlasAdminApiClient(config, session=session),
        repo_root=Path("/tmp/does-not-exist"),
    )

    call = session.get_calls[0]
    assert call["url"].endswith(
        "/api/atlas/v2/groups/group%201/clusters/Cluster%20A/queryShapeInsights/summaries"
    )
    assert call["headers"]["Authorization"] == "Bearer secret-token"
    assert ("nSummaries", "10") in call["params"]
    rendered = json.dumps(report, sort_keys=True)
    assert "secret-token" not in rendered
    assert report["runtime_config"]["credential_values_redacted"] is True
    assert report["rows"][0]["docs_examined_per_returned"] == 10.0


def test_service_account_fetches_token_before_get_without_leaking_secret() -> None:
    session = FakeSession([FakeResponse(200, {"results": []})])
    config = _config(
        access_token=None,
        service_account_client_id="client-id",
        service_account_client_secret="client-secret",
    )

    report = build_atlas_query_insights_report(
        config,
        AtlasQueryInsightsFilters(max_results=1),
        client=AtlasAdminApiClient(config, session=session),
        repo_root=Path("/tmp/does-not-exist"),
    )

    assert session.post_calls
    assert session.get_calls[0]["headers"]["Authorization"] == "Bearer token-from-atlas"
    rendered = json.dumps(report, sort_keys=True)
    assert "client-secret" not in rendered
    assert "token-from-atlas" not in rendered


def test_missing_credentials_raises_typed_blocker() -> None:
    config = _config(access_token=None)
    session = FakeSession([FakeResponse(200, {"results": []})])

    with pytest.raises(AtlasTypedBlocker) as exc_info:
        build_atlas_query_insights_report(
            config,
            AtlasQueryInsightsFilters(),
            client=AtlasAdminApiClient(config, session=session),
            repo_root=Path("/tmp/does-not-exist"),
        )

    assert exc_info.value.blocker_type == "missing_credentials"


def test_permission_denied_maps_to_typed_blocker() -> None:
    session = FakeSession([FakeResponse(403, {"error": "forbidden"})])
    config = _config()

    with pytest.raises(AtlasTypedBlocker) as exc_info:
        build_atlas_query_insights_report(
            config,
            AtlasQueryInsightsFilters(),
            client=AtlasAdminApiClient(config, session=session),
            repo_root=Path("/tmp/does-not-exist"),
        )

    assert exc_info.value.blocker_type == "insufficient_atlas_role"
    assert exc_info.value.status_code == 403


def test_parser_accepts_direct_and_series_metrics_and_ranking_prefers_total_time() -> None:
    rows = parse_query_insights_summaries(
        {
            "results": [
                {
                    "queryShapeHash": "low-targeting",
                    "namespace": "von_db.workflow_selection_experiences",
                    "command": "find",
                    "totalExecutionTimeMs": 4442,
                    "executionCount": 1,
                    "docsExamined": 120,
                    "keysExamined": 120,
                    "docsReturned": 120,
                    "p99ExecutionTimeMs": 4442,
                },
                {
                    "queryShapeHash": "hot-aggregate",
                    "namespace": "von_db.workflow_use_episodes",
                    "command": "aggregate",
                    "series": [
                        {"name": "TOTAL_EXECUTION_TIME", "value": 98765},
                        {"name": "AVG_EXECUTION_TIME", "value": 8.5},
                        {"name": "EXECUTION_COUNT", "value": 12000},
                        {"name": "DOCS_EXAMINED", "value": 2400000},
                        {"name": "KEYS_EXAMINED", "value": 2400000},
                        {"name": "DOCS_RETURNED", "value": 12000},
                        {"name": "P90_EXECUTION_TIME", "value": 15},
                        {"name": "P99_EXECUTION_TIME", "value": 80},
                    ],
                },
            ]
        }
    )

    ranked = rank_query_insights_rows(rows, limit=2)

    assert ranked[0]["namespace"] == "von_db.workflow_use_episodes"
    assert ranked[0]["command_name"] == "aggregate"
    assert ranked[0]["execution_count"] == 12000
    assert ranked[0]["docs_examined_per_returned"] == 200.0
    assert ranked[0]["p99_execution_time_ms"] == 80


def test_query_shape_text_redacts_literal_values() -> None:
    redacted = redact_query_shape_text(
        {
            "aggregate": "workflow_use_episodes",
            "pipeline": [
                {
                    "$match": {
                        "session_id": "secret-session",
                        "created_at": {"$gte": "2026-06-05T00:00:00Z"},
                        "attempt": 42,
                    }
                }
            ],
        }
    )

    assert "secret-session" not in redacted
    assert "2026-06-05" not in redacted
    assert "42" not in redacted
    assert "session_id" in redacted
    assert "created_at" in redacted
    assert "<redacted>" in redacted
    assert "<number>" in redacted


def test_full_report_joins_shape_details_and_repo_index_evidence(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "indexes.py").write_text(
        'db["workflow_instances"].create_index([("status", 1)])\n',
        encoding="utf-8",
    )
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "queryShapeHash": "shape-1",
                            "namespace": "von_db.workflow_instances",
                            "command": "find",
                            "totalExecutionTimeMs": 100,
                            "executionCount": 10,
                            "docsExamined": 1000,
                            "keysExamined": 1000,
                            "docsReturned": 10,
                        }
                    ]
                },
            ),
            FakeResponse(
                200,
                {
                    "results": [
                        {
                            "queryShapeHash": "shape-1",
                            "namespace": "von_db.workflow_instances",
                            "command": "find",
                            "queryShape": {"filter": {"status": "secret"}},
                        }
                    ]
                },
            ),
        ]
    )
    config = _config()

    report = build_atlas_query_insights_report(
        config,
        AtlasQueryInsightsFilters(
            max_results=5,
            include_query_shapes=True,
            include_shape_text=True,
        ),
        client=AtlasAdminApiClient(config, session=session),
        repo_root=tmp_path,
    )

    row = report["rows"][0]
    assert row["query_shape_text_redacted"]
    assert "secret" not in row["query_shape_text_redacted"]
    assert row["repo_index_comparison"] == "candidate_collection_has_repo_index_definitions"
    assert row["repo_index_evidence_files"] == ["src/indexes.py"]


def test_env_config_reports_missing_project_before_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ATLAS_GROUP_ID",
        "MONGODB_ATLAS_GROUP_ID",
        "ATLAS_PROJECT_ID",
        "MONGODB_ATLAS_PROJECT_ID",
        "ATLAS_CLUSTER_NAME",
        "MONGODB_ATLAS_CLUSTER_NAME",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(AtlasTypedBlocker) as exc_info:
        load_atlas_query_insights_config_from_env(cluster_name="Cluster0")

    assert exc_info.value.blocker_type == "missing_project_id"
