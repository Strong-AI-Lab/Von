"""Materialise represented operational-learning proposal and release authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .benchmark_suite_vontology_service import (
    OPERATIONAL_LEARNING_CANDIDATE_SAFETY_BENCHMARK_SUITE_CONCEPT_ID,
    ensure_canonical_benchmark_suites_from_seed_fixtures,
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
from .workflow_vontology_materialisation_helpers import (
    suspend_event_workflow_integration,
)

OPERATIONAL_LEARNING_CANDIDATE_CONTEXT_WORKFLOW_ID = (
    "#V#operational_learning_candidate_context_resolution_workflow"
)
OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_WORKFLOW_ID = (
    "#V#operational_learning_candidate_proposal_workflow"
)
OPERATIONAL_LEARNING_RELEASE_EVALUATOR_WORKFLOW_ID = (
    "#V#operational_learning_release_evaluator_workflow"
)
OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_WORKFLOW_ID = (
    "#V#operational_learning_candidate_behaviour_evaluation_workflow"
)
OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_PROMPT_ID = (
    "#V#prompt_operational_learning_candidate_proposal"
)
OPERATIONAL_LEARNING_RELEASE_EVALUATOR_PROMPT_ID = (
    "#V#prompt_operational_learning_release_evaluator"
)
OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_PROMPT_ID = (
    "#V#prompt_operational_learning_candidate_behaviour_evaluator"
)

_MANAGED_BY = "operational_learning_release_authority_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2575"
_SEED_DIR = Path(__file__).resolve().parents[1] / "workflows" / "repo_seed_bundles"
_RELEASE_WORKFLOW_BUNDLE_PATH = (
    _SEED_DIR / "operational_learning_release_authority_workflow_seed_bundle.json"
)
_BEHAVIOUR_WORKFLOW_BUNDLE_PATH = (
    _SEED_DIR / "operational_learning_candidate_behaviour_workflow_seed_bundle.json"
)
_PROMPT_ASSET_BY_ID = {
    OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_PROMPT_ID: (
        _SEED_DIR / "operational_learning_candidate_proposal_prompt_seed.md"
    ),
    OPERATIONAL_LEARNING_RELEASE_EVALUATOR_PROMPT_ID: (
        _SEED_DIR / "operational_learning_release_evaluator_prompt_seed.md"
    ),
    OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_PROMPT_ID: (
        _SEED_DIR / "operational_learning_candidate_behaviour_evaluator_prompt_seed.md"
    ),
}

_PROMPT_SPECS = (
    WorkflowPromptConceptSpec(
        concept_id=OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_PROMPT_ID,
        name="Operational learning candidate proposal prompt",
        description=(
            "Represented evidence-bound authoring policy for one immutable "
            "operational-learning candidate proposal."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id=OPERATIONAL_LEARNING_RELEASE_EVALUATOR_PROMPT_ID,
        name="Operational learning release evaluator prompt",
        description=(
            "Represented release-decision policy for one exact candidate and "
            "its independently bound evidence."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
    WorkflowPromptConceptSpec(
        concept_id=OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_PROMPT_ID,
        name="Operational learning candidate behaviour evaluator prompt",
        description=(
            "Represented isolated behavioural-safety evaluation policy for one "
            "exact opaque candidate payload."
        ),
        parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
    ),
)
_PROMPT_LINKS = (
    WorkflowPromptLinkSpec(
        workflow_id=OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_WORKFLOW_ID,
        prompt_concept_id=OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_PROMPT_ID,
        predicate="#V#has_operational_learning_candidate_proposal_prompt",
        context={"jira": _SOURCE_TAG},
        reason="operational_learning_candidate_proposal_prompt_bootstrap",
    ),
    WorkflowPromptLinkSpec(
        workflow_id=OPERATIONAL_LEARNING_RELEASE_EVALUATOR_WORKFLOW_ID,
        prompt_concept_id=OPERATIONAL_LEARNING_RELEASE_EVALUATOR_PROMPT_ID,
        predicate="#V#has_operational_learning_release_evaluator_prompt",
        context={"jira": _SOURCE_TAG},
        reason="operational_learning_release_evaluator_prompt_bootstrap",
    ),
    WorkflowPromptLinkSpec(
        workflow_id=OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_WORKFLOW_ID,
        prompt_concept_id=OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_PROMPT_ID,
        predicate=("#V#has_operational_learning_candidate_behaviour_evaluator_prompt"),
        context={"jira": "JVNAUTOSCI-2578"},
        reason="operational_learning_candidate_behaviour_prompt_bootstrap",
    ),
)


def _ensure_prompts(*, force_prompt_seed: bool) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=_PROMPT_SPECS,
        workflow_links=_PROMPT_LINKS,
        provenance_source=_MANAGED_BY,
    )
    seeded_prompt_ids: list[str] = []
    for prompt_id, asset_path in _PROMPT_ASSET_BY_ID.items():
        if not force_prompt_seed and prompt_concept_has_content(prompt_id):
            continue
        prompt_text = asset_path.read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise ValueError(f"operational_learning_prompt_seed_missing:{prompt_id}")
        upsert_singleton_text_relation(
            subject_concept_id=prompt_id,
            predicate="hasContent",
            text=prompt_text,
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(prompt_id)
    if seeded_prompt_ids:
        report = ensure_prompt_concept_support(
            prompt_specs=_PROMPT_SPECS,
            workflow_links=_PROMPT_LINKS,
            provenance_source=_MANAGED_BY,
        )
    content_ready_by_prompt_id = {
        prompt_id: prompt_concept_has_content(prompt_id)
        for prompt_id in _PROMPT_ASSET_BY_ID
    }
    projection = dict(report)
    projection["seeded_prompt_ids"] = seeded_prompt_ids
    projection["content_ready_by_prompt_id"] = content_ready_by_prompt_id
    projection["success"] = bool(
        all(content_ready_by_prompt_id.values())
        and not projection.get("errors_by_target")
    )
    return projection


def bootstrap_operational_learning_release_authority(
    *,
    force_republish: bool = False,
    overwrite_candidate_safety_suite: bool = False,
) -> dict[str, Any]:
    """Publish represented learning authority and import missing suite state."""

    prompt_support = _ensure_prompts(force_prompt_seed=force_republish)
    release_workflow_publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_RELEASE_WORKFLOW_BUNDLE_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
        target_workflow_ids=(
            OPERATIONAL_LEARNING_CANDIDATE_CONTEXT_WORKFLOW_ID,
            OPERATIONAL_LEARNING_RELEASE_EVALUATOR_WORKFLOW_ID,
            OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_WORKFLOW_ID,
        ),
    )
    behaviour_workflow_publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_BEHAVIOUR_WORKFLOW_BUNDLE_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
        target_workflow_ids=(OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_WORKFLOW_ID,),
    )
    suite_support = ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=(
            OPERATIONAL_LEARNING_CANDIDATE_SAFETY_BENCHMARK_SUITE_CONCEPT_ID,
        ),
        overwrite_existing=overwrite_candidate_safety_suite,
        provenance={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
        context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
    )
    errors: list[str] = []
    if prompt_support.get("success") is not True:
        errors.append("operational_learning_prompt_support_failed")
    for label, publication in (
        ("release", release_workflow_publication),
        ("behaviour", behaviour_workflow_publication),
    ):
        error_count = int(
            ((publication.get("publication") or {}).get("counts") or {}).get("errors")
            or 0
        )
        if error_count:
            errors.append(f"operational_learning_{label}_workflow_publication_failed")
    if suite_support.get("success") is not True:
        errors.append("operational_learning_candidate_safety_suite_support_failed")
    return {
        "success": not errors,
        "schema_version": "operational_learning_release_authority_bootstrap.v1",
        "managed_by": _MANAGED_BY,
        "source_tag": _SOURCE_TAG,
        "prompt_support": prompt_support,
        "release_workflow_publication": release_workflow_publication,
        "behaviour_workflow_publication": behaviour_workflow_publication,
        "candidate_safety_suite_support": suite_support,
        "errors": errors,
    }


__all__ = [
    "OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_PROMPT_ID",
    "OPERATIONAL_LEARNING_CANDIDATE_BEHAVIOUR_WORKFLOW_ID",
    "OPERATIONAL_LEARNING_CANDIDATE_CONTEXT_WORKFLOW_ID",
    "OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_PROMPT_ID",
    "OPERATIONAL_LEARNING_CANDIDATE_PROPOSAL_WORKFLOW_ID",
    "OPERATIONAL_LEARNING_RELEASE_EVALUATOR_PROMPT_ID",
    "OPERATIONAL_LEARNING_RELEASE_EVALUATOR_WORKFLOW_ID",
    "bootstrap_operational_learning_release_authority",
]
