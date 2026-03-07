"""Vontology-backed prompt support for workflow-discovery gap recovery.

The recovery and test workflows fail closed if their authoritative prompts are
missing. Startup bootstraps those prompt concepts here so workflow-governed
recovery behaviour can evolve without embedding silent inline fallbacks in code.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .prompt_template_service import PromptTemplateService, RenderedPrompt
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from ..workflows.workflow_gap_workflow_contracts import (
    DEFAULT_WORKFLOW_GAP_PROMPT_WORKFLOW_IDS,
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_ANALYSIS_PROMPT_TYPE_ID,
    WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
    WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
)

_WORKFLOW_GAP_ANALYSIS_LINK_PREDICATES: tuple[str, ...] = (
    WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE,
    "has_workflow_gap_analysis_prompt",
    "hasWorkflowGapAnalysisPrompt",
)

_WORKFLOW_GAP_TEST_LINK_PREDICATES: tuple[str, ...] = (
    WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE,
    "has_workflow_gap_test_prompt",
    "hasWorkflowGapTestPrompt",
)

_CANONICAL_WORKFLOW_GAP_ANALYSIS_PROMPT_TEXT = """You are analysing a workflow-discovery miss in Von.

Read the JSON payload and decide whether the turn reveals a genuine missing
workflow that should be created, or whether the fallback response was adequate.

Return JSON only with this shape:
{
  "decision": "no_gap" | "ask_user" | "create_and_retry",
  "confidence": 0.0,
  "intent_summary": "short summary",
  "gap_summary": "short summary",
  "workflow_name": "Candidate workflow name",
  "workflow_id": "#V#optional_candidate_workflow_id",
  "workflow_description": "One or two sentences in New Zealand English.",
  "workflow_guidance": ["short instruction"],
  "acceptance_requirements": ["observable requirement"],
  "requires_write_tools": false,
  "reasons": ["short reason"],
  "user_confirmation_prompt": "question for the user when certainty is insufficient"
}

Rules:
- Use "no_gap" when the fallback workflow already satisfied the user intent or
  when no distinct reusable workflow gap is evidenced.
- Use "ask_user" when a plausible reusable workflow gap exists but certainty is
  not high enough to auto-retry.
- Use "create_and_retry" only when a reusable workflow can be clearly specified
  and should be tested immediately.
- Acceptance requirements must be explicit, externally checkable outcomes.
- Prefer reusable, knowledge-driven workflow ideas over procedural one-offs.
- Do not claim certainty unless the evidence strongly supports it.

Payload JSON:
{analysis_payload_json}
"""

_CANONICAL_WORKFLOW_GAP_TEST_PROMPT_TEXT = """You are evaluating whether a newly created workflow satisfied the intended task.

Return JSON only with this shape:
{
  "pass": true,
  "confidence": 0.0,
  "satisfied_requirements": ["requirement text"],
  "missing_requirements": ["requirement text"],
  "reason": "short explanation in New Zealand English"
}

Rules:
- Judge against the stated acceptance requirements, not against vague intent.
- Mark "pass" false if any important requirement is missing, contradicted, or
  only claimed without evidence in the response/tool activity.
- Be conservative about completion claims.

Payload JSON:
{test_payload_json}
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


def _resolve_linked_prompt_concept_id(
    *,
    workflow_id: str | None,
    predicates: Sequence[str],
) -> str | None:
    workflow_concept_id = _safe_str(workflow_id)
    if not workflow_concept_id:
        return None
    for predicate in predicates:
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
    return None


def resolve_workflow_gap_analysis_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    explicit_id = _safe_str(prompt_concept_id)
    if explicit_id:
        return explicit_id
    linked_prompt_id = _resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        predicates=_WORKFLOW_GAP_ANALYSIS_LINK_PREDICATES,
    )
    return linked_prompt_id or WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID


def resolve_workflow_gap_test_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    explicit_id = _safe_str(prompt_concept_id)
    if explicit_id:
        return explicit_id
    linked_prompt_id = _resolve_linked_prompt_concept_id(
        workflow_id=workflow_id,
        predicates=_WORKFLOW_GAP_TEST_LINK_PREDICATES,
    )
    return linked_prompt_id or WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID


def _render_prompt(
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
        _safe_str(rendered.prompt_id) or resolved_prompt_id
    )
    return rendered, diagnostics


def render_workflow_gap_analysis_prompt(
    *,
    workflow_id: str | None = WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    resolved_prompt_id = resolve_workflow_gap_analysis_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = _render_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_gap_analysis",
    )
    diagnostics["requested_workflow_id"] = _safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = _safe_str(prompt_concept_id)
    return rendered, diagnostics


def render_workflow_gap_test_prompt(
    *,
    workflow_id: str | None = WORKFLOW_GAP_TEST_WORKFLOW_ID,
    prompt_concept_id: str | None = None,
    variables: Mapping[str, Any] | None = None,
    max_chars: int = 24000,
) -> tuple[RenderedPrompt | None, dict[str, Any]]:
    resolved_prompt_id = resolve_workflow_gap_test_prompt_concept_id(
        workflow_id=workflow_id,
        prompt_concept_id=prompt_concept_id,
    )
    rendered, diagnostics = _render_prompt(
        resolved_prompt_id=resolved_prompt_id,
        variables=variables,
        max_chars=max_chars,
        error_prefix="workflow_gap_test",
    )
    diagnostics["requested_workflow_id"] = _safe_str(workflow_id)
    diagnostics["requested_prompt_concept_id"] = _safe_str(prompt_concept_id)
    return rendered, diagnostics


