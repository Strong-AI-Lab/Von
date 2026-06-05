from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import atlas_query_review_service as svc


def _row(
    shape_hash: str,
    *,
    namespace: str = "von_db.workflow_instances",
    command: str = "find",
    total_ms: float = 10_000,
    avg_ms: float = 10,
    count: int = 100,
    docs_ratio: float = 10,
    keys_ratio: float = 10,
    suggested_count: int = 0,
    repo_index: str = "not_evaluated",
    shape_text: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "query_shape_hash": shape_hash,
        "namespace": namespace,
        "command_name": command,
        "total_execution_time_ms": total_ms,
        "average_execution_time_ms": avg_ms,
        "execution_count": count,
        "docs_examined": docs_ratio * max(count, 1),
        "keys_examined": keys_ratio * max(count, 1),
        "docs_returned": count,
        "docs_examined_per_returned": docs_ratio,
        "keys_examined_per_returned": keys_ratio,
        "p90_execution_time_ms": avg_ms * 1.5,
        "p99_execution_time_ms": avg_ms * 2,
        "repo_index_comparison": repo_index,
    }
    if suggested_count:
        row["atlas_suggested_indexes"] = {
            "suggested_index_count": suggested_count,
            "index_names": ["status_1_created_at_1"],
        }
    if shape_text:
        row["query_shape_text_redacted"] = shape_text
    return row


def _report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "atlas_query_insights_report.v1",
        "status": "ok",
        "runtime_config": {
            "cluster_name": "Cluster0",
            "credential_values_redacted": True,
        },
        "filters": {
            "since": "2026-06-05T00:00:00Z",
            "until": "2026-06-05T01:00:00Z",
        },
        "rows": rows,
    }


def test_diff_reports_new_persistent_resolved_regressed_and_improved() -> None:
    previous = _report(
        [
            _row("persistent", total_ms=10_000, count=200),
            _row("resolved", total_ms=30_000, count=200),
            _row("regressed", total_ms=10_000, count=500),
            _row("improved", total_ms=100_000, count=500),
        ]
    )
    current = _report(
        [
            _row("persistent", total_ms=11_000, count=200),
            _row("new", total_ms=80_000, count=200),
            _row("regressed", total_ms=40_000, count=500),
            _row("improved", total_ms=20_000, count=500),
        ]
    )

    diff = svc.diff_atlas_query_reports(previous, current)

    assert diff["counts"]["new"] == 1
    assert diff["counts"]["persistent"] == 3
    assert diff["counts"]["resolved"] == 1
    assert diff["counts"]["regressed"] == 1
    assert diff["counts"]["improved"] == 1
    assert diff["new"][0]["query_shape_hash"] == "new"
    assert diff["resolved"][0]["query_shape_hash"] == "resolved"
    assert diff["regressed"][0]["query_shape_hash"] == "regressed"
    assert diff["improved"][0]["query_shape_hash"] == "improved"


def test_candidate_classification_uses_evidence_categories() -> None:
    thresholds = svc.AtlasQueryReviewThresholds(
        high_examined_returned_ratio=100,
        high_frequency_execution_count=1000,
        low_latency_ms=20,
        persistent_min_total_execution_time_ms=5000,
    )

    categories = svc.classify_review_candidate(
        _row(
            "shape",
            command="aggregate",
            total_ms=50_000,
            avg_ms=5,
            count=5000,
            docs_ratio=500,
            suggested_count=1,
            repo_index="not_evaluated",
            shape_text='{"pipeline":[{"$match":{"name":{"$regex":"<redacted>"}}}]}',
        ),
        thresholds=thresholds,
    )

    assert "missing_index" in categories
    assert "possible_data_model_issue" in categories
    assert "expensive_regex_text_scan" in categories
    assert "high_frequency_low_latency_hot_path" in categories
    assert "aggregate_recomputation" in categories


