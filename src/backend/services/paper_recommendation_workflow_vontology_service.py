"""Bootstrap and trigger support for canonical paper recommendation workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .paper_recommendation_constants import (
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_DELIVERY_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_RATIONALE_PROMPT_LINK_PREDICATE_ID,
    PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE,
    PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
    PAPER_RECOMMENDATION_WORKFLOW_ID,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_event_integration_service import (
    EVENT_TYPE_RELATIONSHIP_ADDED,
    EVENT_TYPE_RELATIONSHIP_REMOVED,
    EVENT_TYPE_TEXT_RELATION_UPDATED,
    EVENT_TYPE_TEXT_RELATION_UPSERTED,
    launch_event_workflow,
)
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
from ..workflows.durable.startup import get_instance_manager

_MANAGED_BY = "paper_recommendation_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1679"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_recommendation_workflow_seed_bundle.json"
)
_RERANK_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_recommendation_rerank_prompt_seed.md"
)
_DELIVERY_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_recommendation_delivery_message_prompt_seed.md"
)
_RATIONALE_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "paper_recommendation_rationale_prompt_seed.md"
)


def _load_paper_recommendation_prompt_seed_text() -> str:
    """Return the non-authoritative bootstrap prompt text for missing content."""

    prompt_text = _RERANK_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("paper_recommendation_rerank_prompt_seed_missing")
    return prompt_text


def _load_paper_recommendation_delivery_prompt_seed_text() -> str:
    prompt_text = _DELIVERY_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("paper_recommendation_delivery_prompt_seed_missing")
    return prompt_text


def _load_paper_recommendation_rationale_prompt_seed_text() -> str:
    prompt_text = _RATIONALE_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("paper_recommendation_rationale_prompt_seed_missing")
    return prompt_text


def _ensure_paper_recommendation_prompt_support() -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
                name="Paper recommendation reranker prompt",
                description=(
                    "Canonical prompt for multilingual semantic reranking of "
                    "candidate scholarly papers against generic recommendation "
                    "subject concepts."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
                name="Paper recommendation delivery message prompt",
                description=(
                    "Canonical message template for delivering newly active paper "
                    "recommendations to researcher Von users."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
                name="Paper recommendation rationale prompt",
                description=(
                    "Canonical prompt for generating user-facing paragraph "
                    "rationales for one scholarly paper recommendation against a "
                    "specific Von subject profile."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
                prompt_concept_id=PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
                predicate=PAPER_RECOMMENDATION_PROMPT_LINK_PREDICATE_ID,
                context={"jira": _SOURCE_TAG},
                reason="paper_recommendation_prompt_link_bootstrap",
            ),
            WorkflowPromptLinkSpec(
                workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
                prompt_concept_id=PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
                predicate=PAPER_RECOMMENDATION_DELIVERY_PROMPT_LINK_PREDICATE_ID,
                context={"jira": _SOURCE_TAG},
                reason="paper_recommendation_delivery_prompt_link_bootstrap",
            ),
            WorkflowPromptLinkSpec(
                workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
                prompt_concept_id=PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
                predicate=PAPER_RECOMMENDATION_RATIONALE_PROMPT_LINK_PREDICATE_ID,
                context={"jira": _SOURCE_TAG},
                reason="paper_recommendation_rationale_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    seed_specs = (
        (
            PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID,
            _load_paper_recommendation_prompt_seed_text,
        ),
        (
            PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID,
            _load_paper_recommendation_delivery_prompt_seed_text,
        ),
        (
            PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID,
            _load_paper_recommendation_rationale_prompt_seed_text,
        ),
    )
    for prompt_concept_id, prompt_loader in seed_specs:
        if prompt_concept_has_content(prompt_concept_id):
            continue
        upsert_singleton_text_relation(
            subject_concept_id=prompt_concept_id,
            predicate="hasContent",
            text=prompt_loader(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(prompt_concept_id)

    report = dict(report)
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = bool(
        prompt_concept_has_content(PAPER_RECOMMENDATION_RERANK_PROMPT_CONCEPT_ID)
    ) and bool(
        prompt_concept_has_content(PAPER_RECOMMENDATION_DELIVERY_PROMPT_CONCEPT_ID)
    ) and bool(
        prompt_concept_has_content(PAPER_RECOMMENDATION_RATIONALE_PROMPT_CONCEPT_ID)
    )
    return report


def _ensure_paper_recommendation_event_bindings() -> dict[str, Any]:
    manager = get_instance_manager()
    created_count = 0
    updated_count = 0
    bindings: list[dict[str, Any]] = []
    for event_type in (
        PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE,
        EVENT_TYPE_RELATIONSHIP_ADDED,
        EVENT_TYPE_RELATIONSHIP_REMOVED,
        EVENT_TYPE_TEXT_RELATION_UPSERTED,
        EVENT_TYPE_TEXT_RELATION_UPDATED,
    ):
        binding, created, updated = manager.upsert_event_binding(
            event_type=event_type,
            workflow_id=PAPER_RECOMMENDATION_WORKFLOW_ID,
            input_mapping={},
            enabled=True,
            actor=_MANAGED_BY,
            replace_existing=True,
        )
        created_count += 1 if created else 0
        updated_count += 1 if updated else 0
        bindings.append(binding.to_status_dict())
    return {
        "success": True,
        "created_count": created_count,
        "updated_count": updated_count,
        "binding_count": len(bindings),
        "bindings": bindings,
    }


def bootstrap_canonical_paper_recommendation_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish prompt support, workflow authority, and event bindings."""

    prompt_support = _ensure_paper_recommendation_prompt_support()
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
    )
    event_bindings = _ensure_paper_recommendation_event_bindings()
    return {
        "success": bool(prompt_support.get("success"))
        and bool(event_bindings.get("success"))
        and int((publication.get("publication") or {}).get("counts", {}).get("errors") or 0)
        == 0,
        "workflow_ids": [PAPER_RECOMMENDATION_WORKFLOW_ID],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
        "event_bindings": event_bindings,
    }


def request_paper_recommendation_refresh(
    *,
    target_subject_concept_ids: Sequence[str] | None = None,
    candidate_paper_concept_ids: Sequence[str] | None = None,
    trigger_source: str,
    user_id: str | None = None,
    org_id: str | None = None,
    event_payload: Mapping[str, Any] | None = None,
    candidate_limit: int | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    """Launch the canonical paper recommendation workflow for a refresh request."""

    payload = dict(event_payload or {})
    payload.setdefault("target_subject_concept_ids", list(target_subject_concept_ids or []))
    payload.setdefault("candidate_paper_concept_ids", list(candidate_paper_concept_ids or []))
    payload.setdefault("trigger_source", trigger_source)
    inputs = {
        "target_subject_concept_ids": list(target_subject_concept_ids or []),
        "candidate_paper_concept_ids": list(candidate_paper_concept_ids or []),
        "trigger_source": trigger_source,
    }
    if candidate_limit is not None:
        inputs["candidate_limit"] = int(candidate_limit)
    if max_results is not None:
        inputs["max_results"] = int(max_results)
    return launch_event_workflow(
        event_type=PAPER_RECOMMENDATION_REQUESTED_EVENT_TYPE,
        event_id=payload.get("event_id")
        or f"{trigger_source}:{','.join(inputs['target_subject_concept_ids'])}:{','.join(inputs['candidate_paper_concept_ids'])}",
        user_id=user_id,
        org_id=org_id,
        event_payload=payload,
        inputs=inputs,
    )


__all__ = [
    "bootstrap_canonical_paper_recommendation_workflow",
    "request_paper_recommendation_refresh",
]
