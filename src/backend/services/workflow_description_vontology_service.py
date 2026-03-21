"""Vontology-backed prompt support for workflow-description enrichment.

JVNAUTOSCI-1425 keeps retrieval-quality workflow-description prompts in
Vontology so description backfill can run under workflow control rather than
falling back to Python-only prompt text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import concept_service
from .concept_service import ConceptNotFoundError, get_concept_by_concept_id
from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from ..workflows.vontology_loader import build_workflow_process_graph

DESCRIPTION_PROMPT_TYPE_ID = "#V#prompt_for_llm"
DESCRIPTION_PROMPT_CONCEPT_ID = "#V#generate_concept_description_prompt"
WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID = "#V#generate_workflow_description_prompt"
WORKFLOW_DESCRIPTION_PROMPT_LINK_PREDICATE = "#V#has_workflow_description_prompt"

DEFAULT_WORKFLOW_DESCRIPTION_WORKFLOW_IDS: tuple[str, ...] = ("#V#enrichment_workflow",)

_WORKFLOW_DESCRIPTION_LINK_PREDICATES: tuple[str, ...] = (
    WORKFLOW_DESCRIPTION_PROMPT_LINK_PREDICATE,
    "has_workflow_description_prompt",
    "hasWorkflowDescriptionPrompt",
)

_CANONICAL_GENERIC_DESCRIPTION_PROMPT_TEXT = """You are a knowledge engineer helping to document an ontology.

Given the following concept information, generate a clear, concise description
(1-3 sentences) that:
1. Explains what the concept represents.
2. Distinguishes it from similar concepts where the evidence supports that.
3. Uses precise, encyclopaedic language in New Zealand English.

Concept name: {concept_name}
Concept ID: {concept_id}
Type hierarchy: {type_hierarchy}
Relationships: {relationships}
Existing description: {description}

Respond with ONLY the description text, with no preamble or explanation."""

_CANONICAL_WORKFLOW_DESCRIPTION_PROMPT_TEXT = """You are improving workflow descriptions for RAG-based workflow discovery and capability matching in Von.

Given the workflow concept information and workflow-structure context, write a retrieval-quality workflow description in New Zealand English.

Return plain text only. Keep it under 160 words and include:
- One lead sentence saying what task shapes the workflow handles and what it does.
- `Domain: ...`
- `Input types: ...`
- `Output types: ...`
- `Prerequisite capabilities: ...`
- `Cost class: ...`
- `Maturity: ...`
- `Estimated success likelihood: ...`

Rules:
- Use only evidence present in the concept information, relationships, and workflow graph.
- Mention major invoked actions or subworkflows only when they help retrieval.
- If the workflow is skeletal, partially non-executable, or missing action bindings, say so explicitly in `Maturity`.
- Prefer compact, information-dense prose over marketing language.
- Do not mention this prompt or the word "RAG".

