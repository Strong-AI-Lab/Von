"""Materialise the Jira task incremental import workflow authority.

The workflow graph for ``#V#jira_task_incremental_import_workflow`` is
Vontology-governed. Python keeps only the reusable action support in
``jira_task_incremental_import_workflow``; this module seeds the represented
workflow graph when the authoritative Vontology materialisation is missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..workflows.durable.jira_task_incremental_import_workflow import (
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "canonical_workflow_publication_seed_bundle.json"
)


def bootstrap_canonical_jira_task_incremental_import_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Ensure the canonical Jira task incremental import workflow is materialised."""

    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": int(publication_counts.get("errors") or 0) == 0,
        "workflow_ids": [JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID],
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id")
        or {},
    }


__all__ = [
    "bootstrap_canonical_jira_task_incremental_import_workflow",
]
