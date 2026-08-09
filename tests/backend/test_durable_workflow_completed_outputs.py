from __future__ import annotations

from src.backend.workflows.durable.failed_output_diagnostics import (
    build_completed_workflow_outputs,
)


def test_completed_outputs_preserve_selected_workflow_user_response() -> None:
    outputs = build_completed_workflow_outputs(
        {
            "result": False,
            "selected_workflow_user_response": "Grounded selected workflow answer.",
            "final_response": "Generic narration answer.",
            "response_text": "Generic narration answer.",
            "current_response": "Generic narration answer.",
        },
        final_state="#V#workflow_step_conversation_turn_execution_workflow_completed",
        result_envelope=None,
        execution_trace_id="trace-2364",
    )

    assert outputs["terminal_output_source"] == "context.result"
    assert outputs["result"] is False
    assert outputs["selected_workflow_user_response"] == (
        "Grounded selected workflow answer."
    )
    assert outputs["final_response"] == "Generic narration answer."
    assert outputs["response"] == "Grounded selected workflow answer."


def test_completed_outputs_use_final_response_when_no_selected_workflow_answer() -> None:
    outputs = build_completed_workflow_outputs(
        {
            "final_response": "Final generated answer.",
            "response_text": "Intermediate answer.",
        },
        final_state="#V#workflow_step_conversation_turn_execution_workflow_completed",
        result_envelope=None,
        execution_trace_id="trace-2364",
    )

    assert outputs["terminal_output_source"] == "workflow_result_envelope"
    assert outputs["final_response"] == "Final generated answer."
    assert outputs["response"] == "Final generated answer."


def test_completed_outputs_expose_content_free_llm_usage_cost_summary() -> None:
    summary = {
        "schema_version": "llm_usage_cost_summary.v1",
        "call_count": 0,
        "usage": {"status": "not_applicable"},
        "estimated_cost": {"status": "not_applicable", "amount": None},
    }

    outputs = build_completed_workflow_outputs(
        {"llm_usage_cost_summary": summary},
        final_state="done",
    )

    assert outputs["llm_usage_cost_summary"] == summary
