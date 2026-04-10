"""Shared constants for turn-pipeline monitoring workflow authority surfaces."""

from __future__ import annotations

TURN_PIPELINE_MONITORING_WORKFLOW_ID = "#V#turn_pipeline_monitoring_workflow"
TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID = (
    "#V#turn_pipeline_tier1_regression_workflow"
)

CANONICAL_TURN_PIPELINE_MONITORING_WORKFLOW_IDS: tuple[str, ...] = (
    TURN_PIPELINE_MONITORING_WORKFLOW_ID,
    TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
)

__all__ = [
    "CANONICAL_TURN_PIPELINE_MONITORING_WORKFLOW_IDS",
    "TURN_PIPELINE_MONITORING_WORKFLOW_ID",
    "TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID",
]
