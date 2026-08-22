"""Seed-only startup publication support for the scholarly-paper workflow family.

The paper repo bundle remains a reviewable bootstrap fixture, not request-time
workflow authority. When the canonical Vontology materialisation is already
current this path is a no-op; when startup detects missing or drifted
materialisation it republishes through the shared seed-bootstrap helper and
returns explicit diagnostics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..workflows.workflow_repo_seed_export_service import (
    diff_repo_seed_workflow_bundle_from_authority,
    write_repo_seed_workflow_bundle_from_authority,
)
from .publication_scope_profile_vontology_service import (
    bootstrap_canonical_publication_scope_profiles,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)

SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID = (
    "#V#scholarly_article_metadata_representation_workflow"
)
SCHOLARLY_PUBLIC_AUTHOR_REPRESENTATION_WORKFLOW_ID = (
    "#V#scholarly_public_author_representation_workflow"
)
SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID = (
    "#V#scholarly_paper_representation_workflow"
)
ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID = "#V#arxiv_paper_representation_workflow"
SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID = (
    "#V#source_neutral_paper_reference_ingestion_workflow"
)
SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID = (
    "#V#source_neutral_paper_reference_item_ingestion_workflow"
)

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_representation_workflow_seed_bundle.json"
)
_MANAGED_BY = "paper_representation_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2189"
_METADATA_EXTRACTION_PROMPT_CONCEPT_ID = (
    "#V#prompt_scholarly_article_metadata_extraction"
)
_REPRESENTATION_EVIDENCE_PROMPT_CONCEPT_ID = (
    "#V#prompt_scholarly_article_representation_evidence_summary"
)
_METADATA_EXTRACTION_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_scholarly_article_metadata_extraction_seed.md"
)
_REPRESENTATION_EVIDENCE_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_scholarly_article_representation_evidence_summary_seed.md"
)


def _load_prompt_seed_text(asset_path: Path, *, error_code: str) -> str:
    prompt_text = asset_path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError(error_code)
    return prompt_text


def _ensure_paper_workflow_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=_METADATA_EXTRACTION_PROMPT_CONCEPT_ID,
                name="Scholarly article metadata extraction prompt",
                description=(
                    "Canonical prompt used by the scholarly article metadata "
                    "representation workflow to extract DOI, title, author, "
                    "abstract, source, date, and topic fields from user-supplied "
                    "bibliographic text without inventing missing metadata."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_REPRESENTATION_EVIDENCE_PROMPT_CONCEPT_ID,
                name="Scholarly article representation evidence summary prompt",
                description=(
                    "Canonical prompt used by the scholarly article metadata "
                    "representation workflow to produce user-facing evidence "
                    "and observations from the materialised article read-back."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    seed_assets = {
        _METADATA_EXTRACTION_PROMPT_CONCEPT_ID: (
            _METADATA_EXTRACTION_PROMPT_SEED_ASSET_PATH,
            "scholarly_metadata_extraction_prompt_seed_missing",
        ),
        _REPRESENTATION_EVIDENCE_PROMPT_CONCEPT_ID: (
            _REPRESENTATION_EVIDENCE_PROMPT_SEED_ASSET_PATH,
            "scholarly_representation_evidence_prompt_seed_missing",
        ),
    }
    for prompt_id, (asset_path, error_code) in seed_assets.items():
        if force_prompt_seed or not prompt_concept_has_content(prompt_id):
            upsert_singleton_text_relation(
                subject_concept_id=prompt_id,
                predicate="hasContent",
                text=_load_prompt_seed_text(asset_path, error_code=error_code),
                lang="en-NZ",
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            seeded_prompt_ids.append(prompt_id)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    for prompt_id in seed_assets:
        if not prompt_concept_has_content(prompt_id):
            continue
        errors_by_target.pop(prompt_id, None)
        missing_content_prompt_ids = [
            missing_prompt_id
            for missing_prompt_id in missing_content_prompt_ids
            if missing_prompt_id != prompt_id
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if prompt_id not in validated_prompt_ids:
            validated_prompt_ids.append(prompt_id)
        report["validated_prompt_ids"] = validated_prompt_ids

    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["counts"] = {
        "created_prompts": len(report.get("created_prompt_ids") or []),
        "validated_prompts": len(report.get("validated_prompt_ids") or []),
        "missing_content_prompts": len(missing_content_prompt_ids),
        "linked_workflows": len(report.get("linked_workflow_ids") or []),
        "errors": len(errors_by_target),
    }
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = not errors_by_target and not missing_content_prompt_ids
    return report


def bootstrap_canonical_paper_representation_workflows(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish or repair the canonical scholarly-paper workflow family at startup."""

    publication_scope_profiles = bootstrap_canonical_publication_scope_profiles()
    prompt_support = _ensure_paper_workflow_prompt_support(
        force_prompt_seed=bool(force_republish)
    )
    report = dict(
        bootstrap_repo_seed_workflow_bundle(
            asset_path=_REPO_SEED_ASSET_PATH,
            force_republish=force_republish,
        )
    )
    publication_counts = dict((report.get("publication") or {}).get("counts") or {})
    report["publication_scope_profiles"] = publication_scope_profiles
    report["prompt_support"] = prompt_support
    report["success"] = (
        bool(publication_scope_profiles.get("success"))
        and bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0
    )
    return report


def export_canonical_paper_representation_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh the paper workflow seed bundle from authoritative Vontology state."""

    return write_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


def diff_canonical_paper_representation_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Diff the paper workflow seed bundle against authoritative Vontology state."""

    return diff_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


__all__ = [
    "ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_ARTICLE_METADATA_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_PAPER_REPRESENTATION_WORKFLOW_ID",
    "SCHOLARLY_PUBLIC_AUTHOR_REPRESENTATION_WORKFLOW_ID",
    "SOURCE_NEUTRAL_PAPER_REFERENCE_INGESTION_WORKFLOW_ID",
    "SOURCE_NEUTRAL_PAPER_REFERENCE_ITEM_INGESTION_WORKFLOW_ID",
    "bootstrap_canonical_paper_representation_workflows",
    "diff_canonical_paper_representation_workflow_repo_seed_bundle",
    "export_canonical_paper_representation_workflow_repo_seed_bundle",
]
