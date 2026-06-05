"""Durable maintenance workflow for Mongo query-targeting diagnostics.

The workflow graph is published into Vontology by the existing workflow concept
authority bootstrap path. Python only provides the bounded action handlers and a
test/publication definition; diagnostic policy remains in the authored workflow
and in the redacted MCP diagnostic surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_mcp_tool_actions import WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID
from ..workflow_registry import WorkflowRegistration

MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID = (
    "#V#mongo_query_diagnostics_maintenance_workflow"
)
MONGO_QUERY_DIAGNOSTICS_TOOL_NAME = "mongo_query_diagnostics_report"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _top_risk_rows(
    report: Mapping[str, Any], *, limit: int = 5
) -> list[dict[str, Any]]:
    rows = report.get("rows")
    if not isinstance(rows, list):
        return []
    top_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        top_rows.append(
            {
                "namespace": str(row.get("namespace") or "").strip(),
                "command_name": str(row.get("command_name") or "").strip(),
                "estimated_waste_score": row.get("estimated_waste_score"),
                "docs_examined_per_returned": row.get("docs_examined_per_returned"),
                "keys_examined_per_returned": row.get("keys_examined_per_returned"),
                "max_duration_ms": row.get("max_duration_ms"),
                "count": row.get("count"),
                "recommended_next_step": row.get("recommended_next_step"),
            }
        )
        if len(top_rows) >= limit:
            break
    return top_rows


def _handle_finalise(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    context = request.data
    diagnostic_report = _mapping(context.get("mongo_query_diagnostics_report"))
    summary = _mapping(diagnostic_report.get("summary"))
    reports = _mapping(diagnostic_report.get("reports"))
    profiler = _mapping(reports.get("profiler"))
    in_process = _mapping(reports.get("in_process"))
    top_rows = _top_risk_rows(summary)

    result = {
        "schema_version": "mongo_query_diagnostics_maintenance_result.v1",
        "success": bool(diagnostic_report.get("success")),
        "status": str(diagnostic_report.get("status") or "unknown"),
        "workflow_id": MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,
        "generated_at_utc": _utc_now_iso(),
        "diagnostic_tool": MONGO_QUERY_DIAGNOSTICS_TOOL_NAME,
        "diagnostic_mode": diagnostic_report.get("mode"),
        "direct_index_mutation": bool(diagnostic_report.get("direct_index_mutation")),
        "database": diagnostic_report.get("database"),
        "options": _mapping(diagnostic_report.get("options")),
        "observed_shape_count": summary.get("observed_shape_count"),
        "ranking": summary.get("ranking"),
        "top_risk_rows": top_rows,
        "profiler_status": profiler.get("status"),
        "in_process_observed_shape_count": in_process.get("observed_shape_count"),
        "recommended_next_step": diagnostic_report.get("recommended_next_step"),
        "privacy": _mapping(diagnostic_report.get("privacy")),
        "attribution_jira": diagnostic_report.get("attribution_jira"),
    }
    return WorkflowActionResult(
        outputs={"mongo_query_diagnostics_maintenance_result": result}
    )


def build_mongo_query_diagnostics_maintenance_workflow_test_definition() -> (
    WorkflowDefinition
):
    collect = WorkflowStateSpec(
        state_id="collect",
        actions=(
            WorkflowActionInvocation(
                action_id=WORKFLOW_MCP_INVOKE_TOOL_ACTION_ID,
                description="Collect redacted Mongo query-targeting diagnostics.",
                inputs={
                    "tool_name": MONGO_QUERY_DIAGNOSTICS_TOOL_NAME,
                    "tool_arguments": {
                        "allow_operator_diagnostics": True,
                        "source": "combined",
                        "sample_limit": 200,
                        "report_limit": 20,
                        "explain_samples": 0,
                    },
                },
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda ctx: bool(ctx.get("last_action_failed")),
                condition_spec={
                    "kind": "context_flag",
                    "key": "last_action_failed",
                    "expected": True,
                },
                reason="on_failure",
            ),
            WorkflowTransitionSpec(
                to_state="finalise",
                condition=lambda ctx: bool(ctx.get("mongo_query_diagnostics_report")),
                condition_spec={
                    "kind": "context_exists",
                    "key": "mongo_query_diagnostics_report",
                },
                reason="diagnostics_ready",
            ),
            WorkflowTransitionSpec(
                to_state="failed",
                condition=lambda _ctx: True,
                condition_spec={"kind": "always"},
                reason="missing_diagnostics",
            ),
        ),
        metadata={
            "workflow_mcp_allowed_tools": [MONGO_QUERY_DIAGNOSTICS_TOOL_NAME],
            "tool_output_context_mappings": [
                {
                    "tool_output_field": "result",
                    "context_key": "mongo_query_diagnostics_report",
                }
            ],
            "writes_context_keys": ["mongo_query_diagnostics_report"],
        },
    )

    finalise = WorkflowStateSpec(
        state_id="finalise",
        actions=(
            WorkflowActionInvocation(
                action_id="mongo_query_diagnostics_maintenance.finalise",
                description="Publish redacted Mongo diagnostic maintenance evidence.",
            ),
        ),
        terminal=True,
    )
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,
        initial_state="collect",
        states={
            "collect": collect,
            "finalise": finalise,
            "failed": failed,
        },
        termination_states=("finalise", "failed"),
        purpose=(
            "Run read-only Mongo query-targeting diagnostics for Von maintenance "
            "and expose redacted evidence for conversation or recurring rumination."
        ),
    )


def build_mongo_query_diagnostics_maintenance_workflow_test_registration() -> (
    WorkflowRegistration
):
    return WorkflowRegistration(
        workflow_id=MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,
        definition=build_mongo_query_diagnostics_maintenance_workflow_test_definition(),
        purpose=(
            "Collect redacted Mongo query-targeting evidence for maintenance "
            "review without mutating indexes."
        ),
        source="built_in",
    )


def register_mongo_query_diagnostics_maintenance_actions(
    registry: ActionRegistry,
) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id="mongo_query_diagnostics_maintenance.finalise",
            handler=_handle_finalise,
            description="Publish Mongo query diagnostic maintenance result evidence.",
            side_effects="none",
            output_schema={
                "type": "object",
                "properties": {
                    "mongo_query_diagnostics_maintenance_result": {"type": "object"}
                },
            },
        )
    )


__all__ = [
    "MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID",
    "MONGO_QUERY_DIAGNOSTICS_TOOL_NAME",
    "build_mongo_query_diagnostics_maintenance_workflow_test_definition",
    "build_mongo_query_diagnostics_maintenance_workflow_test_registration",
    "register_mongo_query_diagnostics_maintenance_actions",
]
