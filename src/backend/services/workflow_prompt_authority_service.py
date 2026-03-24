"""Shared Vontology-first prompt support for workflow-governed behaviour.

This module keeps prompt bodies authoritative in Vontology while letting Python
retain only generic prompt resolution, validation, and workflow-link support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .prompt_template_service import PromptTemplateService, RenderedPrompt
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation

DEFAULT_PROMPT_TYPE_ID = "#V#prompt_for_llm"
DEFAULT_PROMPT_CONTENT_PREDICATES: tuple[str, ...] = ("hasContent", "#V#hasContent")


@dataclass(frozen=True)
class WorkflowPromptConceptSpec:
    concept_id: str
    name: str
    description: str
    parent_concept_ids: tuple[str, ...] = (DEFAULT_PROMPT_TYPE_ID,)
    create_as_instance: bool = True
    visibility_scope_mode: str = "global_general"
    require_content: bool = True


@dataclass(frozen=True)
class WorkflowPromptLinkSpec:
    workflow_id: str
    prompt_concept_id: str
    predicate: str
    context: Mapping[str, Any] | None = None
    reason: str = "workflow_prompt_link_bootstrap"


def safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def normalise_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return ()
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        cleaned = safe_str(value)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        output.append(cleaned)
    return tuple(output)


def safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def prompt_concept_has_content(
    concept_id: str,
    *,
    predicate_aliases: Sequence[str] = DEFAULT_PROMPT_CONTENT_PREDICATES,
) -> bool:
    prompt_id = safe_str(concept_id)
    if not prompt_id:
        return False
    try:
        rows = get_texts_for_concept(
            subject_concept_id=prompt_id,
            limit=16,
        )
    except Exception:
        return False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        predicate = safe_str(row.get("predicate"))
        if predicate_aliases and predicate not in predicate_aliases:
            continue
        text = safe_str(row.get("text"))
        if text:
            return True
    return False


def resolve_linked_prompt_concept_id(
    *,
    workflow_id: str | None,
    prompt_concept_id: str | None,
    predicates: Sequence[str],
    default_prompt_concept_id: str | None = None,
) -> str | None:
    explicit_id = safe_str(prompt_concept_id)
    if explicit_id:
        return explicit_id

    workflow_concept_id = safe_str(workflow_id)
    if workflow_concept_id:
        for predicate in predicates:
            rows = get_texts_for_concept(
                workflow_concept_id,
                predicate=predicate,
                limit=1,
            )
            if not rows:
                continue
            linked_prompt_id = safe_str((rows[0] or {}).get("text"))
            if linked_prompt_id and linked_prompt_id.startswith("#V#"):
                return linked_prompt_id

    return safe_str(default_prompt_concept_id)


def render_authoritative_prompt(
    *,
    resolved_prompt_id: str | None,
    variables: Mapping[str, Any] | None,
    max_chars: int,
    error_prefix: str,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    diagnostics: dict[str, Any] = {
        "resolved_prompt_concept_id": resolved_prompt_id,
        "loaded_prompt_concept_id": None,
        "error": None,
    }
    if not resolved_prompt_id:
        diagnostics["error"] = f"{error_prefix}_prompt_unresolved"
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
        diagnostics["error"] = f"{error_prefix}_prompt_render_failed:{exc}"
        return None, diagnostics

    if rendered is None:
        diagnostics["error"] = f"{error_prefix}_prompt_missing_or_empty"
        return None, diagnostics

    diagnostics["loaded_prompt_concept_id"] = (
        safe_str(rendered.prompt_id) or resolved_prompt_id
    )
    return rendered, diagnostics


def ensure_prompt_concept_support(
    *,
    prompt_specs: Sequence[WorkflowPromptConceptSpec],
    workflow_links: Sequence[WorkflowPromptLinkSpec] = (),
    provenance_source: str,
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
) -> dict[str, Any]:
    created_prompt_ids: list[str] = []
    validated_prompt_ids: list[str] = []
    linked_workflow_ids: list[str] = []
    missing_content_prompt_ids: list[str] = []
    errors_by_target: dict[str, str] = {}

    for prompt_spec in prompt_specs:
        prompt_concept = safe_get_concept(prompt_spec.concept_id)
        if prompt_concept is None:
            try:
                concept_service.create_concept(
                    name=prompt_spec.name,
                    concept_id=prompt_spec.concept_id,
                    description=prompt_spec.description,
                    parent_concept_ids=list(prompt_spec.parent_concept_ids),
                    create_as_instance=bool(prompt_spec.create_as_instance),
                    visibility_scope_mode=prompt_spec.visibility_scope_mode,
                )
                created_prompt_ids.append(prompt_spec.concept_id)
                prompt_concept = safe_get_concept(prompt_spec.concept_id)
            except Exception as exc:
                errors_by_target[prompt_spec.concept_id] = (
                    f"prompt_create_failed:{exc}"
                )
                continue

        if not prompt_spec.require_content:
            validated_prompt_ids.append(prompt_spec.concept_id)
            continue

        if prompt_concept_has_content(prompt_spec.concept_id):
            validated_prompt_ids.append(prompt_spec.concept_id)
            continue

        missing_content_prompt_ids.append(prompt_spec.concept_id)
        errors_by_target[prompt_spec.concept_id] = "prompt_content_missing"

    for link_spec in workflow_links:
        if not safe_str(link_spec.workflow_id) or not safe_str(link_spec.prompt_concept_id):
            continue
        try:
            upsert_singleton_text_relation(
                subject_concept_id=link_spec.workflow_id,
                predicate=link_spec.predicate,
                text=link_spec.prompt_concept_id,
                lang=language,
                policy=policy,
                garbage_collect=garbage_collect,
                provenance={
                    "source": provenance_source,
                    "reason": link_spec.reason,
                },
                context=dict(link_spec.context or {}),
            )
            linked_workflow_ids.append(link_spec.workflow_id)
        except Exception as exc:
            errors_by_target[link_spec.workflow_id] = (
                f"workflow_prompt_link_failed:{exc}"
            )

    return {
        "success": not errors_by_target,
        "created_prompt_ids": created_prompt_ids,
        "validated_prompt_ids": validated_prompt_ids,
        "missing_content_prompt_ids": missing_content_prompt_ids,
        "linked_workflow_ids": linked_workflow_ids,
        "counts": {
            "created_prompts": len(created_prompt_ids),
            "validated_prompts": len(validated_prompt_ids),
            "missing_content_prompts": len(missing_content_prompt_ids),
            "linked_workflows": len(linked_workflow_ids),
            "errors": len(errors_by_target),
        },
        "errors_by_target": errors_by_target,
    }


__all__ = [
    "DEFAULT_PROMPT_CONTENT_PREDICATES",
    "DEFAULT_PROMPT_TYPE_ID",
    "WorkflowPromptConceptSpec",
    "WorkflowPromptLinkSpec",
    "ensure_prompt_concept_support",
    "normalise_strings",
    "prompt_concept_has_content",
    "render_authoritative_prompt",
    "resolve_linked_prompt_concept_id",
    "safe_get_concept",
    "safe_str",
]
