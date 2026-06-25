from __future__ import annotations

import json

from scripts import replay_latency_report


def _write_artifact(path, progress_history):
    path.write_text(
        json.dumps(
            {
                "status": "error",
                "conversation": {"background_task_id": "task-123"},
                "selection": {"prompt_id": "jira_150"},
                "response": {
                    "failure": {
                        "background_task": {
                            "cancellation_payload": {
                                "post_cancellation_status_payload": {
                                    "progress_history": progress_history
                                }
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_replay_latency_report_ranks_phases_and_llm_prepare_gaps(tmp_path) -> None:
    artifact = tmp_path / "sample.json"
    _write_artifact(
        artifact,
        [
            {
                "recorded_at": "2026-06-24T00:00:00+00:00",
                "phase": "context_build",
                "status": "thinking",
            },
            {
                "recorded_at": "2026-06-24T00:00:10+00:00",
                "phase": "context_build",
                "status": "thinking",
            },
            {
                "recorded_at": "2026-06-24T00:00:11+00:00",
                "phase": "context_adjudication_decision",
                "status": "thinking",
                "subtask": "bounded LLM call",
                "prompt_id": "#V#adjudication_prompt",
                "model_name": "gpt-5.4-nano",
                "provider": "openai",
            },
            {
                "recorded_at": "2026-06-24T00:00:12+00:00",
                "phase": "context_adjudication_decision",
                "status": "llm_request_preparation_step",
                "request_preparation_step": "model_candidate_resolution",
                "duration_ms": 1700,
                "provider": "openai",
            },
            {
                "recorded_at": "2026-06-24T00:00:41+00:00",
                "phase": "context_adjudication_decision",
                "status": "llm_request_prepared",
                "llm_exchange_id": "llm-1",
            },
            {
                "recorded_at": "2026-06-24T00:00:43+00:00",
                "phase": "context_adjudication_decision",
                "status": "llm_call_end",
                "llm_exchange_id": "llm-1",
            },
        ],
    )

    report = replay_latency_report.build_replay_latency_report([artifact])

    assert report["schema_version"] == "replay_latency_report.v1"
    assert report["artifact_count"] == 1
    assert report["phase_totals"][0]["phase"] == "context_adjudication_decision"
    assert report["phase_totals"][0]["total_duration_ms"] == 32000
    assert report["llm_request_preparation_gap_totals"][0]["phase"] == (
        "context_adjudication_decision"
    )
    assert report["llm_request_preparation_gap_totals"][0]["status"] == "prepared"
    assert report["llm_request_preparation_gap_totals"][0]["total_duration_ms"] == 30000
    assert report["llm_request_preparation_step_totals"][0]["phase"] == (
        "context_adjudication_decision"
    )
    assert report["llm_request_preparation_step_totals"][0]["step"] == (
        "model_candidate_resolution"
    )
    assert report["llm_request_preparation_step_totals"][0]["total_duration_ms"] == 1700


def test_replay_latency_report_flags_missing_llm_request_prepared(tmp_path) -> None:
    artifact = tmp_path / "cancelled.json"
    _write_artifact(
        artifact,
        [
            {
                "recorded_at": "2026-06-24T00:00:00+00:00",
                "phase": "selector_decision",
                "status": "thinking",
                "subtask": "bounded LLM call",
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "model_name": "gpt-5.4-nano",
            },
            {
                "recorded_at": "2026-06-24T00:01:40+00:00",
                "phase": "selector_decision",
                "status": "thinking",
                "subtask": "bounded LLM call",
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "model_name": "gpt-5.4-nano",
            },
            {
                "recorded_at": "2026-06-24T00:02:00+00:00",
                "phase": "cancelled",
                "status": "cancelled",
            },
        ],
    )

    report = replay_latency_report.build_replay_latency_report([artifact])

    gap = report["llm_request_preparation_gap_totals"][0]
    assert gap["phase"] == "selector_decision"
    assert gap["status"] == "missing_llm_request_prepared"
    assert gap["total_duration_ms"] == 120000


def test_replay_latency_report_counts_phase_start_prepare_gap(tmp_path) -> None:
    artifact = tmp_path / "tool_plan.json"
    _write_artifact(
        artifact,
        [
            {
                "recorded_at": "2026-06-24T00:00:00+00:00",
                "phase": "tool_plan",
                "status": "phase_transition",
            },
            {
                "recorded_at": "2026-06-24T00:03:27+00:00",
                "phase": "tool_plan",
                "status": "llm_request_prepared",
                "llm_exchange_id": "llm-tool-plan",
            },
            {
                "recorded_at": "2026-06-24T00:03:30+00:00",
                "phase": "tool_plan",
                "status": "llm_call_start",
            },
        ],
    )

    report = replay_latency_report.build_replay_latency_report([artifact])

    gap = report["llm_request_preparation_gap_totals"][0]
    assert gap["phase"] == "tool_plan"
    assert gap["status"] == "prepared_from_phase_start"
    assert gap["total_duration_ms"] == 207000


def test_replay_latency_report_uses_prepared_timestamp_from_call_event(tmp_path) -> None:
    artifact = tmp_path / "call_event_timestamp.json"
    _write_artifact(
        artifact,
        [
            {
                "recorded_at": "2026-06-24T00:00:00+00:00",
                "phase": "selector_decision",
                "status": "thinking",
                "subtask": "bounded LLM call",
                "model_name": "gpt-5.4-nano",
            },
            {
                "recorded_at": "2026-06-24T00:00:35+00:00",
                "phase": "selector_decision",
                "status": "llm_call_end",
                "duration_ms": 5000,
                "llm_request_prepared_at_utc": "2026-06-24T00:00:30+00:00",
            },
        ],
    )

    report = replay_latency_report.build_replay_latency_report([artifact])

    gap = report["llm_request_preparation_gap_totals"][0]
    assert gap["phase"] == "selector_decision"
    assert gap["status"] == "prepared_from_timestamp"
    assert gap["total_duration_ms"] == 30000


def test_render_markdown_is_redacted_and_structural(tmp_path) -> None:
    artifact = tmp_path / "sample.json"
    _write_artifact(
        artifact,
        [
            {
                "recorded_at": "2026-06-24T00:00:00+00:00",
                "phase": "context_build",
                "status": "thinking",
            },
            {
                "recorded_at": "2026-06-24T00:00:01+00:00",
                "phase": "context_build",
                "status": "thinking",
            },
        ],
    )
    report = replay_latency_report.build_replay_latency_report([artifact])

    markdown = replay_latency_report.render_markdown(report)

    assert "| Phase | Artefacts | Total ms | Max ms | Prompts | Models |" in markdown
    assert (
        "| Phase | Step | Artefacts | Total ms | Max ms | Prompts | Models |"
        in markdown
    )
    assert "context_build" in markdown
    assert "task-123" not in markdown
