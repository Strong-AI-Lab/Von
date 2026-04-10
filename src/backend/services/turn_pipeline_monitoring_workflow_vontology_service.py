"""Materialise canonical turn-pipeline monitoring workflows from repo seed bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .turn_pipeline_monitoring_workflow_contracts import (
    CANONICAL_TURN_PIPELINE_MONITORING_WORKFLOW_IDS,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

_MANAGED_BY = "turn_pipeline_monitoring_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1795"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "turn_pipeline_monitoring_workflow_seed_bundle.json"
)


def bootstrap_canonical_turn_pipeline_monitoring_workflows(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical turn-pipeline monitoring workflows."""

    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=CANONICAL_TURN_PIPELINE_MONITORING_WORKFLOW_IDS,
    )
    publication_payload = publication.get("publication")
    publication_counts = dict((publication_payload or {}).get("counts") or {})
    return {
        "success": int(publication_counts.get("errors") or 0) == 0,
        "source": _SOURCE_TAG,
        "managed_by": _MANAGED_BY,
        "workflow_ids": list(CANONICAL_TURN_PIPELINE_MONITORING_WORKFLOW_IDS),
        "publication": publication_payload,
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "bootstrap_canonical_turn_pipeline_monitoring_workflows",
]