def ensure_workflow_gap_prompt_support(
    *,
    workflow_ids: Sequence[str] | None = DEFAULT_WORKFLOW_GAP_PROMPT_WORKFLOW_IDS,
    analysis_prompt_concept_id: str = WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID,
    test_prompt_concept_id: str = WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID,
    prompt_predicate: str = "hasContent",
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
) -> dict[str, Any]:
    requested_workflow_ids = _normalise_strings(workflow_ids)
    prompt_created: list[str] = []
    prompt_persisted: list[str] = []
    linked_workflow_ids: list[str] = []
    errors_by_target: dict[str, str] = {}

    prompt_specs = (
        (
            analysis_prompt_concept_id,
            "Workflow gap analysis prompt",
            _CANONICAL_WORKFLOW_GAP_ANALYSIS_PROMPT_TEXT,
        ),
        (
            test_prompt_concept_id,
            "Workflow gap test prompt",
            _CANONICAL_WORKFLOW_GAP_TEST_PROMPT_TEXT,
        ),
    )

    for prompt_concept_id, prompt_name, prompt_text in prompt_specs:
        prompt_concept = _safe_get_concept(prompt_concept_id)
        if prompt_concept is None:
            try:
                concept_service.create_concept(
                    name=prompt_name,
                    concept_id=prompt_concept_id,
                    description=(
                        "Canonical LLM prompt for workflow-gap recovery and "
                        "candidate workflow evaluation."
                    ),
                    parent_concept_ids=[WORKFLOW_GAP_ANALYSIS_PROMPT_TYPE_ID],
                    create_as_instance=True,
                    visibility_scope_mode="global_general",
                )
                prompt_created.append(prompt_concept_id)
            except Exception as exc:
                errors_by_target[prompt_concept_id] = f"prompt_create_failed:{exc}"
                continue

        try:
            upsert_singleton_text_relation(
                subject_concept_id=prompt_concept_id,
                predicate=prompt_predicate,
                text=prompt_text,
                lang=language,
                policy=policy,
                garbage_collect=garbage_collect,
                provenance={
                    "source": "workflow_gap_vontology_service",
                    "reason": "canonical_workflow_gap_prompt_bootstrap",
                },
                context={"jira": "JVNAUTOSCI-1389"},
            )
            prompt_persisted.append(prompt_concept_id)
        except Exception as exc:
            errors_by_target[prompt_concept_id] = f"prompt_persist_failed:{exc}"

    for workflow_id in requested_workflow_ids:
        try:
            if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
                upsert_singleton_text_relation(
                    subject_concept_id=workflow_id,
                    predicate=WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE,
                    text=analysis_prompt_concept_id,
                    lang=language,
                    policy=policy,
                    garbage_collect=garbage_collect,
                    provenance={
                        "source": "workflow_gap_vontology_service",
                        "reason": "workflow_gap_analysis_prompt_link_bootstrap",
                    },
                    context={"jira": "JVNAUTOSCI-1389"},
                )
                linked_workflow_ids.append(workflow_id)
                continue
            if workflow_id == WORKFLOW_GAP_TEST_WORKFLOW_ID:
                upsert_singleton_text_relation(
                    subject_concept_id=workflow_id,
                    predicate=WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE,
                    text=test_prompt_concept_id,
                    lang=language,
                    policy=policy,
                    garbage_collect=garbage_collect,
                    provenance={
                        "source": "workflow_gap_vontology_service",
                        "reason": "workflow_gap_test_prompt_link_bootstrap",
                    },
                    context={"jira": "JVNAUTOSCI-1389"},
                )
                linked_workflow_ids.append(workflow_id)
        except Exception as exc:
            errors_by_target[workflow_id] = f"workflow_prompt_link_failed:{exc}"

    return {
        "success": not errors_by_target,
        "analysis_prompt_concept_id": analysis_prompt_concept_id,
        "test_prompt_concept_id": test_prompt_concept_id,
        "prompt_created": list(prompt_created),
        "prompt_persisted": list(prompt_persisted),
        "linked_workflow_ids": linked_workflow_ids,
        "counts": {
            "prompts_created": len(prompt_created),
            "prompts_persisted": len(prompt_persisted),
            "linked_workflows": len(linked_workflow_ids),
            "errors": len(errors_by_target),
        },
        "errors_by_target": errors_by_target,
    }


__all__ = [
    "WORKFLOW_GAP_ANALYSIS_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_ANALYSIS_PROMPT_LINK_PREDICATE",
    "WORKFLOW_GAP_CANDIDATE_PROMPT_LINK_PREDICATE",
    "WORKFLOW_GAP_TEST_PROMPT_CONCEPT_ID",
    "WORKFLOW_GAP_TEST_PROMPT_LINK_PREDICATE",
    "ensure_workflow_gap_prompt_support",
    "render_workflow_gap_analysis_prompt",
    "render_workflow_gap_test_prompt",
    "resolve_workflow_gap_analysis_prompt_concept_id",
    "resolve_workflow_gap_test_prompt_concept_id",
]
