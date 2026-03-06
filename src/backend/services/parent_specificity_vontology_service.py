"""Vontology-backed prompt support for parent-specificity rumination.

JVNAUTOSCI-217 keeps parent-specificity analysis prompt text in Vontology so
taxonomy-improvement behaviour can evolve without code edits. The workflow still
has a canonical bootstrap path here so startup can repair missing prompt support
deterministically instead of silently falling back to ad-hoc inline prompts.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .prompt_template_service import PromptTemplateService, RenderedPrompt
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation

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

_CANONICAL_PARENT_SPECIFICITY_PROMPT_TEXT = """You are a careful Vontology taxonomic refinement analyst.

Read the dossier JSON and decide whether the concept can be given a more
specific direct parent (for a type) or a more specific direct type (for an
instance). You must use evidence from all available languages, not just English,
and you must also use taxonomic and other relation evidence.

Return JSON only with this shape:
{
  "decision": "no_change" | "add_existing_parent" | "create_intervening_type",
  "subject_kind": "type" | "instance",
  "recommended_parent_id": "#V#existing_parent" | null,
  "confidence": 0.0,
  "reasons": ["short reason"],
  "evidence": ["short evidence item"],
  "language_signals": ["en-NZ"],
  "new_type": {
    "name": "More specific missing type",
    "concept_id": "#V#optional_concept_id",
    "description": "One or two sentence type description in New Zealand English.",
    "parent_ids": ["#V#existing_parent"]
  } | null
}

Rules:
- Use "no_change" when evidence is ambiguous, weak, contradictory, or merely
  restates the current parent/type.
- Use "add_existing_parent" only when an existing more specific parent/type is
  clearly justified.
- Use "create_intervening_type" only when a missing intermediate type is very
  clearly implied by descriptions and relations.
- Do not recommend "#V#thing".
- Prefer existing ontology concepts whenever possible.
- Confidence must reflect certainty. High-confidence mutation should generally
  mean 0.92 or above.

Dossier JSON:
{analysis_payload_json}
"""


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = _safe_str(value)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(cleaned)
    return tuple(output)


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def resolve_parent_specificity_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    """Resolve the authoritative prompt concept ID for parent-specificity analysis."""

    explicit_id = _safe_str(prompt_concept_id)
    if explicit_id:
        return explicit_id

    workflow_concept_id = _safe_str(workflow_id)
    if workflow_concept_id:
        for predicate in _WORKFLOW_LINK_TEXT_PREDICATES:
            rows = get_texts_for_concept(
                workflow_concept_id,
                predicate=predicate,
                limit=1,
            )
            if not rows:
                continue
            linked_prompt_id = _safe_str((rows[0] or {}).get("text"))
            if linked_prompt_id and linked_prompt_id.startswith("#V#"):
                return linked_prompt_id

    return PARENT_SPECIFICITY_PROMPT_CONCEPT_ID


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
    diagnostics: dict[str, Any] = {
        "requested_workflow_id": _safe_str(workflow_id),
        "requested_prompt_concept_id": _safe_str(prompt_concept_id),
        "resolved_prompt_concept_id": resolved_prompt_id,
        "loaded_prompt_concept_id": None,
        "error": None,
    }
    if not resolved_prompt_id:
        diagnostics["error"] = "parent_specificity_prompt_unresolved"
        return None, diagnostics

    prompt_service = PromptTemplateService(default_max_chars=max(4000, int(max_chars)))
    try:
        rendered = prompt_service.render_prompt(
            [resolved_prompt_id],
            variables=dict(variables or {}),
            fallback=None,
            max_chars=max_chars,
        )
    except Exception as exc:
        diagnostics["error"] = f"parent_specificity_prompt_render_failed:{exc}"
        return None, diagnostics

    if rendered is None:
        diagnostics["error"] = "parent_specificity_prompt_missing_or_empty"
        return None, diagnostics

    diagnostics["loaded_prompt_concept_id"] = (
        _safe_str(rendered.prompt_id) or resolved_prompt_id
    )
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

    requested_workflow_ids = _normalise_strings(workflow_ids)
    prompt_created = False
    prompt_persisted = False
    linked_workflow_ids: list[str] = []
    errors_by_target: dict[str, str] = {}

    prompt_concept = _safe_get_concept(prompt_concept_id)
    if prompt_concept is None:
        try:
            concept_service.create_concept(
                name="Parent specificity analysis prompt",
                concept_id=prompt_concept_id,
                description=(
                    "Canonical LLM prompt for multilingual taxonomic refinement "
                    "analysis in parent-specificity rumination."
                ),
                parent_concept_ids=[PARENT_SPECIFICITY_PROMPT_TYPE_ID],
                create_as_instance=True,
                visibility_scope_mode="global_general",
            )
            prompt_created = True
        except Exception as exc:
            errors_by_target[prompt_concept_id] = f"prompt_create_failed:{exc}"

    if prompt_concept is not None or prompt_created:
        try:
            upsert_singleton_text_relation(
                subject_concept_id=prompt_concept_id,
                predicate=prompt_predicate,
                text=_CANONICAL_PARENT_SPECIFICITY_PROMPT_TEXT,
                lang=language,
                policy=policy,
                garbage_collect=garbage_collect,
                provenance={
                    "source": "parent_specificity_vontology_service",
                    "reason": "canonical_parent_specificity_prompt_bootstrap",
                },
                context={
                    "workflow_id": PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
                    "jira": "JVNAUTOSCI-217",
                },
            )
            prompt_persisted = True
        except Exception as exc:
            errors_by_target[prompt_concept_id] = f"prompt_persist_failed:{exc}"

    for workflow_id in requested_workflow_ids:
        try:
            upsert_singleton_text_relation(
                subject_concept_id=workflow_id,
                predicate=workflow_link_predicate,
                text=prompt_concept_id,
                lang=language,
                policy=policy,
                garbage_collect=garbage_collect,
                provenance={
                    "source": "parent_specificity_vontology_service",
                    "reason": "parent_specificity_prompt_link_bootstrap",
                },
                context={
                    "prompt_concept_id": prompt_concept_id,
                    "jira": "JVNAUTOSCI-217",
                },
            )
            linked_workflow_ids.append(workflow_id)
        except Exception as exc:
            errors_by_target[workflow_id] = f"workflow_prompt_link_failed:{exc}"

    return {
        "success": not errors_by_target,
        "prompt_concept_id": prompt_concept_id,
        "prompt_created": prompt_created,
        "prompt_persisted": prompt_persisted,
        "linked_workflow_ids": linked_workflow_ids,
        "counts": {
            "linked_workflows": len(linked_workflow_ids),
            "errors": len(errors_by_target),
        },
        "errors_by_target": errors_by_target,
    }


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
