from __future__ import annotations

from src.backend.services.thinking_semantic_projection_service import (
    build_semantic_operation_projection,
)
from src.backend.services.turn_execution_diagnostic_event_service import (
    update_tool_observation_summary,
)


def _semantic_operation(
    *,
    lifecycle_status: str,
    success: bool | None = None,
    result: dict | None = None,
) -> dict:
    return build_semantic_operation_projection(
        operation_id="call-relationship",
        capability_name="add_relationship",
        execution_method="add_relationship",
        capability_kind="registered_tool",
        arguments={
            "source_id": "#V#candidate_situation",
            "predicate": "#V#has_doctoral_supervisor",
            "target": "#V#robert_amor",
        },
        lifecycle_status=lifecycle_status,
        success=success,
        result=result,
    )


def test_tool_history_preserves_terminal_semantic_operation_across_reload() -> None:
    running = _semantic_operation(lifecycle_status="running")
    terminal = _semantic_operation(
        lifecycle_status="succeeded",
        success=True,
        result={"effect_status": "succeeded", "changed": False},
    )

    summary = update_tool_observation_summary(
        None,
        {
            "status": "tool_call_start",
            "event_kind": "tool_call_start",
            "phase": "adaptive_research",
            "tool": "add_relationship",
            "call_id": "call-relationship",
            "result_summary": running["summary"],
            "semantic_operation": running,
        },
    )
    summary = update_tool_observation_summary(
        summary,
        {
            "status": "tool_completed",
            "event_kind": "tool_call_end",
            "phase": "adaptive_research",
            "tool": "add_relationship",
            "call_id": "call-relationship",
            "success": True,
            "result_summary": terminal["summary"],
            "semantic_operation": terminal,
        },
    )
    reloaded = update_tool_observation_summary(summary, None)

    assert reloaded["tool_call_count"] == 1
    assert reloaded["tool_success_count"] == 1
    assert reloaded["tool_pending_count"] == 0
    assert len(reloaded["tool_history"]) == 1
    operation = reloaded["tool_history"][0]["semanticOperation"]
    assert operation["lifecycle_status"] == "succeeded"
    assert operation["outcome"]["changed"] is False
    assert operation["verification"]["status"] == "receipt_only"


def test_tool_history_drops_semantic_operation_with_wrong_visibility() -> None:
    operation = {
        **_semantic_operation(lifecycle_status="running"),
        "visibility": "organisation_scope",
    }

    summary = update_tool_observation_summary(
        None,
        {
            "status": "tool_call_start",
            "event_kind": "tool_call_start",
            "tool": "add_relationship",
            "call_id": "call-relationship",
            "semantic_operation": operation,
        },
    )

    assert "semanticOperation" not in summary["tool_history"][0]
