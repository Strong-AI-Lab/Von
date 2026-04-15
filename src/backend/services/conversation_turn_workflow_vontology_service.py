"""Materialise the canonical conversation-turn workflow family."""

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
from ..workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
)

_MANAGED_BY = "conversation_turn_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-1770"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "canonical_workflow_publication_seed_bundle.json"
)
_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID = (
    "#V#prompt_turn_execution_expected_outcome_inference"
)
_SELECTOR_PROMPT_CONCEPT_ID = "#V#chat_turn_classifier_prompt"
_NARRATION_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_narrate_completion_report"
_RECOVERY_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_recovery_decision"
_EXPECTED_OUTCOME_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_expected_outcome_inference_seed.md"
)
_SELECTOR_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_chat_turn_classifier_seed.md"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_narrate_completion_report_seed.md"
)
_RECOVERY_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_recovery_decision_seed.md"
)
_TARGET_WORKFLOW_IDS: tuple[str, ...] = (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
)


def _load_narration_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("turn_execution_narration_prompt_seed_missing")
    return prompt_text


def _load_expected_outcome_prompt_seed_text() -> str:
    prompt_text = _EXPECTED_OUTCOME_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("turn_execution_expected_outcome_prompt_seed_missing")
    return prompt_text


def _load_selector_prompt_seed_text() -> str:
    prompt_text = _SELECTOR_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("chat_turn_classifier_prompt_seed_missing")
    return prompt_text


def _load_recovery_prompt_seed_text() -> str:
    prompt_text = _RECOVERY_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("turn_execution_recovery_prompt_seed_missing")
    return prompt_text


def _ensure_conversation_turn_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID,
                name="Turn expected-outcome inference prompt",
                description=(
                    "Canonical early-turn inference prompt for deriving the "
                    "grounded success contract that should shape workflow "
                    "selection, omission policy, and direct-answer behaviour."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_SELECTOR_PROMPT_CONCEPT_ID,
                name="Chat turn classifier prompt",
                description=(
                    "Canonical workflow-selector prompt for conversation turns. "
                    "The selector receives the full turn context as LLM context "
                    "messages and chooses the best workflow from the candidate set."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_NARRATION_PROMPT_CONCEPT_ID,
                name="Turn execution completion-report narration prompt",
                description=(
                    "Canonical conversation-turn narration prompt for composing "
                    "the user-facing answer from selected-workflow result "
                    "content, using completion-report data only as supporting "
                    "evidence."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_RECOVERY_PROMPT_CONCEPT_ID,
                name="Turn execution recovery decision prompt",
                description=(
                    "Canonical recovery prompt for choosing the next best "
                    "bounded executable turn-next-action from accumulated turn "
                    "evidence, including a workflow retry, a direct bounded "
                    "tool batch, a direct grounded answer, or an explicit "
                    "follow-up response when no further automated route is "
                    "likely to help."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if force_prompt_seed or not prompt_concept_has_content(
        _EXPECTED_OUTCOME_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_expected_outcome_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID)
    if force_prompt_seed or not prompt_concept_has_content(_SELECTOR_PROMPT_CONCEPT_ID):
        upsert_singleton_text_relation(
            subject_concept_id=_SELECTOR_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_selector_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_SELECTOR_PROMPT_CONCEPT_ID)
    if force_prompt_seed or not prompt_concept_has_content(
        _NARRATION_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_NARRATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_narration_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_NARRATION_PROMPT_CONCEPT_ID)
    if force_prompt_seed or not prompt_concept_has_content(_RECOVERY_PROMPT_CONCEPT_ID):
        upsert_singleton_text_relation(
            subject_concept_id=_RECOVERY_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_recovery_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_RECOVERY_PROMPT_CONCEPT_ID)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    if prompt_concept_has_content(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _EXPECTED_OUTCOME_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _EXPECTED_OUTCOME_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_SELECTOR_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_SELECTOR_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _SELECTOR_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _SELECTOR_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_SELECTOR_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_NARRATION_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_NARRATION_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _NARRATION_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _NARRATION_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_NARRATION_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_RECOVERY_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_RECOVERY_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _RECOVERY_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _RECOVERY_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_RECOVERY_PROMPT_CONCEPT_ID)
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


def bootstrap_canonical_conversation_turn_workflows(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical conversation-turn workflow family."""

    prompt_support = _ensure_conversation_turn_prompt_support(
        force_prompt_seed=bool(force_republish),
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=_TARGET_WORKFLOW_IDS,
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
        "workflow_ids": list(_TARGET_WORKFLOW_IDS),
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = ["bootstrap_canonical_conversation_turn_workflows"]
