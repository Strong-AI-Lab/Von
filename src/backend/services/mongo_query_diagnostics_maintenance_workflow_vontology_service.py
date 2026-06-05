"""Materialise the Mongo query-diagnostics maintenance workflow in Vontology."""

from __future__ import annotations

from typing import Any

from ..workflows import workflow_concept_authority_service as authority_service
from ..workflows.durable.mongo_query_diagnostics_maintenance_workflow import (
    MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,
    build_mongo_query_diagnostics_maintenance_workflow_test_registration,
)
from ..workflows.workflow_registry import WorkflowRegistry

_MANAGED_BY = "mongo_query_diagnostics_maintenance_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2450"


def bootstrap_canonical_mongo_query_diagnostics_maintenance_workflow() -> (
    dict[str, Any]
):
    """Ensure the canonical diagnostic-maintenance workflow graph is published."""

    registry = WorkflowRegistry()
    registry.register(
        build_mongo_query_diagnostics_maintenance_workflow_test_registration()
    )
    report = authority_service.bootstrap_workflow_concepts(
        registry=registry,
        target_workflow_ids=(MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,),
    )
    report = dict(report)
    report["workflow_id"] = MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    errors_by_workflow_id = (report.get("graph_publication") or {}).get(
        "errors_by_workflow_id"
    ) or {}
    report["success"] = MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID not in (
        errors_by_workflow_id
    )
    return report


__all__ = ["bootstrap_canonical_mongo_query_diagnostics_maintenance_workflow"]
