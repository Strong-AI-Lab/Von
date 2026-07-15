from __future__ import annotations

import mongomock

from src.backend.services.workflow_llm_duration_stats_service import (
    calculate_next_duration_stats,
    record_workflow_llm_step_duration_observation,
)


def test_calculate_next_duration_stats_uses_welford_sufficient_stats() -> None:
    stats = calculate_next_duration_stats(None, duration_ms=100.0)
    assert stats is not None
    stats = calculate_next_duration_stats(stats, duration_ms=300.0)
    assert stats is not None
    stats = calculate_next_duration_stats(stats, duration_ms=500.0)

    assert stats is not None
    assert stats["observed_count"] == 3
    assert stats["total_duration_ms"] == 900.0
    assert stats["mean_duration_ms"] == 300.0
    assert stats["m2_duration_ms"] == 80000.0
    assert stats["variance_duration_ms"] == 40000.0
    assert stats["stddev_duration_ms"] == 200.0


def test_record_workflow_llm_step_duration_persists_no_raw_duration_list() -> None:
    collection = mongomock.MongoClient().db.workflow_llm_step_duration_stats

    first_baseline = record_workflow_llm_step_duration_observation(
        workflow_id="#V#workflow",
        workflow_state_id="#V#step",
        workflow_stage_id="#V#step",
        stage="selector_decision",
        provider="openai",
        model_name="gpt-test",
        duration_ms=100,
        request_id="req-1",
        collection=collection,
    )
    second_baseline = record_workflow_llm_step_duration_observation(
        workflow_id="#V#workflow",
        workflow_state_id="#V#step",
        workflow_stage_id="#V#step",
        stage="selector_decision",
        provider="openai",
        model_name="gpt-test",
        duration_ms=300,
        request_id="req-2",
        collection=collection,
    )

    assert first_baseline is None
    assert second_baseline is not None
    assert second_baseline["historical_observation_count"] == 1
    assert second_baseline["historical_mean_duration_ms"] == 100.0
    assert (
        second_baseline["duration_deviation_classification"] == "insufficient_history"
    )

    doc = collection.find_one({"model_name": "gpt-test"}, {"_id": 0})
    assert doc is not None
    assert doc["schema_version"] == "workflow_llm_step_duration_stats.v1"
    assert doc["observed_count"] == 2
    assert doc["mean_duration_ms"] == 200.0
    assert doc["m2_duration_ms"] == 20000.0
    assert "durations" not in doc
    assert "raw_durations" not in doc


def test_duration_stats_accumulate_success_and_failure_outcomes_separately() -> None:
    stats = calculate_next_duration_stats(
        None,
        duration_ms=100.0,
        outcome="success",
    )
    assert stats is not None
    stats = calculate_next_duration_stats(
        stats,
        duration_ms=300.0,
        outcome="success",
    )
    assert stats is not None
    stats = calculate_next_duration_stats(
        stats,
        duration_ms=500.0,
        outcome="timeout",
    )

    assert stats is not None
    assert stats["successful_observation_count"] == 2
    assert stats["successful_mean_duration_ms"] == 200.0
    assert stats["failed_observation_count"] == 1
    assert stats["failed_mean_duration_ms"] == 500.0
    assert stats["timeout_observation_count"] == 1
    assert stats["last_outcome"] == "timeout"
