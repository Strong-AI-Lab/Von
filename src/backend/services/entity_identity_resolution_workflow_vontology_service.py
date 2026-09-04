"""Materialise the canonical entity identity-resolution workflow authority.

This module publishes the workflow's canonical repo-seed graph, ensures the
LLM rumination prompt concept (``#V#entity_duplicate_reasoning_prompt``) and
its content text relation exist, and links the workflow concept to the prompt
via ``#V#hasEntityDuplicateReasoningPrompt``.

JVNAUTOSCI-2148 - replaces Python heuristic scoring with VWL + Vontology-stored
prompt authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..workflows.durable.entity_identity_resolution_workflow import (
    ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
    ENTITY_DUPLICATE_REASONING_PROMPT_LINK_PREDICATE,
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

_MANAGED_BY = "entity_identity_resolution_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2148"

_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "entity_duplicate_reasoning_prompt_seed.md"
)
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "canonical_workflow_publication_seed_bundle.json"
)


def _load_prompt_seed_text() -> str:
    text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(
            f"entity_duplicate_reasoning_prompt_seed_missing:{_PROMPT_SEED_ASSET_PATH.name}"
        )
    return text


def bootstrap_canonical_entity_identity_resolution_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish the workflow graph and its entity-reasoning prompt authority."""

    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
                name="Entity duplicate reasoning prompt",
                description=(
                    "Canonical LLM prompt for the entity-identity-resolution "
                    "workflow's rumination stage; decides per-pair "
                    "auto_merge / queue_review / leave_distinct / "
                    "insufficient_evidence and merge direction over assembled "
                    "evidence."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
                prompt_concept_id=ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
                predicate=ENTITY_DUPLICATE_REASONING_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="entity_duplicate_reasoning_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_republish or not prompt_concept_has_content(
        ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    seeded_prompt_ready = prompt_concept_has_content(
        ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
    )
    if seeded_prompt_ready and seeded_prompt_ids:
        errors_by_target.pop(ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            pid
            for pid in missing_content_prompt_ids
            if pid != ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID)
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
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["workflow_id"] = ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    report["prompt_concept_id"] = ENTITY_DUPLICATE_REASONING_PROMPT_CONCEPT_ID
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=(ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,),
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    report["publication"] = publication.get("publication")
    report["typed_workflow_ids"] = publication.get("typed_workflow_ids") or []
    report["typed_step_ids"] = publication.get("typed_step_ids") or []
    report["validation_by_workflow_id"] = (
        publication.get("validation_by_workflow_id") or {}
    )
    report["success"] = (
        not errors_by_target
        and seeded_prompt_ready
        and int(publication_counts.get("errors") or 0) == 0
    )
    return report


__all__ = [
    "bootstrap_canonical_entity_identity_resolution_workflow",
]
