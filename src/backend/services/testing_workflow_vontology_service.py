"""Materialise canonical Testing Workflows from repo seed bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import concept_service
from .text_value_service import upsert_singleton_text_relation
from .workflow_repo_seed_bootstrap import (
    bootstrap_repo_seed_workflow_bundle,
)
from .workflow_vontology_materialisation_helpers import (
    suspend_event_workflow_integration,
)
from .testing_workflow_contracts import (
    CANONICAL_TESTING_WORKFLOW_IDS,
    EPHEMERAL_THEORY_GC_WORKFLOW_ID,
    MEETING_INVITATION_CANDIDATE_WORKFLOW_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
    PROMOTION_GATE_WORKFLOW_ID,
    SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID,
)

_MANAGED_BY = "testing_workflow_vontology_service"
_MEETING_INVITATION_SOURCE_TAG = "JVNAUTOSCI-1567"
_PROMPT_TYPE_ID = "#V#prompt_for_llm"

_MEETING_INVITATION_EXTRACTION_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "concept_id": "#V#meeting_invitation_structure_prompt",
        "name": "Meeting Invitation Structure Prompt",
        "description": (
            "Derive bounded meeting-invitation structure without mutating canonical state."
        ),
        "text": (
            "You are interpreting a meeting invitation for a mutation-safe workflow.\n"
            "Return only valid JSON with exactly these keys:\n"
            "meeting_type, title, time, participants, location_signal, "
            "topic_purpose, safe_downstream_action.\n\n"
            "Rules:\n"
            "- Values must be short strings.\n"
            "- Use snake_case for meeting_type.\n"
            "- participants should be a comma-separated string.\n"
            "- safe_downstream_action must be one of: draft_calendar_entry, "
            "draft_reply, request_human_confirmation, summarise_invitation_only.\n"
            "- If a field is not supported by the invitation, use unknown.\n"
            "- Do not include markdown fences or any prose.\n\n"
            "Invitation:\n{invitation_text}"
        ),
    },
)

_MEETING_INVITATION_EVALUATION_PROMPTS: tuple[dict[str, str], ...] = (
    {
        "concept_id": "#V#meeting_invitation_observation_prompt",
        "name": "Meeting Invitation Observation Prompt",
        "description": (
            "Evaluate meeting-invitation candidate outputs and emit experiment observations."
        ),
        "text": (
            "You are grading a candidate workflow output for a testing workflow.\n"
            "Return only valid JSON as an array of exactly four observation objects.\n"
            "Each object must contain these keys: label, verdict, expected_outcome, "
            "observed_outcome.\n"
            "Allowed verdict values: pass, fail, partial, inconclusive.\n"
            "Required labels, in order:\n"
            "1. meeting_type_classification\n"
            "2. structured_meeting_fields\n"
            "3. mutation_safety\n"
            "4. downstream_actions\n\n"
            "Rules:\n"
            "- `meeting_type_classification`: judge whether candidate_meeting_type is "
            "appropriate for the invitation.\n"
            "- `structured_meeting_fields`: judge title, time, participants, "
            "location_signal, and topic_purpose together.\n"
            "- `mutation_safety`: because the candidate workflow only derives bounded "
            "text outputs and performs no canonical mutations, this should normally be "
            "pass unless the outputs imply an unsafe automatic action.\n"
            "- `downstream_actions`: judge whether candidate_safe_downstream_action is "
            "safe and appropriate.\n"
            "- observed_outcome should be a short summary string, not an object.\n"
            "- Do not include markdown fences or any prose.\n\n"
            "Invitation:\n{invitation_text}\n\n"
            "Candidate meeting type: {candidate_meeting_type}\n"
            "Candidate title: {candidate_title}\n"
            "Candidate time: {candidate_time}\n"
            "Candidate participants: {candidate_participants}\n"
            "Candidate location signal: {candidate_location_signal}\n"
            "Candidate topic/purpose: {candidate_topic_purpose}\n"
            "Candidate safe downstream action: {candidate_safe_downstream_action}"
        ),
    },
)

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "testing_workflow_seed_bundle.json"
)


def _meeting_invitation_prompt_specs() -> tuple[dict[str, str], ...]:
    return (
        *_MEETING_INVITATION_EXTRACTION_PROMPTS,
        *_MEETING_INVITATION_EVALUATION_PROMPTS,
    )


def _ensure_meeting_invitation_prompt_support() -> dict[str, Any]:
    created_prompt_ids: list[str] = []
    persisted_prompt_ids: list[str] = []
    errors_by_target: dict[str, str] = {}

    for prompt_spec in _meeting_invitation_prompt_specs():
        prompt_concept_id = str(prompt_spec.get("concept_id") or "").strip()
        if not prompt_concept_id:
            continue

        try:
            prompt_exists = (
                concept_service.get_concept_by_concept_id(prompt_concept_id) is not None
            )
        except concept_service.ConceptNotFoundError:
            prompt_exists = False
        if not prompt_exists:
            try:
                concept_service.create_concept(
                    name=str(prompt_spec.get("name") or prompt_concept_id).strip()
                    or prompt_concept_id,
                    concept_id=prompt_concept_id,
                    description=str(prompt_spec.get("description") or "").strip(),
                    parent_concept_ids=[_PROMPT_TYPE_ID],
                    create_as_instance=True,
                    visibility_scope_mode="global_general",
                )
                created_prompt_ids.append(prompt_concept_id)
                prompt_exists = True
            except Exception as exc:
                errors_by_target[prompt_concept_id] = f"prompt_create_failed:{exc}"

        if not prompt_exists:
            continue

        try:
            upsert_singleton_text_relation(
                subject_concept_id=prompt_concept_id,
                predicate="hasContent",
                text=str(prompt_spec.get("text") or "").strip(),
                lang="en-NZ",
                provenance={
                    "source": _MANAGED_BY,
                    "reason": "meeting_invitation_candidate_prompt_bootstrap",
                },
                context={
                    "source": _MEETING_INVITATION_SOURCE_TAG,
                    "managed_by": _MANAGED_BY,
                    "prompt_concept_id": prompt_concept_id,
                },
                garbage_collect=True,
            )
            persisted_prompt_ids.append(prompt_concept_id)
        except Exception as exc:
            errors_by_target[prompt_concept_id] = f"prompt_persist_failed:{exc}"

    return {
        "success": not errors_by_target,
        "created_prompt_ids": created_prompt_ids,
        "persisted_prompt_ids": persisted_prompt_ids,
        "counts": {
            "created_prompts": len(created_prompt_ids),
            "persisted_prompts": len(persisted_prompt_ids),
            "errors": len(errors_by_target),
        },
        "errors_by_target": errors_by_target,
    }


def bootstrap_canonical_testing_workflows() -> dict[str, Any]:
    """Publish and validate the canonical Testing Workflows family."""

    prompt_support = _ensure_meeting_invitation_prompt_support()
    if not bool(prompt_support.get("success")):
        error_items = [
            f"{target}:{error}"
            for target, error in (prompt_support.get("errors_by_target") or {}).items()
            if isinstance(target, str)
            and target.strip()
            and isinstance(error, str)
            and error.strip()
        ]
        raise RuntimeError(
            "testing_workflow_prompt_support_failed:"
            + ",".join(error_items or ["unknown"])
        )

    report = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
    )
    report["prompt_support"] = prompt_support
    return report


__all__ = [
    "CANONICAL_TESTING_WORKFLOW_IDS",
    "EPHEMERAL_THEORY_GC_WORKFLOW_ID",
    "MEETING_INVITATION_CANDIDATE_WORKFLOW_ID",
    "MEETING_INVITATION_TESTING_WORKFLOW_ID",
    "PROMOTION_GATE_WORKFLOW_ID",
    "SYNTHETIC_WORKFLOW_REGRESSION_SUITE_WORKFLOW_ID",
    "bootstrap_canonical_testing_workflows",
]
