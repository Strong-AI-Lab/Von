from __future__ import annotations

import json

from src.backend.services.turn_timing_telemetry_service import (
    MAX_TIMING_SPANS,
    build_turn_timing_trace,
    merge_timing_spans,
)


def test_turn_timing_trace_records_model_prompt_facts_without_raw_prompt() -> None:
    trace = build_turn_timing_trace(
        request_id="req-timing",
        llm_calls=[
            {
                "workflow_stage_id": "workflow_routing",
                "model": "gpt-5-mini",
                "provider": "openai",
                "prompt_text": "This is the raw prompt body that must not be stored.",
                "prompt_id": "#V#workflow_selector_prompt",
                "duration_ms": 1234,
                "started_at_utc": "2026-06-09T00:00:00Z",
                "first_output_at_utc": "2026-06-09T00:00:00.321Z",
                "completed_at_utc": "2026-06-09T00:00:01.234Z",
                "input_tokens": 120,
                "output_tokens": 40,
                "total_tokens": 160,
                "success": True,
            }
        ],
    )

    assert trace["schema_version"] == "turn_timing_trace.v1"
    assert trace["span_count"] == 1
    assert trace["summary"]["llm_elapsed_ms"] == 1234
    assert trace["model_prompt_summary"] == [
        {
            "stage_id": "workflow_routing",
            "provider": "openai",
            "model": "gpt-5-mini",
            "prompt_id": "#V#workflow_selector_prompt",
            "prompt_sha256": trace["model_prompt_summary"][0]["prompt_sha256"],
            "call_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "duration_ms": 1234,
            "first_output_latency_ms": {
                "min_ms": 321,
                "max_ms": 321,
                "mean_ms": 321,
            },
        }
    ]
    assert trace["spans"][0]["attributes"]["input_tokens"] == 120
    assert trace["spans"][0]["attributes"]["total_tokens"] == 160
    assert trace["model_prompt_summary"][0]["prompt_sha256"]
    assert "raw prompt body" not in json.dumps(trace, default=str)


def test_turn_timing_trace_bounds_spans_and_keeps_slowest_summary() -> None:
    spans = [
        {
            "span_id": f"span-{index}",
            "stage_id": "response_finalising",
            "operation_kind": "support",
            "operation_name": f"operation-{index}",
            "duration_ms": index,
        }
        for index in range(MAX_TIMING_SPANS + 7)
    ]

    trace = build_turn_timing_trace(request_id="req-many-spans", extra_spans=spans)

    assert trace["span_count"] == MAX_TIMING_SPANS + 7
    assert trace["stored_span_count"] == MAX_TIMING_SPANS
    assert trace["dropped_span_count"] == 7
    assert trace["truncated"] is True
    assert len(trace["spans"]) == MAX_TIMING_SPANS
    assert trace["slowest_spans"][0]["duration_ms"] == MAX_TIMING_SPANS + 6


def test_tool_invocation_spans_keep_argument_keys_not_argument_values() -> None:
    trace = build_turn_timing_trace(
        request_id="req-tool",
        tool_invocations=[
            {
                "tool": "get_text_relations",
                "duration_ms": 42,
                "status": "completed",
                "arguments": {
                    "concept_id": "#V#sensitive_entity",
                    "query": "private content should not appear",
                },
            }
        ],
    )

    encoded = json.dumps(trace, default=str)
    assert "private content should not appear" not in encoded
    tool_span = trace["spans"][0]
    assert tool_span["attributes"]["argument_keys"] == ["concept_id", "query"]


def test_tool_timing_separates_queue_handler_transport_and_persistence() -> None:
    trace = build_turn_timing_trace(
        request_id="req-tool-deadline",
        tool_invocations=[
            {
                "tool": "synthetic_grounded_read",
                "execution_id": "mcp_synthetic",
                "duration_ms": 42,
                "status": "timeout",
                "queue_duration_ms": 5,
                "handler_duration_ms": None,
                "handler_elapsed_ms": 35,
                "transport_overhead_ms": 2,
                "timeout_sec": 0.04,
                "advisory_timeout_sec": 0.01,
                "advisory_budget_exceeded": True,
                "timeout_phase": "handler",
            }
        ],
        extra_spans=[
            {
                "span_id": "persist-assistant",
                "stage_id": "response_finalising",
                "operation_kind": "chat_history_persistence",
                "operation_name": "persist_assistant_message",
                "duration_ms": 9,
                "status": "success",
            }
        ],
    )

    tool_span = next(
        span for span in trace["spans"] if span["operation_kind"] == "tool_call"
    )
    assert tool_span["status"] == "timeout"
    assert tool_span["attributes"]["execution_id"] == "mcp_synthetic"
    assert tool_span["attributes"]["queue_duration_ms"] == 5
    assert tool_span["attributes"]["handler_duration_ms"] is None
    assert tool_span["attributes"]["handler_elapsed_ms"] == 35
    assert tool_span["attributes"]["transport_overhead_ms"] == 2
    assert tool_span["attributes"]["timeout_phase"] == "handler"
    assert trace["summary"]["tool_queue_elapsed_ms"] == 5
    assert trace["summary"]["tool_handler_elapsed_ms"] == 35
    assert trace["summary"]["tool_transport_overhead_ms"] == 2
    assert any(
        row["operation_kind"] == "chat_history_persistence"
        and row["operation_name"] == "persist_assistant_message"
        and row["duration_ms"] == 9
        for row in trace["operation_totals"]
    )


def test_merge_timing_spans_deduplicates_and_bounds() -> None:
    merged = merge_timing_spans(
        [{"span_id": "a", "stage_id": "x", "operation_kind": "support", "duration_ms": 1}],
        [
            {"span_id": "a", "stage_id": "x", "operation_kind": "support", "duration_ms": 1},
            {"span_id": "b", "stage_id": "x", "operation_kind": "support", "duration_ms": 2},
        ],
    )

    assert [span["span_id"] for span in merged] == ["a", "b"]
