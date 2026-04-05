"""Reusable concept-dossier workflow for parent-specificity rumination.

JVNAUTOSCI-217 requires parent-specificity analysis to consider multilingual
descriptions plus taxonomic and other relationship evidence. This subworkflow is
kept separately so other taxonomy-improvement workflows can reuse the same
bounded dossier assembly step.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...services.concept_relation_service import find_relations_with_argument
from ...services.concept_service import get_concept_by_concept_id
from ...services.context_bundle_service import (
    assemble_context_dossier,
    build_reconstructed_workspace,
)
from ...services.namespace_service import derive_actor_context_from_namespace
from ...services.text_value_service import get_texts_for_concept
from ...vontology.utils_vontology import get_concept_display_name_with_names_fallback
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)
from ..engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from ..workflow_registry import WorkflowRegistration

logger = logging.getLogger(__name__)

PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID = (
    "#V#parent_specificity_concept_dossier_workflow"
)

DEFAULT_DOSSIER_TEXT_LIMIT = 120
DEFAULT_DOSSIER_RELATION_LIMIT = 160


def _coerce_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _normalise_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_predicate(value: Any) -> str:
    text = _normalise_text(value)
    if text.startswith("#V#"):
        text = text[3:]
    return text


def _normalise_language(value: Any) -> str | None:
    text = _normalise_text(value)
    return text or None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _infer_subject_kind(concept_doc: Mapping[str, Any]) -> str:
    computed = _normalise_text(concept_doc.get("computed_kind")).lower()
    if computed == "individual":
        return "instance"
    if computed in {"instance", "type", "predicate"}:
        return computed

    relationships = concept_doc.get("relationships")
    if not isinstance(relationships, Mapping):
        return "unknown"
    if relationships.get("is_a_type_of"):
        return "type"
    if relationships.get("is_an_instance_of"):
        return "instance"
    return "unknown"


def _structural_target_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned.startswith("#V#") else []
    if isinstance(value, list):
        output: list[str] = []
        for item in value:
            output.extend(_structural_target_ids(item))
        return output
    return []


def _summarise_text_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    description_rows: list[dict[str, Any]] = []
    name_rows: list[dict[str, Any]] = []
    other_rows: list[dict[str, Any]] = []
    languages: set[str] = set()

    for row in rows:
        predicate = _normalise_predicate(row.get("predicate")).lower()
        language = _normalise_language(row.get("lang"))
        if language:
            languages.add(language)
        entry = {
            "predicate": _normalise_text(row.get("predicate")),
            "lang": language,
            "text": _normalise_text(row.get("text")),
        }
        if not entry["text"]:
            continue
        if predicate == "hasdescription":
            description_rows.append(entry)
        elif predicate == "hasname":
            name_rows.append(entry)
        else:
            other_rows.append(entry)

    return {
        "multilingual_descriptions": description_rows,
        "multilingual_names": name_rows,
        "other_text_relations": other_rows,
        "languages_seen": sorted(languages),
    }


def _split_relation_hits(hits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    outgoing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    for hit in hits:
        indexes = hit.get("argument_indexes")
        index_values = indexes if isinstance(indexes, list) else []
        if 1 in index_values:
            outgoing.append(hit)
        else:
            incoming.append(hit)
    return outgoing, incoming


def _build_parent_specificity_report_text(
    *,
    dossier: Mapping[str, Any],
    summary: Mapping[str, Any],
) -> str:
    display_name = _normalise_text(dossier.get("display_name")) or _normalise_text(
        dossier.get("concept_id")
    )
    languages = ", ".join(summary.get("languages_seen") or []) or "unknown"
    parent_ids = ", ".join(dossier.get("current_parent_ids") or []) or "none"
    instance_type_ids = ", ".join(dossier.get("current_instance_of_ids") or []) or "none"
    non_hierarchy = ", ".join(
        (dossier.get("relationship_summary") or {}).get("non_hierarchy_predicates") or []
    ) or "none"

    lines = [
        f"Parent-specificity dossier scaffold for {display_name}.",
        f"Languages represented: {languages}.",
        f"Description count: {summary.get('description_count', 0)}.",
        f"Direct parent IDs: {parent_ids}.",
        f"Direct instance-of IDs: {instance_type_ids}.",
        f"Non-hierarchy predicates observed: {non_hierarchy}.",
        (
            "Open question: do the multilingual descriptions and non-hierarchy "
            "relations justify a narrower existing type or a new intervening subtype?"
        ),
    ]
    return "\n".join(lines)


def build_parent_specificity_concept_dossier_workflow_test_definition() -> WorkflowDefinition:
    collect = WorkflowStateSpec(
        state_id="collect",
        actions=(
            WorkflowActionInvocation(
                action_id="parent_specificity.collect_dossier",
                inputs={
                    "materialise_context_dossier": True,
                    "materialise_report_revision": True,
                    "build_reconstructed_workspace": True,
                },
                description=(
                    "Assemble multilingual descriptions, names, and relation "
                    "evidence for one concept."
                ),
            ),
        ),
        transitions=(
            WorkflowTransitionSpec(
                to_state="complete",
                condition=lambda _ctx: True,
                reason="dossier_collected",
            ),
        ),
        metadata={
            "writes_context_keys": [
                "concept_dossier",
                "concept_dossier_summary",
                "concept_dossier_languages",
                "concept_dossier_relation_count",
                "context_dossier_id",
                "report_revision_id",
                "reconstructed_workspace",
            ],
        },
    )

    complete = WorkflowStateSpec(state_id="complete", terminal=True)
    failed = WorkflowStateSpec(state_id="failed", terminal=True)

    return WorkflowDefinition(
        workflow_id=PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
        initial_state="collect",
        states={
            "collect": collect,
            "complete": complete,
            "failed": failed,
        },
        termination_states=("complete", "failed"),
        purpose=(
            "Gather multilingual dossier evidence for parent-specificity "
            "taxonomy analysis."
        ),
    )


def _handle_collect_dossier(request: WorkflowActionRequest) -> WorkflowActionResult:
    concept_id = _normalise_text(
        request.inputs.get("concept_id") or request.data.get("concept_id")
    )
    if not concept_id:
        return WorkflowActionResult(
            status="failed",
            error="concept_id_required",
        )

    text_limit = _coerce_int(
        request.inputs.get("text_limit") or request.data.get("text_limit"),
        default=DEFAULT_DOSSIER_TEXT_LIMIT,
        minimum=10,
        maximum=400,
    )
    relation_limit = _coerce_int(
        request.inputs.get("relation_limit") or request.data.get("relation_limit"),
        default=DEFAULT_DOSSIER_RELATION_LIMIT,
        minimum=20,
        maximum=400,
    )

    try:
        concept_doc = get_concept_by_concept_id(concept_id)
    except Exception as exc:
        return WorkflowActionResult(
            status="failed",
            error=f"concept_lookup_failed:{exc}",
        )
    if not isinstance(concept_doc, Mapping):
        return WorkflowActionResult(
            status="failed",
            error=f"concept_not_found:{concept_id}",
        )

    try:
        text_rows = list(get_texts_for_concept(concept_id, limit=text_limit) or [])
    except Exception as exc:
        logger.warning(
            "[parent_specificity_dossier] text fetch failed for %s: %s",
            concept_id,
            exc,
        )
        text_rows = []

    try:
        relation_payload = find_relations_with_argument(
            concept_id,
            include_text_snippets=False,
            include_concept_preview=False,
            limit=relation_limit,
        )
        relation_hits = list(relation_payload.get("hits") or [])
    except Exception as exc:
        logger.warning(
            "[parent_specificity_dossier] relation fetch failed for %s: %s",
            concept_id,
            exc,
        )
        relation_hits = []

    try:
        display_name = get_concept_display_name_with_names_fallback(dict(concept_doc))
    except Exception:
        display_name = None
    display_name = display_name or concept_id

    text_summary = _summarise_text_rows(
        [row for row in text_rows if isinstance(row, dict)]
    )
    outgoing_hits, incoming_hits = _split_relation_hits(
        [row for row in relation_hits if isinstance(row, dict)]
    )

    relationships = concept_doc.get("relationships")
    relationship_map = dict(relationships) if isinstance(relationships, Mapping) else {}
    parent_ids = _structural_target_ids(relationship_map.get("is_a_type_of"))
    instance_of_ids = _structural_target_ids(relationship_map.get("is_an_instance_of"))
    non_hierarchy_predicates = sorted(
        predicate
        for predicate, raw_value in relationship_map.items()
        if predicate not in {"is_a_type_of", "is_an_instance_of", "has_subtype", "has_instance"}
        and _structural_target_ids(raw_value)
    )

    dossier = {
        "concept_id": concept_id,
        "display_name": display_name,
        "subject_kind": _infer_subject_kind(concept_doc),
        "current_parent_ids": parent_ids,
        "current_instance_of_ids": instance_of_ids,
        "multilingual_descriptions": text_summary["multilingual_descriptions"],
        "multilingual_names": text_summary["multilingual_names"],
        "other_text_relations": text_summary["other_text_relations"],
        "outgoing_relation_hits": outgoing_hits,
        "incoming_relation_hits": incoming_hits,
        "relationship_summary": {
            "non_hierarchy_predicates": non_hierarchy_predicates,
            "outgoing_relation_count": len(outgoing_hits),
            "incoming_relation_count": len(incoming_hits),
            "direct_parent_count": len(parent_ids),
            "direct_instance_type_count": len(instance_of_ids),
        },
    }
    summary = {
        "concept_id": concept_id,
        "display_name": display_name,
        "subject_kind": dossier["subject_kind"],
        "languages_seen": text_summary["languages_seen"],
        "description_count": len(dossier["multilingual_descriptions"]),
        "name_count": len(dossier["multilingual_names"]),
        "outgoing_relation_count": len(outgoing_hits),
        "incoming_relation_count": len(incoming_hits),
        "direct_parent_count": len(parent_ids),
        "direct_instance_type_count": len(instance_of_ids),
        "non_hierarchy_predicate_count": len(non_hierarchy_predicates),
    }

    materialise_context_dossier = _truthy(
        request.inputs.get("materialise_context_dossier")
        or request.data.get("materialise_context_dossier")
    )
    materialise_report_revision = _truthy(
        request.inputs.get("materialise_report_revision")
        or request.data.get("materialise_report_revision")
        or materialise_context_dossier
    )
    build_workspace = _truthy(
        request.inputs.get("build_reconstructed_workspace")
        or request.data.get("build_reconstructed_workspace")
        or materialise_context_dossier
    )

    context_dossier_id = None
    report_revision_id = None
    reconstructed_workspace = None

    if materialise_context_dossier:
        namespace = request.environment.user_namespace if request.environment else None
        user_id, org_id = derive_actor_context_from_namespace(namespace)
        open_questions = [
            (
                "Do the multilingual descriptions and non-hierarchy relations justify "
                "a narrower existing type?"
            ),
            (
                "Is an intervening subtype required before changing the current "
                "parent/type assignment?"
            ),
        ]
        dossier_result = assemble_context_dossier(
            name=f"{display_name} parent-specificity dossier",
            dossier_id=_normalise_text(request.inputs.get("context_dossier_id")) or None,
            subject_kind="concept",
            subject_id=concept_id,
            dossier_kind="ontology_refinement",
            effective_context_bundle_ids=(
                request.inputs.get("effective_context_bundle_ids")
                or request.data.get("effective_context_bundle_ids")
                or ()
            ),
            open_questions=open_questions,
            immediate_context={
                "concept_dossier": dossier,
                "concept_dossier_summary": summary,
            },
            search_history=request.data.get("search_history") or (),
            report_text=(
                _build_parent_specificity_report_text(dossier=dossier, summary=summary)
                if materialise_report_revision
                else None
            ),
            report_title=f"{display_name} parent-specificity scaffold",
            report_summary={
                "source_workflow_id": PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
                "concept_id": concept_id,
                "summary": summary,
            },
            namespace=namespace,
            user_id=user_id,
            org_id=org_id,
        )
        if bool(dossier_result.get("success")):
            context_dossier_id = _normalise_text(dossier_result.get("dossier_id")) or None
            report_revision_id = (
                _normalise_text(dossier_result.get("report_revision_id")) or None
            )
            if build_workspace and context_dossier_id:
                workspace_result = build_reconstructed_workspace(
                    subject_kind="concept",
                    subject_id=concept_id,
                    question=(
                        "Assess whether this concept should gain a narrower parent "
                        "or an intervening subtype."
                    ),
                    task="Prepare bounded ontology-refinement context for parent-specificity analysis.",
                    dossier_id=context_dossier_id,
                    report_revision_id=report_revision_id,
                )
                if bool(workspace_result.get("success")):
                    reconstructed_workspace = workspace_result.get("workspace")

    return WorkflowActionResult(
        outputs={
            "concept_dossier": dossier,
            "concept_dossier_summary": summary,
            "concept_dossier_languages": text_summary["languages_seen"],
            "concept_dossier_relation_count": len(outgoing_hits) + len(incoming_hits),
            "context_dossier_id": context_dossier_id,
            "report_revision_id": report_revision_id,
            "reconstructed_workspace": reconstructed_workspace,
        }
    )


def build_parent_specificity_concept_dossier_workflow_test_registration() -> WorkflowRegistration:
    return WorkflowRegistration(
        workflow_id=PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
        definition=build_parent_specificity_concept_dossier_workflow_test_definition(),
        purpose=(
            "Gather multilingual dossier evidence for parent-specificity "
            "taxonomy analysis."
        ),
        source="built_in",
    )


def register_parent_specificity_concept_dossier_actions(
    registry: ActionRegistry,
) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id="parent_specificity.collect_dossier",
            handler=_handle_collect_dossier,
            description=(
                "Assemble multilingual descriptions, names, and relation "
                "evidence for one concept."
            ),
            side_effects="read_only",
        )
    )


__all__ = [
    "DEFAULT_DOSSIER_RELATION_LIMIT",
    "DEFAULT_DOSSIER_TEXT_LIMIT",
    "PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID",
    "build_parent_specificity_concept_dossier_workflow_test_definition",
    "build_parent_specificity_concept_dossier_workflow_test_registration",
    "register_parent_specificity_concept_dossier_actions",
]
