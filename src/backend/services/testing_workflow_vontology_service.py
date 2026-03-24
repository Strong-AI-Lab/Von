"""Materialise canonical Testing Workflows from repo seed bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)
from .workflow_vontology_materialisation_helpers import (
    suspend_event_workflow_integration,
)
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
)
from .testing_workflow_contracts import (
    CANONICAL_TESTING_WORKFLOW_IDS,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
)

_MANAGED_BY = "testing_workflow_vontology_service"
_MEETING_INVITATION_SOURCE_TAG = "JVNAUTOSCI-1567"

_MEETING_INVITATION_PROMPT_SPECS: tuple[WorkflowPromptConceptSpec, ...] = (
    WorkflowPromptConceptSpec(
        concept_id="#V#meeting_invitation_structure_prompt",
        name="Meeting Invitation Structure Prompt",
        description=(
            "Derive bounded meeting-invitation structure without mutating canonical state."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id="#V#meeting_invitation_observation_prompt",
        name="Meeting Invitation Observation Prompt",
        description=(
            "Evaluate meeting-invitation candidate outputs and emit experiment observations."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
)

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "testing_workflow_seed_bundle.json"
)


def _ensure_meeting_invitation_prompt_support() -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=_MEETING_INVITATION_PROMPT_SPECS,
        provenance_source=_MANAGED_BY,
    )
    report["source"] = _MEETING_INVITATION_SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    return report


def bootstrap_canonical_testing_workflows(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical Testing Workflows family."""

    prompt_support = _ensure_meeting_invitation_prompt_support()
    report = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
    )
    publication_counts = (report.get("publication") or {}).get("counts") or {}
    report["prompt_support"] = prompt_support
    report["success"] = bool(prompt_support.get("success")) and int(
        publication_counts.get("errors") or 0
    ) == 0
    return report


__all__ = [
    "CANONICAL_TESTING_WORKFLOW_IDS",
    "EPHEMERAL_THEORY_GC_WORKFLOW_ID",
    "MEETING_INVITATION_CANDIDATE_WORKFLOW_ID",
    "MEETING_INVITATION_TESTING_WORKFLOW_ID",
    "PROMOTION_GATE_WORKFLOW_ID",
    "SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID",
    "bootstrap_canonical_testing_workflows",
]
