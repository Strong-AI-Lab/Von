"""Bootstrap support for canonical academic-roster represented workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

ACADEMIC_ROSTER_WORKFLOW_ID = "#V#academic_roster_reconciliation_workflow"
ACADEMIC_APPOINTMENT_WORKFLOW_ID = "#V#academic_appointment_representation_workflow"
ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID = "#V#prompt_academic_roster_acquisition"

_MANAGED_BY = "academic_roster_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2679"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "academic_roster_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_academic_roster_acquisition_seed.md"
)


def _load_academic_roster_acquisition_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("academic_roster_acquisition_prompt_seed_missing")
    return prompt_text


def _ensure_academic_roster_prompt_support(
    *, force_prompt_seed: bool = False
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID,
                name="Academic roster acquisition prompt",
                description=(
                    "Canonical source-neutral prompt for acquiring bounded academic "
                    "rosters while preserving cohort semantics and evidence."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )
    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_academic_roster_acquisition_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID)
    result = dict(report)
    result["seeded_prompt_ids"] = seeded_prompt_ids
    result["seeded_prompt_count"] = len(seeded_prompt_ids)
    result["success"] = bool(
        prompt_concept_has_content(ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID)
    )
    return result


def bootstrap_canonical_academic_roster_workflows(
    *, force_republish: bool = False
) -> dict[str, Any]:
    """Publish the roster parent and reusable per-person child workflows."""

    prompt_support = _ensure_academic_roster_prompt_support(
        force_prompt_seed=bool(force_republish)
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(
            ACADEMIC_ROSTER_WORKFLOW_ID,
            ACADEMIC_APPOINTMENT_WORKFLOW_ID,
        ),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    support_concepts = publication.get("support_concepts")
    support_errors = (
        list(support_concepts.get("errors") or [])
        if isinstance(support_concepts, dict)
        else []
    )
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0
        and not support_errors,
        "workflow_ids": [
            ACADEMIC_ROSTER_WORKFLOW_ID,
            ACADEMIC_APPOINTMENT_WORKFLOW_ID,
        ],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "support_concepts": support_concepts,
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "ACADEMIC_APPOINTMENT_WORKFLOW_ID",
    "ACADEMIC_ROSTER_ACQUISITION_PROMPT_CONCEPT_ID",
    "ACADEMIC_ROSTER_WORKFLOW_ID",
    "_ensure_academic_roster_prompt_support",
    "_load_academic_roster_acquisition_prompt_seed_text",
    "bootstrap_canonical_academic_roster_workflows",
]
