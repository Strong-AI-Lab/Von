"""Materialise the canonical actor/critic episode-evaluation workflow family."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
    EPISODE_EVALUATION_WORKFLOW_ID,
    EPISODE_EVALUATION_WORKFLOW_IDS,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_LINK_PREDICATE,
    EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_CONCEPT_ID,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_LINK_PREDICATE,
    EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
    EVENT_TYPE_TURN_COMPLETION_GATE_FINALISED,
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
)
from .episode_self_improvement_profile_vontology_service import (
    ensure_canonical_episode_self_improvement_profiles,
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
_EPISODE_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_evaluation_workflow_seed_bundle.json"
)
_SELF_IMPROVEMENT_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_self_improvement_workflow_seed_bundle.json"
)
_EPISODE_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_evaluation_prompt_seed.md"
)
_SELF_IMPROVEMENT_PROPOSAL_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_self_improvement_proposal_prompt_seed.md"
)
_SELF_IMPROVEMENT_PROMOTION_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "episode_self_improvement_promotion_prompt_seed.md"
)

_PROMPT_CONFIGS = (
    {
        "concept_id": EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        "workflow_id": EPISODE_EVALUATION_WORKFLOW_ID,
        "predicate": EPISODE_EVALUATION_PROMPT_LINK_PREDICATE,
        "asset_path": _EPISODE_PROMPT_SEED_ASSET_PATH,
        "name": "Episode critic evaluation prompt",
        "description": (
            "Canonical LLM prompt for workflow-first actor/critic evaluation of "
            "completed chat turns and durable workflow runs."
        ),
    },
    {
        "concept_id": EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_CONCEPT_ID,
        "workflow_id": EPISODE_SELF_IMPROVEMENT_PROPOSAL_WORKFLOW_ID,
        "predicate": EPISODE_SELF_IMPROVEMENT_PROPOSAL_PROMPT_LINK_PREDICATE,
        "asset_path": _SELF_IMPROVEMENT_PROPOSAL_PROMPT_SEED_ASSET_PATH,
        "name": "Episode self-improvement proposal prompt",
        "description": (
            "Canonical LLM prompt for critique-driven workflow revision proposal "
            "design before approval."
        ),
    },
    {
        "concept_id": EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_CONCEPT_ID,
        "workflow_id": EPISODE_SELF_IMPROVEMENT_PROMOTION_WORKFLOW_ID,
        "predicate": EPISODE_SELF_IMPROVEMENT_PROMOTION_PROMPT_LINK_PREDICATE,
        "asset_path": _SELF_IMPROVEMENT_PROMOTION_PROMPT_SEED_ASSET_PATH,
        "name": "Episode self-improvement promotion prompt",
        "description": (
            "Canonical LLM prompt for evaluating critique-driven workflow "
            "revision proposals before promotion or review."
        ),
    },
)


def _load_prompt_seed_text(asset_path: Path) -> str:
    """Return the non-authoritative bootstrap prompt text for missing content."""

    prompt_text = asset_path.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError(f"episode_evaluation_prompt_seed_missing:{asset_path.name}")
    return prompt_text


def _ensure_episode_evaluation_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=tuple(
            WorkflowPromptConceptSpec(
                concept_id=str(config["concept_id"]),
                name=str(config["name"]),
                description=str(config["description"]),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            )
            for config in _PROMPT_CONFIGS
        ),
        workflow_links=tuple(
            WorkflowPromptLinkSpec(
                workflow_id=str(config["workflow_id"]),
                prompt_concept_id=str(config["concept_id"]),
                predicate=str(config["predicate"]),
                context={"jira": _SOURCE_TAG},
                reason="episode_evaluation_prompt_link_bootstrap",
            )
            for config in _PROMPT_CONFIGS
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    required_prompt_ids = [str(config["concept_id"]) for config in _PROMPT_CONFIGS]
    for config in _PROMPT_CONFIGS:
        concept_id = str(config["concept_id"])
        if force_prompt_seed or not prompt_concept_has_content(concept_id):
            upsert_singleton_text_relation(
                subject_concept_id=concept_id,
                predicate="hasContent",
                text=_load_prompt_seed_text(Path(str(config["asset_path"]))),
                lang="en-NZ",
                context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
                garbage_collect=True,
            )
            seeded_prompt_ids.append(concept_id)

    report = dict(report)
    seeded_prompt_ready = all(
        prompt_concept_has_content(prompt_id) for prompt_id in required_prompt_ids
    )
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    if seeded_prompt_ready and seeded_prompt_ids:
        for prompt_id in required_prompt_ids:
            errors_by_target.pop(prompt_id, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id not in required_prompt_ids
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        for prompt_id in required_prompt_ids:
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


def _merge_publication_reports(*publication_rows: Mapping[str, Any] | None) -> dict[str, Any]:
    reports = [dict(row) for row in publication_rows if isinstance(row, Mapping)]
    counts: dict[str, int] = {}
    for report in reports:
        for key, value in dict(report.get("counts") or {}).items():
            counts[str(key)] = counts.get(str(key), 0) + int(value or 0)
    return {
        "counts": counts,
        "reports": reports,
    }


def bootstrap_canonical_episode_evaluation_workflow(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical episode-evaluation workflow family."""

    prompt_support = _ensure_episode_evaluation_prompt_support(
        force_prompt_seed=bool(force_republish),
    )
    episode_publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_EPISODE_REPO_SEED_ASSET_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
    )
    self_improvement_publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_SELF_IMPROVEMENT_REPO_SEED_ASSET_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
    )
    self_improvement_profile_support = ensure_canonical_episode_self_improvement_profiles(
        context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
    )
    event_bindings = _ensure_episode_evaluation_event_bindings()
    publication = _merge_publication_reports(
        episode_publication.get("publication"),
        self_improvement_publication.get("publication"),
    )
    return {
        "success": bool(prompt_support.get("success"))
        and bool(self_improvement_profile_support.get("success"))
        and bool(event_bindings.get("success"))
        and int((publication.get("counts") or {}).get("errors") or 0) == 0,
        "workflow_ids": list(EPISODE_EVALUATION_WORKFLOW_IDS),
        "prompt_support": prompt_support,
        "self_improvement_profile_support": self_improvement_profile_support,
        "publication": publication,
        "typed_workflow_ids": [
            *list(episode_publication.get("typed_workflow_ids") or []),
            *list(self_improvement_publication.get("typed_workflow_ids") or []),
        ],
        "typed_step_ids": [
            *list(episode_publication.get("typed_step_ids") or []),
            *list(self_improvement_publication.get("typed_step_ids") or []),
        ],
        "validation_by_workflow_id": {
            **dict(episode_publication.get("validation_by_workflow_id") or {}),
            **dict(self_improvement_publication.get("validation_by_workflow_id") or {}),
        },
        "event_bindings": event_bindings,
    }


__all__ = [
    "bootstrap_canonical_episode_evaluation_workflow",
]
