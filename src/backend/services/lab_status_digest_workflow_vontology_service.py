"""Bootstrap represented authority for the lab/project status digest slice."""

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

LAB_STATUS_DIGEST_WORKFLOW_ID = "#V#lab_project_status_digest_workflow"
LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID = "#V#lab_status_digest_prompt"
LAB_STATUS_DIGEST_WORK_PRODUCT_ID = "#V#operational_reliability_status_digest"

_MANAGED_BY = "lab_status_digest_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2590"
_SEED_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "workflows" / "repo_seed_bundles"
)
_REPO_SEED_ASSET_PATH = _SEED_DIRECTORY / "lab_status_digest_workflow_seed_bundle.json"
_PROMPT_SEED_ASSET_PATH = _SEED_DIRECTORY / "lab_status_digest_prompt_seed.md"


def _load_lab_status_digest_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("lab_status_digest_prompt_seed_missing")
    return prompt_text


def _ensure_lab_status_digest_prompt_support(
    *, force_prompt_seed: bool = False
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID,
                name="Lab and project status digest prompt",
                description=(
                    "Canonical prompt for evidence-backed, privacy-bounded status "
                    "digests with durable obligation carry-forward and read-back."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_lab_status_digest_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID)

    result = dict(report)
    result["seeded_prompt_ids"] = seeded_prompt_ids
    result["seeded_prompt_count"] = len(seeded_prompt_ids)
    result["success"] = bool(
        prompt_concept_has_content(LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID)
    )
    return result


def bootstrap_canonical_lab_status_digest_workflow(
    *, force_republish: bool = False
) -> dict[str, Any]:
    """Publish the represented workflow and its stable digest work product."""

    prompt_support = _ensure_lab_status_digest_prompt_support(
        force_prompt_seed=bool(force_republish)
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(LAB_STATUS_DIGEST_WORKFLOW_ID,),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
        "workflow_ids": [LAB_STATUS_DIGEST_WORKFLOW_ID],
        "work_product_ids": [LAB_STATUS_DIGEST_WORK_PRODUCT_ID],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "support_concepts": publication.get("support_concepts") or {},
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id")
        or {},
    }


__all__ = [
    "LAB_STATUS_DIGEST_PROMPT_CONCEPT_ID",
    "LAB_STATUS_DIGEST_WORKFLOW_ID",
    "LAB_STATUS_DIGEST_WORK_PRODUCT_ID",
    "_ensure_lab_status_digest_prompt_support",
    "_load_lab_status_digest_prompt_seed_text",
    "bootstrap_canonical_lab_status_digest_workflow",
]
