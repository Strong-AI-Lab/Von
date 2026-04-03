from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _make_trace_doc(
    *,
    execution_id: str,
    workflow_id: str = "#V#workflow_prediction_target",
    status: str,
    start_offset_seconds: int,
    duration_ms: int,
    top_level_model: str | None = None,
    top_level_provider: str | None = None,
    top_level_usage: dict[str, int] | None = None,
    action_model: str | None = None,
    action_provider: str | None = None,
    action_usage: dict[str, int] | None = None,
) -> dict[str, object]:
    from src.backend.workflows.trace_model import WorkflowExecutionTrace

    start_time = datetime(2026, 4, 1, tzinfo=timezone.utc) + timedelta(
        seconds=start_offset_seconds
    )
    trace = WorkflowExecutionTrace(
        workflow_id=workflow_id,
        execution_id=execution_id,
    )
    trace.start_time = start_time
    trace.end_time = start_time + timedelta(milliseconds=duration_ms)
    trace.status = status
    if top_level_model:
        trace.metadata["default_model"] = top_level_model
    if top_level_provider:
        trace.metadata["default_provider"] = top_level_provider
    if top_level_usage:
        trace.metadata["llm_usage"] = dict(top_level_usage)
        trace.metadata["llm_calls"] = [
            {
                "model": top_level_model,
                "provider": top_level_provider,
                "usage": dict(top_level_usage),
            }
        ]
    trace.metadata["workflow_dispatch_prepare_steps"] = [
        {
            "step_id": "selector_candidates",
            "step_label": "Prepare selector candidates",
            "duration_ms": 180,
        },
        {
            "step_id": "policy_load",
            "step_label": "Load workflow policy",
            "duration_ms": 60,
        },
    ]
    if action_model:
        trace.record_action(
            action_id="paper.rank",
            outputs={
                "llm_calls": [
                    {
                        "model_name": action_model,
                        "provider": action_provider,
                        "usage": dict(action_usage or {}),
                    }
                ]
            },
            duration_ms=900,
        )
    return trace.to_storage_document()


def test_build_workflow_prediction_envelope_aggregates_recent_traces(monkeypatch):
    import src.backend.services.workflow_prediction_service as service

    docs = [
        _make_trace_doc(
            execution_id="trace-1",
            status="completed",
            start_offset_seconds=0,
            duration_ms=1000,
            top_level_model="gpt-5-mini",
            top_level_provider="openai",
            top_level_usage={
                "prompt_tokens": 80,
                "completion_tokens": 20,
                "total_tokens": 100,
            },
        ),
        _make_trace_doc(
            execution_id="trace-2",
            status="completed",
            start_offset_seconds=10,
            duration_ms=3000,
            action_model="gpt-4o-mini",
            action_provider="openai",
            action_usage={
                "prompt_tokens": 150,
                "completion_tokens": 50,
                "total_tokens": 200,
            },
        ),
        _make_trace_doc(
            execution_id="trace-3",
            status="failed",
            start_offset_seconds=20,
            duration_ms=5000,
            top_level_model="gpt-5-mini",
            top_level_provider="openai",
            top_level_usage={
                "prompt_tokens": 60,
                "completion_tokens": 15,
                "total_tokens": 75,
            },
            action_model="gpt-5-mini",
            action_provider="openai",
            action_usage={
                "prompt_tokens": 60,
                "completion_tokens": 15,
                "total_tokens": 75,
            },
        ),
    ]

    monkeypatch.setattr(
        service,
        "list_recent_workflow_execution_traces",
        lambda **_kwargs: docs,
    )

    payload = service.build_workflow_prediction_envelope(
        workflow_id="#V#workflow_prediction_target",
        limit=25,
    )

    assert payload["success"] is True
    assert payload["schema_version"] == "workflow_prediction_envelope.v1"
    assert payload["sample_window"]["candidate_trace_count"] == 3
    assert payload["sample_window"]["matched_trace_count"] == 3

    envelope = payload["prediction_envelope"]
    assert envelope["duration_ms"]["sample_count"] == 3
    assert envelope["duration_ms"]["total"] == 9000
    assert envelope["completion_rate"] == 0.6667
    assert envelope["failure_rate"] == 0.3333
    assert envelope["llm_usage"]["aggregate_totals"]["total_tokens"] == 375

    dispatch_rows = envelope["dispatch_prepare_step_duration_ms"]
    selector_row = next(
        row for row in dispatch_rows if row["step_id"] == "selector_candidates"
    )
    assert selector_row["label"] == "Prepare selector candidates"
    assert selector_row["duration_ms"]["sample_count"] == 3

    action_rows = envelope["action_duration_ms"]
    action_row = next(row for row in action_rows if row["action_id"] == "paper.rank")
    assert action_row["duration_ms"]["sample_count"] == 2

    model_tradeoffs = envelope["model_tradeoffs"]
    assert {item["model"] for item in model_tradeoffs} == {
        "gpt-5-mini",
        "gpt-4o-mini",
    }
    gpt5 = next(item for item in model_tradeoffs if item["model"] == "gpt-5-mini")
    assert gpt5["run_count"] == 2
    assert gpt5["llm_usage"]["aggregate_totals"]["total_tokens"] == 175


def test_build_workflow_prediction_envelope_honours_model_filters(monkeypatch):
    import src.backend.services.workflow_prediction_service as service

    docs = [
        _make_trace_doc(
            execution_id="trace-openai",
            status="completed",
            start_offset_seconds=0,
            duration_ms=1200,
            top_level_model="gpt-5-mini",
            top_level_provider="openai",
            top_level_usage={
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        ),
        _make_trace_doc(
            execution_id="trace-other",
            status="completed",
            start_offset_seconds=5,
            duration_ms=2400,
            action_model="gpt-4o-mini",
            action_provider="openai",
            action_usage={
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        ),
    ]

    monkeypatch.setattr(
        service,
        "list_recent_workflow_execution_traces",
        lambda **_kwargs: docs,
    )

    payload = service.build_workflow_prediction_envelope(
        workflow_id="#V#workflow_prediction_target",
        provider="openai",
        model="gpt-5-mini",
    )

    assert payload["sample_window"]["candidate_trace_count"] == 2
    assert payload["sample_window"]["matched_trace_count"] == 1
    envelope = payload["prediction_envelope"]
    assert envelope["duration_ms"]["total"] == 1200
    assert envelope["llm_usage"]["aggregate_totals"]["total_tokens"] == 15