def test_jira_payload_generation_includes_required_evidence_and_redacts_literals() -> (
    None
):
    previous = _report([_row("hot", total_ms=50_000, count=1000)])
    current = _report(
        [
            _row(
                "hot",
                total_ms=120_000,
                count=2000,
                docs_ratio=2000,
                suggested_count=1,
                repo_index="candidate_collection_has_repo_index_definitions",
                shape_text='{"filter":{"session_id":"<redacted>","attempt":"<number>"}}',
            )
        ]
    )
    options = svc.AtlasReviewOptions(
        thresholds=svc.AtlasQueryReviewThresholds(
            high_total_execution_time_ms=60_000,
            high_examined_returned_ratio=1000,
        )
    )

    review = svc.build_atlas_query_review(
        previous_report=previous,
        current_report=current,
        options=options,
    )

    assert review["candidate_count"] == 1
    payload = review["jira_task_payloads"][0]
    description = payload["description"]
    assert payload["parent_issue_key"] == "JVNAUTOSCI-2427"
    assert "atlas-query-insights" in payload["labels"]
    assert "likely-query-shape-index-mismatch" in payload["labels"]
    assert "Namespace: `von_db.workflow_instances`" in description
    assert "Query shape hash: `hot`" in description
    assert "Total execution time ms: `120000.0`" in description
    assert "Docs examined per returned: `2000.0`" in description
    assert "JVNAUTOSCI-2427" in description
    assert "secret-token" not in json.dumps(review)


def test_duplicate_task_suppression_prefers_regressed_state() -> None:
    previous = _report([_row("same", total_ms=10_000, count=500)])
    current = _report([_row("same", total_ms=100_000, count=500)])

    review = svc.build_atlas_query_review(
        previous_report=previous,
        current_report=current,
        options=svc.AtlasReviewOptions(
            thresholds=svc.AtlasQueryReviewThresholds(
                high_total_execution_time_ms=50_000
            )
        ),
    )

    assert review["candidate_count"] == 1
    assert (
        review["jira_task_payloads"][0]["description"].count("Review fingerprint") == 1
    )
    assert (
        "State in latest comparison: `regressed`"
        in review["jira_task_payloads"][0]["description"]
    )


def test_apply_jira_payloads_dry_run_and_approved_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "project_key": "JVNAUTOSCI",
        "issue_type": "Task",
        "summary": "Review Mongo query shape",
        "description": "Description",
        "fingerprint": "abc123",
        "search_label": "mongo-query-tuning",
        "labels": ["mongo-query-tuning"],
        "existing_comment": "Comment",
    }

    dry = svc.apply_jira_task_payloads([payload], options=svc.AtlasReviewOptions())
    assert dry == [
        {
            "success": True,
            "mode": "dry_run",
            "fingerprint": "abc123",
            "summary": "Review Mongo query shape",
        }
    ]

    calls: list[dict[str, Any]] = []

    def fake_upsert(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "mode": "created_new", "issue_key": "JVNAUTOSCI-2500"}

    monkeypatch.setattr(
        "src.backend.services.jira_deduplicated_issue_service.upsert_deduplicated_jira_issue",
        fake_upsert,
    )

    approved = svc.apply_jira_task_payloads(
        [payload],
        options=svc.AtlasReviewOptions(dry_run=False, approved=True),
    )

    assert approved[0]["issue_key"] == "JVNAUTOSCI-2500"
    assert calls[0]["fingerprint"] == "abc123"
    assert calls[0]["search_label"] == "mongo-query-tuning"
    assert calls[0]["link_issue_key"] == "JVNAUTOSCI-2427"
    assert calls[0]["link_type"] == "Relates"


def test_report_file_blocker_for_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(svc.AtlasReviewTypedBlocker) as exc_info:
        svc.read_report_file(path)

    assert exc_info.value.blocker_type == "report_file_unreadable"


def test_review_secret_guard_blocks_known_secret_markers() -> None:
    with pytest.raises(RuntimeError):
        svc.assert_review_report_has_no_known_secrets({"value": "client-secret"})
