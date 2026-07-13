"""Bootstrap prompt and workflow support for generic represented artefact creation."""

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

REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID = "#V#represented_artefact_creation_workflow"
REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID = (
    "#V#represented_artefact_item_creation_workflow"
)
REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID = (
    "#V#prompt_represented_artefact_creation_plan"
)
REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID = (
    "#V#prompt_represented_artefact_set_extraction"
)

_MANAGED_BY = "represented_artefact_creation_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2577"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "represented_artefact_creation_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_represented_artefact_creation_plan_seed.md"
)
_SET_EXTRACTION_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_represented_artefact_set_extraction_seed.md"
)


def _load_represented_artefact_creation_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("represented_artefact_creation_prompt_seed_missing")
    return prompt_text


def _load_represented_artefact_set_extraction_prompt_seed_text() -> str:
    prompt_text = _SET_EXTRACTION_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("represented_artefact_set_extraction_prompt_seed_missing")
    return prompt_text


def _ensure_represented_artefact_creation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
                name="Represented artefact creation planning prompt",
                description=(
                    "Canonical prompt for generic represented artefact creation "
                    "planning, grounded parent/type resolution, and verified reuse."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID,
                name="Represented artefact set extraction prompt",
                description=(
                    "Canonical prompt for deciding whether a represented-artefact "
                    "request names one artefact or a bounded set of artefacts, and "
                    "for producing per-item child workflow requests."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_represented_artefact_creation_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID)
    if force_prompt_seed or not prompt_concept_has_content(
        REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=(REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID),
            predicate="hasContent",
            text=_load_represented_artefact_set_extraction_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID)

    report = dict(report)
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = bool(
        prompt_concept_has_content(REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID)
        and prompt_concept_has_content(
            REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID
        )
    )
    return report


def bootstrap_canonical_represented_artefact_creation_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish the canonical generic represented-artefact creation workflow."""

    prompt_support = _ensure_represented_artefact_creation_prompt_support(
        force_prompt_seed=bool(force_republish),
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(
            REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
            REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
        ),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    support_concepts = publication.get("support_concepts")
    support_errors = []
    if isinstance(support_concepts, dict):
        support_errors = list(support_concepts.get("errors") or [])
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0
        and not support_errors,
        "workflow_ids": [
            REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID,
            REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID,
        ],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "support_concepts": support_concepts,
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = [
    "REPRESENTED_ARTEFACT_CREATION_PROMPT_CONCEPT_ID",
    "REPRESENTED_ARTEFACT_CREATION_WORKFLOW_ID",
    "REPRESENTED_ARTEFACT_ITEM_CREATION_WORKFLOW_ID",
    "REPRESENTED_ARTEFACT_SET_EXTRACTION_PROMPT_CONCEPT_ID",
    "_ensure_represented_artefact_creation_prompt_support",
    "_load_represented_artefact_creation_prompt_seed_text",
    "_load_represented_artefact_set_extraction_prompt_seed_text",
    "bootstrap_canonical_represented_artefact_creation_workflow",
]
