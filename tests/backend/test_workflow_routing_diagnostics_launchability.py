from src.backend.services.turn_execution_record_service import (
    build_workflow_routing_diagnostics,
)


def test_build_workflow_routing_diagnostics_preserves_pre_dispatch_step_outcomes():
    diagnostics = build_workflow_routing_diagnostics(
        workflow_discovery=None,
        workflow_routing={
            "workflow_id": "#V#tool_calling_workflow",
            "verdict": "tool_seeking",
            "source": "selector",
        },
        turn_execution_diagnostics=None,
        aux_llm_calls=[
            {
                "type": "workflow_dispatch_prepare_step",
                "step_id": "launchability_probe",
                "step_label": (
                    "Evaluate launch requirements for ArXiv paper representation workflow"
                ),
                "status": "failed",
                "duration_ms": 6,
                "result_summary": "Missing required context key: file_copy_concept_id",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "workflow_name": "ArXiv paper representation workflow",
                "reason_code": "context_key_present",
                "symbol": "file_copy_concept_id",
                "workflow_launch_input_resolution_status": "resolved",
                "unresolved_required_inputs": [],
                "error": "Missing required context key: file_copy_concept_id",
            },
            {
                "type": "workflow_dispatch_prepare_step",
                "step_id": "safe_general_fallback",
                "step_label": "Use safe general fallback",
                "status": "completed",
                "duration_ms": 0,
                "result_summary": (
                    "ArXiv paper representation workflow could not launch from current "
                    "turn inputs; using the general tool workflow instead."
                ),
                "workflow_id": "#V#tool_calling_workflow",
                "workflow_name": "Tool calling workflow",
            },
        ],
    )

    pre_dispatch = diagnostics["dispatch"]["pre_dispatch"]
    assert pre_dispatch["failed_step_count"] == 1
    assert pre_dispatch["completed_step_count"] == 1
    assert pre_dispatch["steps"][0]["result_summary"] == (
        "Missing required context key: file_copy_concept_id"
    )
    assert pre_dispatch["steps"][0]["reason_code"] == "context_key_present"
    assert pre_dispatch["steps"][0]["symbol"] == "file_copy_concept_id"
    assert pre_dispatch["steps"][1]["result_summary"] == (
        "ArXiv paper representation workflow could not launch from current turn "
        "inputs; using the general tool workflow instead."
    )
    assert pre_dispatch["steps"][1]["workflow_name"] == "Tool calling workflow"
