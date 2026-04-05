"""Materialise the canonical actor/critic episode-evaluation workflow family."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
    EPISODE_EVALUATION_WORKFLOW_ID,
    EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
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
from ..workflows.durable.startup import get_instance_manager

_MANAGED_BY = "episode_evaluation_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1605"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_evaluation_workflow_seed_bundle.json"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_evaluation_prompt_seed.md"
)


def _load_episode_evaluation_prompt_seed_text() -> str:
    """Return the non-authoritative bootstrap prompt text for missing content."""

    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("episode_evaluation_prompt_seed_missing")
    return prompt_text


def _ensure_episode_evaluation_prompt_support() -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
                name="Episode critic evaluation prompt",
                description=(
                    "Canonical LLM prompt for workflow-first actor/critic "
                    "evaluation of completed chat turns and durable workflow runs."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=EPISODE_EVALUATION_WORKFLOW_ID,
                prompt_concept_id=EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
                predicate=EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
                context={"jira": _SOURCE_TAG},
                reason="episode_evaluation_prompt_link_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if not prompt_concept_has_content(EPISODE_EVALUATION_PROMPT_CONCEPT_ID):
        upsert_singleton_text_relation(
            subject_concept_id=EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_episode_evaluation_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(EPISODE_EVALUATION_PROMPT_CONCEPT_ID)

    report = dict(report)
    seeded_prompt_ready = prompt_concept_has_content(EPISODE_EVALUATION_PROMPT_CONCEPT_ID)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    if seeded_prompt_ready and seeded_prompt_ids:
        errors_by_target.pop(EPISODE_EVALUATION_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != EPISODE_EVALUATION_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if EPISODE_EVALUATION_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(EPISODE_EVALUATION_PROMPT_CONCEPT_ID)
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
    report["success"] = not errors_by_target and seeded_prompt_ready
    return report


def _ensure_episode_evaluation_event_bindings() -> dict[str, Any]:
    manager = get_instance_manager()
    created_count = 0
    updated_count = 0
    bindings: list[dict[str, Any]] = []

    for event_type in (
        EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
        EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
    ):
        binding, created, updated = manager.upsert_event_binding(
            event_type=event_type,
            workflow_id=EPISODE_EVALUATION_WORKFLOW_ID,
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


def bootstrap_canonical_episode_evaluation_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical episode-evaluation workflow."""

    prompt_support = _ensure_episode_evaluation_prompt_support()
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
    )
    event_bindings = _ensure_episode_evaluation_event_bindings()
    return {
        "success": bool(prompt_support.get("success")) and bool(
            event_bindings.get("success")
        )
        and int((publication.get("publication") or {}).get("counts", {}).get("errors") or 0)
        == 0,
        "workflow_ids": [EPISODE_EVALUATION_WORKFLOW_ID],
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
        "event_bindings": event_bindings,
    }


__all__ = [
    "bootstrap_canonical_episode_evaluation_workflow",
]
