from __future__ import annotations

from src.backend.services.turn_execution_diagnostics_service import (
    build_workflow_execution_trace_mcp_access_refs,
)


def test_workflow_trace_refs_include_selected_workflow_use_episode() -> None:
    refs = build_workflow_execution_trace_mcp_access_refs(
        {
            "workflow_routing": {
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "verdict": "rag_selected",
            },
            "aux_llm_calls": [
                {
                    "type": "workflow_execution_trace",
                    "workflow_id": "#V#chat_narration_workflow",
                    "execution_id": "exec-narration",
                },
                {
                    "type": "workflow_use_episode",
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "workflow_instance_id": "wf-instance-selected",
                    "completed": False,
                },
            ],
        }
    )

    assert refs[0]["trace_role"] == "selected_workflow"
    assert refs[0]["workflow_id"] == "#V#arxiv_paper_representation_workflow"
    assert refs[0]["instance_id"] == "wf-instance-selected"
    assert refs[0]["mcp_access"]["arguments"] == {"instance_id": "wf-instance-selected"}
    assert refs[1]["trace_role"] == "auxiliary_workflow"
    assert refs[1]["workflow_id"] == "#V#chat_narration_workflow"
    assert refs[1]["execution_id"] == "exec-narration"


def test_workflow_trace_refs_include_selected_pre_trace_failure() -> None:
    refs = build_workflow_execution_trace_mcp_access_refs(
        {
            "workflow_routing": {
                "workflow_id": "#V#example_selected_workflow",
                "verdict": "rag_selected",
            },
            "selected_workflow_trace": {
                "selected_workflow_id": "#V#example_selected_workflow",
                "workflow_id": "#V#example_selected_workflow",
                "trace_role": "selected_workflow",
                "trace_unavailable": True,
                "trace_unavailable_reason": "workflow_definition_not_runnable",
                "selected_workflow_pre_trace_failure": {
                    "source": "workflow_instance_submission",
                    "status": "submission_failed",
                    "failure_code": "workflow_definition_not_runnable",
                    "failure_detail": "Verified durable workflow submission failed",
                    "workflow_id": "#V#example_selected_workflow",
                },
            },
        }
    )

    assert refs == [
        {
            "execution_id": None,
            "instance_id": None,
            "workflow_id": "#V#example_selected_workflow",
            "trace_role": "selected_workflow",
            "trace_unavailable": True,
            "trace_unavailable_reason": "workflow_definition_not_runnable",
            "selected_workflow_pre_trace_failure": {
                "source": "workflow_instance_submission",
                "status": "submission_failed",
                "failure_code": "workflow_definition_not_runnable",
                "failure_detail": "Verified durable workflow submission failed",
                "workflow_id": "#V#example_selected_workflow",
            },
        }
    ]