Concept name: {concept_name}
Concept ID: {concept_id}
Existing description: {description}
Type hierarchy: {type_hierarchy}
Relationships: {relationships}
Workflow initial step: {workflow_initial_step}
Workflow step count: {workflow_step_count}
Workflow actions: {workflow_action_ids}
Workflow subworkflows: {workflow_subworkflow_ids}
Workflow transition reasons: {workflow_transition_reasons}
Workflow output context keys: {workflow_output_context_keys}
Workflow structure summary: {workflow_structure_summary}
Workflow graph warnings: {workflow_graph_warnings}
"""

_WORKFLOW_DOMAIN_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("jira", "reconciliation", "import"), "Jira task synchronisation and reconciliation"),
    (("file_copy", "upload", "document", "image"), "File-copy ingestion and interpretation"),
    (("arxiv", "paper", "scholarly"), "Scholarly paper representation"),
    (("talk", "presentation", "seminar"), "Talk and presentation representation"),
    (("parent_specificity", "taxonomy", "dossier"), "Taxonomy refinement and parent-specificity analysis"),
    (("workflow_gap", "introspection", "maintenance", "enrichment", "rag"), "Workflow maintenance and knowledge-store synchronisation"),
    (("entity", "identity", "duplicate"), "Entity identity resolution and ontology maintenance"),
    (("conversation", "chat", "tool_calling", "planning", "rumination", "turn"), "Conversation orchestration and tool-assisted reasoning"),
    (("testing", "experiment", "benchmark", "promotion", "theory"), "Workflow testing and experiment evaluation"),
)


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


def _humanise_identifier(value: Any) -> str | None:
    cleaned = _safe_str(value)
    if not cleaned:
        return None
    if cleaned.startswith("#V#"):
        cleaned = cleaned[3:]
    cleaned = cleaned.replace(".", " ").replace("_", " ").strip()
    return " ".join(part for part in cleaned.split() if part) or None


def _parse_context_items(value: Any) -> tuple[str, ...]:
    cleaned = _safe_str(value)
    if not cleaned:
        return ()
    lowered = cleaned.lower()
    if lowered in {
        "no bound actions",
        "no subworkflows",
        "no explicit transition reasons",
        "no declared output context keys",
        "no workflow step summary",
        "none",
    }:
        return ()
    separators = [";", ","]
    items = [cleaned]
    for separator in separators:
        next_items: list[str] = []
        for item in items:
            next_items.extend(item.split(separator))
        items = next_items
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        cleaned_item = _safe_str(item)
        if not cleaned_item:
            continue
        lowered_item = cleaned_item.lower()
        if lowered_item in seen:
            continue
        seen.add(lowered_item)
        output.append(cleaned_item)
    return tuple(output)


def _build_sentence(text: str | None, fallback: str) -> str:
    chosen = _safe_str(text) or fallback
    if chosen.endswith("."):
        return chosen
    return f"{chosen}."


def _infer_workflow_domain(*, concept_id: str, concept_name: str, evidence_text: str) -> str:
    lowered = " ".join([concept_id, concept_name, evidence_text]).lower()
    for keywords, label in _WORKFLOW_DOMAIN_HINTS:
        if any(keyword in lowered for keyword in keywords):
            return label
    return "General workflow orchestration"


def _summarise_capabilities(values: Sequence[str], *, fallback: str) -> str:
    rendered = [
        _humanise_identifier(value)
        for value in values
    ]
    compact = [item for item in rendered if item]
    if not compact:
        return fallback
    return ", ".join(compact[:3])


def _derive_cost_class(*, step_count: int, action_count: int, subworkflow_count: int) -> str:
    if step_count >= 9 or subworkflow_count >= 2:
        return "High; multi-stage orchestration."
    if step_count >= 5 or action_count >= 3 or subworkflow_count >= 1:
        return "Medium; bounded orchestration."
    return "Low; short deterministic flow."


def build_deterministic_workflow_description(
    *,
    concept_id: str,
    concept_name: str | None = None,
    existing_description: str | None = None,
    workflow_context: Mapping[str, str] | None = None,
) -> str | None:
    """Build a retrieval-ready workflow description without requiring an LLM.

    This is an explicit graph-derived synthesis path used when the workflow
    description enrichment flow cannot reach a model runtime. It keeps the
    backfill operation under workflow control while remaining grounded in
    Vontology-resident workflow structure.
    """

    context = (
        dict(workflow_context)
        if isinstance(workflow_context, Mapping)
        else build_workflow_description_prompt_context(concept_id)
    )
    if not context:
        return None

    name = _safe_str(concept_name) or _humanise_identifier(concept_id) or concept_id
    base_description = _safe_str(existing_description)
    action_ids = _parse_context_items(context.get("workflow_action_ids"))
    subworkflow_ids = _parse_context_items(context.get("workflow_subworkflow_ids"))
    transition_reasons = _parse_context_items(context.get("workflow_transition_reasons"))
    output_context_keys = _parse_context_items(context.get("workflow_output_context_keys"))
    graph_warnings = _parse_context_items(context.get("workflow_graph_warnings"))
    try:
        step_count = int(_safe_str(context.get("workflow_step_count")) or "0")
    except ValueError:
        step_count = 0

    evidence_text = " ".join(
        [
            base_description or "",
            " ".join(action_ids),
            " ".join(subworkflow_ids),
            " ".join(transition_reasons),
            " ".join(output_context_keys),
        ]
    )
    domain = _infer_workflow_domain(
        concept_id=concept_id,
        concept_name=name,
        evidence_text=evidence_text,
    )
    lead = _build_sentence(
        base_description,
        f"{name} workflow that coordinates {_summarise_capabilities(action_ids or subworkflow_ids, fallback='workflow steps')}",
    )
    input_types = (
        f"Workflow context, trigger data, and state for {_summarise_capabilities(action_ids or subworkflow_ids, fallback='the mapped steps')}."
    )
    if output_context_keys:
        output_types = (
            f"Updated workflow state plus {_summarise_capabilities(output_context_keys, fallback='execution outputs')}."
        )
    else:
        output_types = "Updated workflow state and persisted side effects."
    prerequisite_capabilities = (
        f"{_summarise_capabilities(action_ids, fallback='Action registry bindings')}"
    )
    if subworkflow_ids:
        prerequisite_capabilities = (
            f"{prerequisite_capabilities}; {_summarise_capabilities(subworkflow_ids, fallback='subworkflow orchestration')}"
        )
    maturity = (
        "Authoritative graph with execution warnings."
        if graph_warnings
        else f"Authoritative Vontology workflow with {max(step_count, 1)} mapped steps."
    )
    success_likelihood = (
        "Moderate while warnings remain."
        if graph_warnings
        else "Moderate to high for supported inputs."
    )

    return "\n".join(
        [
            lead,
            f"Domain: {domain}",
            f"Input types: {input_types}",
            f"Output types: {output_types}",
            f"Prerequisite capabilities: {prerequisite_capabilities}",
            f"Cost class: {_derive_cost_class(step_count=step_count, action_count=len(action_ids), subworkflow_count=len(subworkflow_ids))}",
            f"Maturity: {maturity}",
            f"Estimated success likelihood: {success_likelihood}",
        ]
    )


def _safe_get_concept(concept_id: str) -> Mapping[str, Any] | None:
    try:
        concept = get_concept_by_concept_id(concept_id)
    except ConceptNotFoundError:
        return None
    return concept if isinstance(concept, Mapping) else None


def resolve_workflow_description_prompt_concept_id(
    *,
    workflow_id: str | None = None,
    prompt_concept_id: str | None = None,
) -> str | None:
    explicit_id = _safe_str(prompt_concept_id)
    if explicit_id:
        return explicit_id

    workflow_concept_id = _safe_str(workflow_id)
    if workflow_concept_id:
        for predicate in _WORKFLOW_DESCRIPTION_LINK_PREDICATES:
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

    return WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID


def build_workflow_description_prompt_context(concept_id: str) -> dict[str, str]:
    """Derive compact workflow-structure context for description generation."""

    graph, warnings = build_workflow_process_graph(concept_id)
    if not isinstance(graph, Mapping):
        return {}

    steps = [
        dict(step)
        for step in (graph.get("steps") or [])
        if isinstance(step, Mapping)
    ]
    if not steps:
        return {}

    initial_step = _safe_str(graph.get("initial_step")) or "Unknown"
    action_ids: list[str] = []
    subworkflow_ids: list[str] = []
    transition_reasons: list[str] = []
    output_context_keys: list[str] = []
    step_summaries: list[str] = []

    def _append_unique(target: list[str], value: Any) -> None:
        cleaned = _safe_str(value)
        if cleaned and cleaned not in target:
            target.append(cleaned)

    for step in steps:
        step_id = _safe_str(step.get("step_id")) or "unknown_step"
        action_id = _safe_str(step.get("invokes_action_target")) or _safe_str(
            step.get("invokes_action")
        )
        subworkflow_id = _safe_str(step.get("invokes_workflow"))
        _append_unique(action_ids, action_id)
        _append_unique(subworkflow_ids, subworkflow_id)

        for key in step.get("writes_context_keys") or []:
            _append_unique(output_context_keys, key)

        control_flow = step.get("control_flow") or {}
        if isinstance(control_flow, Mapping):
            for condition in control_flow.get("conditions") or []:
                if not isinstance(condition, Mapping):
                    continue
                _append_unique(transition_reasons, condition.get("reason"))

        summary_parts: list[str] = [step_id]
        if action_id:
            summary_parts.append(f"action={action_id}")
        if subworkflow_id:
            summary_parts.append(f"subworkflow={subworkflow_id}")
        step_summaries.append("[" + "; ".join(summary_parts) + "]")

    return {
        "workflow_initial_step": initial_step,
        "workflow_step_count": str(len(steps)),
        "workflow_action_ids": ", ".join(action_ids) if action_ids else "No bound actions",
        "workflow_subworkflow_ids": (
            ", ".join(subworkflow_ids) if subworkflow_ids else "No subworkflows"
        ),
        "workflow_transition_reasons": (
            ", ".join(transition_reasons)
            if transition_reasons
            else "No explicit transition reasons"
        ),
        "workflow_output_context_keys": (
            ", ".join(output_context_keys)
            if output_context_keys
            else "No declared output context keys"
        ),
        "workflow_structure_summary": " ".join(step_summaries[:8]) or "No workflow step summary",
        "workflow_graph_warnings": (
            "; ".join(str(item).strip() for item in warnings[:4] if str(item).strip())
            if warnings
            else "None"
        ),
    }


def ensure_workflow_description_prompt_support(
    *,
    workflow_ids: Sequence[str] | None = DEFAULT_WORKFLOW_DESCRIPTION_WORKFLOW_IDS,
    generic_prompt_concept_id: str = DESCRIPTION_PROMPT_CONCEPT_ID,
    workflow_prompt_concept_id: str = WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID,
    prompt_predicate: str = "hasContent",
    workflow_link_predicate: str = WORKFLOW_DESCRIPTION_PROMPT_LINK_PREDICATE,
    language: str = "en-NZ",
    policy: str = "replace_others",
    garbage_collect: bool = True,
) -> dict[str, Any]:
    """Ensure workflow-description prompt concepts exist and are linked."""

    requested_workflow_ids = _normalise_strings(workflow_ids)
    created_prompt_ids: list[str] = []
    persisted_prompt_ids: list[str] = []
    linked_workflow_ids: list[str] = []
    errors_by_target: dict[str, str] = {}

    prompt_specs = (
        (
            generic_prompt_concept_id,
            "Concept description prompt",
            "Canonical LLM prompt for general concept description generation.",
            _CANONICAL_GENERIC_DESCRIPTION_PROMPT_TEXT,
        ),
        (
            workflow_prompt_concept_id,
            "Workflow description prompt",
            "Canonical LLM prompt for retrieval-quality workflow descriptions.",
            _CANONICAL_WORKFLOW_DESCRIPTION_PROMPT_TEXT,
        ),
    )

    for prompt_concept_id, prompt_name, prompt_description, prompt_text in prompt_specs:
        prompt_concept = _safe_get_concept(prompt_concept_id)
        if prompt_concept is None:
            try:
                concept_service.create_concept(
                    name=prompt_name,
                    concept_id=prompt_concept_id,
                    description=prompt_description,
                    parent_concept_ids=[DESCRIPTION_PROMPT_TYPE_ID],
                    create_as_instance=True,
                    visibility_scope_mode="global_general",
                )
                created_prompt_ids.append(prompt_concept_id)
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
                    "source": "workflow_description_vontology_service",
                    "reason": "canonical_workflow_description_prompt_bootstrap",
                },
                context={"jira": "JVNAUTOSCI-1425"},
            )
            persisted_prompt_ids.append(prompt_concept_id)
        except Exception as exc:
            errors_by_target[prompt_concept_id] = f"prompt_persist_failed:{exc}"

    for workflow_id in requested_workflow_ids:
        try:
            upsert_singleton_text_relation(
                subject_concept_id=workflow_id,
                predicate=workflow_link_predicate,
                text=workflow_prompt_concept_id,
                lang=language,
                policy=policy,
                garbage_collect=garbage_collect,
                provenance={
                    "source": "workflow_description_vontology_service",
                    "reason": "workflow_description_prompt_link_bootstrap",
                },
                context={
                    "prompt_concept_id": workflow_prompt_concept_id,
                    "jira": "JVNAUTOSCI-1425",
                },
            )
            linked_workflow_ids.append(workflow_id)
        except Exception as exc:
            errors_by_target[workflow_id] = f"workflow_prompt_link_failed:{exc}"

    return {
        "success": not errors_by_target,
        "generic_prompt_concept_id": generic_prompt_concept_id,
        "workflow_prompt_concept_id": workflow_prompt_concept_id,
        "created_prompt_ids": created_prompt_ids,
        "persisted_prompt_ids": persisted_prompt_ids,
        "linked_workflow_ids": linked_workflow_ids,
        "counts": {
            "created_prompts": len(created_prompt_ids),
            "persisted_prompts": len(persisted_prompt_ids),
            "linked_workflows": len(linked_workflow_ids),
            "errors": len(errors_by_target),
        },
        "errors_by_target": errors_by_target,
    }


__all__ = [
    "DESCRIPTION_PROMPT_CONCEPT_ID",
    "DESCRIPTION_PROMPT_TYPE_ID",
    "WORKFLOW_DESCRIPTION_PROMPT_CONCEPT_ID",
    "WORKFLOW_DESCRIPTION_PROMPT_LINK_PREDICATE",
    "DEFAULT_WORKFLOW_DESCRIPTION_WORKFLOW_IDS",
    "build_deterministic_workflow_description",
    "build_workflow_description_prompt_context",
    "ensure_workflow_description_prompt_support",
    "resolve_workflow_description_prompt_concept_id",
]
