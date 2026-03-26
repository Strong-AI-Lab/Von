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
    ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID,
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

_REPO_SEED_ASSET_PATHS = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "testing_workflow_seed_bundle.json",
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "arxiv_paper_ingestion_testing_workflow_seed_bundle.json",
)


def _merge_bundle_publication_reports(
    bundle_reports: list[dict[str, Any]],
    *,
    force_republish: bool,
) -> tuple[dict[str, Any], list[str], list[str]]:
    counts: dict[str, int] = {}
    typed_workflow_ids: list[str] = []
    typed_step_ids: list[str] = []
    skip_reasons: list[str] = []
    all_skipped = bool(bundle_reports)

    for report in bundle_reports:
        publication = report.get("publication") or {}
        publication_counts = publication.get("counts") or {}
        for raw_key, raw_value in publication_counts.items():
            key = str(raw_key).strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + int(raw_value or 0)

        typed_workflow_ids.extend(report.get("typed_workflow_ids") or [])
        typed_step_ids.extend(report.get("typed_step_ids") or [])

        if publication.get("skipped") is True:
            reason = str(publication.get("skip_reason") or "").strip()
            if reason:
                skip_reasons.append(reason)
        else:
            all_skipped = False

    merged_publication: dict[str, Any] = {
        "bundle_reports": bundle_reports,
        "counts": counts,
        "forced_republish": bool(force_republish),
    }
    if all_skipped:
        merged_publication["skipped"] = True
        if len(set(skip_reasons)) == 1 and skip_reasons:
            merged_publication["skip_reason"] = skip_reasons[0]
        elif skip_reasons:
            merged_publication["skip_reason"] = "all_bundles_current"

    return (
        merged_publication,
        list(dict.fromkeys(typed_workflow_ids)),
        list(dict.fromkeys(typed_step_ids)),
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
    bundle_reports: list[dict[str, Any]] = []
    for asset_path in _REPO_SEED_ASSET_PATHS:
        bundle_reports.append(
            bootstrap_repo_seed_workflow_bundle(
                asset_path=asset_path,
                publish_context_manager_factory=suspend_event_workflow_integration,
                force_republish=force_republish,
            )
        )
    publication, typed_workflow_ids, typed_step_ids = _merge_bundle_publication_reports(
        bundle_reports,
        force_republish=force_republish,
    )
    publication_counts = publication.get("counts") or {}
    report: dict[str, Any] = {
        "publication": publication,
        "prompt_support": prompt_support,
        "typed_workflow_ids": typed_workflow_ids,
        "typed_step_ids": typed_step_ids,
    }
    report["success"] = bool(prompt_support.get("success")) and int(
        publication_counts.get("errors") or 0
    ) == 0
    return report


__all__ = [
    "ARXIV_PAPER_INGESTION_TESTING_WORKFLOW_ID",
    "CANONICAL_TESTING_WORKFLOW_IDS",
    "EPHEMERAL_THEORY_GC_WORKFLOW_ID",
    "MEETING_INVITATION_CANDIDATE_WORKFLOW_ID",
    "MEETING_INVITATION_TESTING_WORKFLOW_ID",
    "PROMOTION_GATE_WORKFLOW_ID",
    "SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID",
    "bootstrap_canonical_testing_workflows",
]
