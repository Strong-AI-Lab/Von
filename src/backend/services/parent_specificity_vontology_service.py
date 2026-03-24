"""Vontology-backed prompt support for parent-specificity rumination.

JVNAUTOSCI-217 keeps parent-specificity analysis prompt text in Vontology so
taxonomy-improvement behaviour can evolve without code edits. Startup validates
and links the authoritative prompt concept here instead of writing prompt bodies
from Python.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .prompt_template_service import RenderedPrompt
from .workflow_prompt_authority_service import (
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    normalise_strings,
    render_authoritative_prompt,
    resolve_linked_prompt_concept_id,
    safe_str,
)

PARENT_SPECIFICITY_PROMPT_TYPE_ID = "#V#prompt_for_llm"
PARENT_SPECIFICITY_PROMPT_CONCEPT_ID = "#V#parent_specificity_analysis_prompt"
PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE = "#V#has_parent_specificity_prompt"
PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID = (
    "#V#parent_specificity_rumination_workflow"
)

DEFAULT_PARENT_SPECIFICITY_WORKFLOW_IDS: tuple[str, ...] = (
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
)

_WORKFLOW_LINK_TEXT_PREDICATES: tuple[str, ...] = (
    PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE,
    "has_parent_specificity_prompt",
    "hasParentSpecificityPrompt",
)

def resolve_parent_specificity_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    """Resolve the authoritative prompt concept ID for parent-specificity analysis."""

    return resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
        predicates=_WORKFLOW_LINK_TEXT_PREDICATES,
        default_prompt_concept_id=PARENT_SPECIFICITY_PROMPT_CONCEPT_ID,
    )


def render_parent_specificity_prompt(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    """Render the authoritative parent-specificity analysis prompt.

    This helper intentionally has no inline fallback text. Missing prompt support
    is surfaced explicitly so workflow-governed behaviour can fail closed.
    """

    resolved_prompt_id = resolve_parent_specificity_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = render_authoritative_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="parent_specificity",
    )
    diagnostics["requested_workflow_id"] = safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = safe_str(prompt_concept_id)
    return rendered, diagnostics


def ensure_parent_specificity_prompt_support(
    *,
    workflow_ids: Sequence[str] | None = DEFAULT_PARENT_SPECIFICITY_WORKFLOW_IDS,
    prompt_concept_id: str = PARENT_SPECIFICITY_PROMPT_CONCEPT_ID,
    prompt_predicate: str = "hasContent",
    workflow_link_predicate: str = PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Ensure the canonical parent-specificity prompt exists and is linked."""

    requested_workflow_ids = normalise_strings(workflow_ids)
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=prompt_concept_id,
                name="Parent specificity analysis prompt",
                description=(
                    "Canonical LLM prompt for multilingual taxonomic refinement "
                    "analysis in parent-specificity rumination."
                ),
                parent_concept_ids=(PARENT_SPECIFICITY_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=tuple(
            WorkflowPromptLinkSpec(
                workflow_id=workflow_id,
                prompt_concept_id=prompt_concept_id,
                predicate=workflow_link_predicate,
                context={
                    "prompt_concept_id": prompt_concept_id,
                    "jira": "JVNAUTOSCI-217",
                },
                reason="parent_specificity_prompt_link_bootstrap",
            )
            for workflow_id in requested_workflow_ids
        ),
        provenance_source="parent_specificity_vontology_service",
        language=language,
        policy=policy,
        garbage_collect=garbage_collect,
    )
    report["prompt_concept_id"] = prompt_concept_id
    return report


__all__ = [
    "DEFAULT_PARENT_SPECIFICITY_WORKFLOW_IDS",
    "PARENT_SPECIFICITY_PROMPT_CONCEPT_ID",
    "PARENT_SPECIFICITY_PROMPT_LINK_PREDICATE",
    "PARENT_SPECIFICITY_PROMPT_TYPE_ID",
    "PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID",
    "ensure_parent_specificity_prompt_support",
    "render_parent_specificity_prompt",
    "resolve_parent_specificity_prompt_concept_id",
]
