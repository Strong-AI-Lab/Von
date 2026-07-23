"""Bootstrap represented spreadsheet programme workflow authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..db.repositories.concepts_repository import ConceptsRepository
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle
from .kr_materialisation_workflow_vontology_service import (
    bootstrap_canonical_kr_materialisation_workflows,
)


SPREADSHEET_PROGRAMME_WORKFLOW_ID = (
    "#V#spreadsheet_phd_programme_representation_workflow"
)
SPREADSHEET_RECORD_ITEM_WORKFLOW_ID = (
    "#V#spreadsheet_record_representation_item_workflow"
)
SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID = "#V#spreadsheet_programme_plan_prompt"
SPREADSHEET_PROGRAMME_WORKFLOW_IDS = (
    SPREADSHEET_RECORD_ITEM_WORKFLOW_ID,
    SPREADSHEET_PROGRAMME_WORKFLOW_ID,
)
SPREADSHEET_SUPPORT_PARENT_CONCEPT_IDS = (
    "#V#thing",
    "#V#abstract_object",
    "#V#information_object",
    "#V#predicate",
    "#V#binary_predicate",
)

_MANAGED_BY = "spreadsheet_programme_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2592"
_SEED_DIRECTORY = (
    Path(__file__).resolve().parents[1] / "workflows" / "repo_seed_bundles"
)
_REPO_SEED_ASSET_PATH = (
    _SEED_DIRECTORY
    / "spreadsheet_programme_representation_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = (
    _SEED_DIRECTORY / "spreadsheet_programme_plan_prompt_seed.md"
)


def _load_spreadsheet_programme_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("spreadsheet_programme_plan_prompt_seed_missing")
    return prompt_text


def _verify_spreadsheet_support_parent_concepts() -> dict[str, Any]:
    """Fail closed rather than publishing support concepts with orphan parents."""

    expected = set(SPREADSHEET_SUPPORT_PARENT_CONCEPT_IDS)
    errors_by_concept_id: dict[str, str] = {}
    try:
        observed_ids = {
            str(row.get("concept_id") or "").strip()
            for row in ConceptsRepository.find(
                {"concept_id": {"$in": list(SPREADSHEET_SUPPORT_PARENT_CONCEPT_IDS)}},
                {"_id": 0, "concept_id": 1},
                max_time_ms=5_000,
            )
            if isinstance(row, dict) and str(row.get("concept_id") or "").strip()
        }
    except Exception as exc:
        observed_ids = set()
        errors_by_concept_id["support_parent_batch_read"] = type(exc).__name__
    verified = [
        concept_id
        for concept_id in SPREADSHEET_SUPPORT_PARENT_CONCEPT_IDS
        if concept_id in observed_ids
    ]
    missing = [
        concept_id
        for concept_id in SPREADSHEET_SUPPORT_PARENT_CONCEPT_IDS
        if concept_id in expected - observed_ids
    ]
    return {
        "success": not missing and not errors_by_concept_id,
        "verified_concept_ids": verified,
        "missing_concept_ids": missing,
        "errors_by_concept_id": errors_by_concept_id,
    }


def _ensure_spreadsheet_programme_prompt_support(
    *, force_prompt_seed: bool = False
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID,
                name="Spreadsheet programme record planning prompt",
                description=(
                    "Canonical model-planning prompt for untrusted spreadsheet "
                    "table selection, joins, coverage, and representation policy."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )
    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID,
            predicate="hasContent",
            text=_load_spreadsheet_programme_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID)
    result = dict(report)
    result["seeded_prompt_ids"] = seeded_prompt_ids
    result["seeded_prompt_count"] = len(seeded_prompt_ids)
    result["success"] = bool(
        prompt_concept_has_content(SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID)
    )
    return result


def bootstrap_canonical_spreadsheet_programme_workflows(
    *, force_republish: bool = False
) -> dict[str, Any]:
    """Seed or repair the represented workflow family and prompt support."""

    parent_support = _verify_spreadsheet_support_parent_concepts()
    if not parent_support["success"]:
        return {
            "success": False,
            "workflow_ids": list(SPREADSHEET_PROGRAMME_WORKFLOW_IDS),
            "support_parent_concepts": parent_support,
            "error_code": "spreadsheet_support_parent_concepts_unavailable",
            "publication": {
                "counts": {"workflows_published": 0, "errors": 1},
                "skipped": True,
                "skip_reason": "spreadsheet_support_parent_concepts_unavailable",
            },
        }
    prompt_support = _ensure_spreadsheet_programme_prompt_support(
        force_prompt_seed=bool(force_republish)
    )
    kr_dependency = bootstrap_canonical_kr_materialisation_workflows(
        force_republish=force_republish
    )
    report = dict(
        bootstrap_repo_seed_workflow_bundle(
            asset_path=_REPO_SEED_ASSET_PATH,
            force_republish=force_republish,
            target_workflow_ids=SPREADSHEET_PROGRAMME_WORKFLOW_IDS,
        )
    )
    publication_counts = dict((report.get("publication") or {}).get("counts") or {})
    report["workflow_ids"] = list(SPREADSHEET_PROGRAMME_WORKFLOW_IDS)
    report["support_parent_concepts"] = parent_support
    report["prompt_support"] = prompt_support
    report["kr_materialisation_dependency"] = kr_dependency
    report["success"] = (
        bool(prompt_support.get("success"))
        and bool(kr_dependency.get("success"))
        and int(publication_counts.get("errors") or 0) == 0
    )
    return report


__all__ = [
    "SPREADSHEET_PROGRAMME_PLAN_PROMPT_ID",
    "SPREADSHEET_SUPPORT_PARENT_CONCEPT_IDS",
    "SPREADSHEET_PROGRAMME_WORKFLOW_ID",
    "SPREADSHEET_PROGRAMME_WORKFLOW_IDS",
    "SPREADSHEET_RECORD_ITEM_WORKFLOW_ID",
    "_ensure_spreadsheet_programme_prompt_support",
    "_load_spreadsheet_programme_prompt_seed_text",
    "_verify_spreadsheet_support_parent_concepts",
    "bootstrap_canonical_spreadsheet_programme_workflows",
]
