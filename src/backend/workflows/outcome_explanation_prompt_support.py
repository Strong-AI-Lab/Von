"""Resolve optional workflow-owned guidance for terminal outcome explanations.

The ordinary-turn model remains responsible for explaining the user outcome.
This module only resolves an optional, represented prompt fragment that can be
projected beside the execution evidence after a workflow was actually invoked.
The fragment is guidance, not evidence about execution, effects, or authority.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..services.prompt_template_service import PromptTemplateService
from ..services.text_value_service import get_preferred_text_for_concept

WORKFLOW_OUTCOME_EXPLANATION_PROMPT_MAP_SCHEMA_VERSION = (
    "workflow_outcome_explanation_prompt_map.v1"
)
WORKFLOW_OUTCOME_EXPLANATION_PROMPT_MAP_PREDICATE_PRECEDENCE = (
    (
        "#V#hasWorkflowOutcomeExplanationPromptMapJson",
        "hasWorkflowOutcomeExplanationPromptMapJson",
        "#V#has_workflow_outcome_explanation_prompt_map_json",
        "has_workflow_outcome_explanation_prompt_map_json",
    ),
)
WORKFLOW_OUTCOME_EXPLANATION_SUPPORT_SCHEMA_VERSION = (
    "workflow_outcome_explanation_support.v1"
)

_SUPPORTED_OUTCOME_KEYS = frozenset(
    {
        "blocked",
        "clarification_required",
        "completed",
        "default",
        "failed",
        "follow_up_required",
        "indeterminate",
        "not_completed",
        "not_started",
        "partial",
        "succeeded",
    }
)
_MAX_PROMPT_CHARS = 8_000


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def normalise_workflow_outcome_explanation_prompt_map(
    value: Any,
) -> dict[str, str] | None:
    """Validate the represented status-to-prompt mapping."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, Mapping):
        return None
    if value.get("schema_version") != (
        WORKFLOW_OUTCOME_EXPLANATION_PROMPT_MAP_SCHEMA_VERSION
    ):
        return None

    raw_prompts = value.get("prompt_concept_ids")
    if not isinstance(raw_prompts, Mapping):
        return None

    prompt_ids: dict[str, str] = {}
    for raw_key, raw_prompt_id in raw_prompts.items():
        clean_key = _clean_text(raw_key)
        key = clean_key.lower() if clean_key else None
        prompt_id = _clean_text(raw_prompt_id)
        if (
            key not in _SUPPORTED_OUTCOME_KEYS
            or prompt_id is None
            or not prompt_id.startswith("#V#")
        ):
            continue
        prompt_ids[key] = prompt_id
    return prompt_ids or None


def resolve_workflow_outcome_explanation_prompt_support(
    workflow_id: str,
) -> dict[str, Any] | None:
    """Resolve represented prompt IDs and bounded bodies for one workflow."""

    clean_workflow_id = _clean_text(workflow_id)
    if clean_workflow_id is None:
        return None
    preferred = get_preferred_text_for_concept(
        clean_workflow_id,
        predicate_precedence=(
            WORKFLOW_OUTCOME_EXPLANATION_PROMPT_MAP_PREDICATE_PRECEDENCE
        ),
        preferred_languages=("en-NZ", "en"),
        limit=200,
    )
    if not isinstance(preferred, Mapping):
        return None
    prompt_ids = normalise_workflow_outcome_explanation_prompt_map(
        preferred.get("text")
    )
    if prompt_ids is None:
        return None

    prompt_service = PromptTemplateService(default_max_chars=_MAX_PROMPT_CHARS)
    resolved_prompts: dict[str, dict[str, str]] = {}
    for outcome_key, prompt_id in prompt_ids.items():
        resolved_prompt_id, prompt_text = prompt_service.resolve_prompt_text(
            [prompt_id],
            fallback=None,
            max_chars=_MAX_PROMPT_CHARS,
        )
        clean_prompt_text = _clean_text(prompt_text)
        if clean_prompt_text is None:
            continue
        resolved_prompts[outcome_key] = {
            "prompt_concept_id": resolved_prompt_id or prompt_id,
            "prompt_text": clean_prompt_text,
        }
    if not resolved_prompts:
        return None

    predicate = _clean_text(preferred.get("predicate"))
    return {
        "schema_version": WORKFLOW_OUTCOME_EXPLANATION_SUPPORT_SCHEMA_VERSION,
        "workflow_id": clean_workflow_id,
        "source": f"text_relation:{predicate}" if predicate else "text_relation",
        "prompts": resolved_prompts,
    }


def select_workflow_outcome_explanation_prompt(
    support: Mapping[str, Any] | None,
    *,
    outcome_key: str,
) -> dict[str, Any] | None:
    """Select exact-status guidance, falling back to the represented default."""

    if not isinstance(support, Mapping):
        return None
    prompts = support.get("prompts")
    if not isinstance(prompts, Mapping):
        return None
    raw_outcome_key = _clean_text(outcome_key)
    clean_outcome_key = raw_outcome_key.lower() if raw_outcome_key else None
    if clean_outcome_key in prompts:
        selected_key = clean_outcome_key
    elif clean_outcome_key in {"completed", "succeeded"}:
        # A generic failure/partial-outcome prompt should not decorate an
        # ordinary success. Workflows may opt in with an explicit success key.
        return None
    else:
        selected_key = "default"
    selected = prompts.get(selected_key)
    if not isinstance(selected, Mapping):
        return None
    prompt_id = _clean_text(selected.get("prompt_concept_id"))
    prompt_text = _clean_text(selected.get("prompt_text"))
    if prompt_id is None or prompt_text is None:
        return None
    return {
        "schema_version": WORKFLOW_OUTCOME_EXPLANATION_SUPPORT_SCHEMA_VERSION,
        "workflow_id": _clean_text(support.get("workflow_id")),
        "outcome_key": clean_outcome_key,
        "matched_prompt_key": selected_key,
        "prompt_concept_id": prompt_id,
        "prompt_text": prompt_text,
        "source": _clean_text(support.get("source")),
        "role": "supplemental_explanation_guidance",
        "evidence_boundary": (
            "This represented prompt is guidance for explaining independently "
            "observed execution evidence. It is not evidence that the workflow "
            "ran, succeeded, failed, changed canonical state, or has authority."
        ),
    }


__all__ = [
    "WORKFLOW_OUTCOME_EXPLANATION_PROMPT_MAP_PREDICATE_PRECEDENCE",
    "WORKFLOW_OUTCOME_EXPLANATION_PROMPT_MAP_SCHEMA_VERSION",
    "WORKFLOW_OUTCOME_EXPLANATION_SUPPORT_SCHEMA_VERSION",
    "normalise_workflow_outcome_explanation_prompt_map",
    "resolve_workflow_outcome_explanation_prompt_support",
    "select_workflow_outcome_explanation_prompt",
]
