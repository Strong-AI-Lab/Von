from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from ...db.repositories.concepts_repository import ConceptsRepository
from ...services.text_value_service import get_texts_for_concept
from ...workflows.trace_store import (
    get_workflow_execution_trace,
    list_recent_workflow_execution_traces,
)

workflows_bp = Blueprint("workflows", __name__)


def _normalise_relationship_targets(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _first_relationship_target(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> Optional[str]:
    for predicate in candidate_predicates:
        targets = _normalise_relationship_targets(relationships.get(predicate))
        if targets:
            return targets[0]
    return None


def _all_relationship_targets(
    relationships: Dict[str, Any], candidate_predicates: Iterable[str]
) -> List[str]:
    results: List[str] = []
    for predicate in candidate_predicates:
        results.extend(_normalise_relationship_targets(relationships.get(predicate)))
    # Preserve order but remove duplicates.
    seen: set[str] = set()
    unique: List[str] = []
    for item in results:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _best_effort_workflow_narrative_text(workflow_id: str) -> Optional[str]:
    """Fetch a stored workflow definition *narrative* text from Vontology.

    Workflows should be represented structurally via explicit relationships
    (steps + control flow). This text is optional and is not machine-parsed.
    """

    if not isinstance(workflow_id, str) or not workflow_id.strip():
        return None

    try:
        texts = get_texts_for_concept(workflow_id)
    except Exception:
        return None

    if not isinstance(texts, list):
        return None

    preferred_predicates = ("hasDefinition", "hasContent", "hasDescription")

    for predicate in preferred_predicates:
        for item in texts:
            if not isinstance(item, dict):
                continue
            if item.get("predicate") != predicate:
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()

    return None


def _fetch_concepts_by_id(concept_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not concept_ids:
        return {}

    cursor = ConceptsRepository.find(
        {"concept_id": {"$in": concept_ids}},
        {"concept_id": 1, "name": 1, "relationships": 1},
        limit=len(concept_ids),
    )
    docs = list(cursor)
    mapping: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        cid = doc.get("concept_id")
        if isinstance(cid, str) and cid.strip():
            mapping[cid] = doc
    return mapping


def _build_workflow_process_graph(
    workflow_id: str,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build a workflow definition from explicit Vontology relationships.

    Expected (flexible) predicate names:
    - workflow -> hasInitialStep / has_initial_step
    - workflow -> hasStep / has_step
    - step -> invokesAction / invokes_action
    - step -> nextStep / next_step
    - step -> onTrueNextStep / on_true_next_step
    - step -> onFalseNextStep / on_false_next_step
    - step -> onFailureNextStep / on_failure_next_step
    """

    warnings: List[str] = []

    workflow_doc = ConceptsRepository.find_one(
        {"concept_id": workflow_id},
        {"concept_id": 1, "name": 1, "relationships": 1},
    )
    if not workflow_doc:
        return None, ["workflow_concept_not_found"]

    relationships = workflow_doc.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}

    initial_step = _first_relationship_target(
        relationships,
        (
            "hasInitialStep",
            "has_initial_step",
            "#V#hasInitialStep",
            "#V#has_initial_step",
        ),
    )
    step_ids = _all_relationship_targets(
        relationships, ("hasStep", "has_step", "#V#hasStep", "#V#has_step")
    )

    if initial_step and initial_step not in step_ids:
        step_ids.insert(0, initial_step)
    if not initial_step and step_ids:
        initial_step = step_ids[0]
        warnings.append("missing_hasInitialStep_used_first_hasStep")

    if not step_ids:
        return None, ["workflow_has_no_steps"]

    step_docs = _fetch_concepts_by_id(step_ids)
    missing_steps = [sid for sid in step_ids if sid not in step_docs]
    if missing_steps:
        warnings.append(f"missing_step_concepts:{','.join(missing_steps[:25])}")

    step_items: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    def _edge(source_id: str, predicate: str, target_id: Optional[str]) -> None:
        if not target_id:
            return
        edges.append({"from": source_id, "predicate": predicate, "to": target_id})

    for step_id in step_ids:
        doc = step_docs.get(step_id, {})
        step_rels = doc.get("relationships") or {}
        if not isinstance(step_rels, dict):
            step_rels = {}

        invokes_action = _first_relationship_target(
            step_rels,
            (
                "invokesAction",
                "invokes_action",
                "#V#invokesAction",
                "#V#invokes_action",
            ),
        )
        next_step = _first_relationship_target(
            step_rels, ("nextStep", "next_step", "#V#nextStep", "#V#next_step")
        )
        on_true = _first_relationship_target(
            step_rels,
            (
                "onTrueNextStep",
                "on_true_next_step",
                "#V#onTrueNextStep",
                "#V#on_true_next_step",
            ),
        )
        on_false = _first_relationship_target(
            step_rels,
            (
                "onFalseNextStep",
                "on_false_next_step",
                "#V#onFalseNextStep",
                "#V#on_false_next_step",
            ),
        )
        on_failure = _first_relationship_target(
            step_rels,
            (
                "onFailureNextStep",
                "on_failure_next_step",
                "#V#onFailureNextStep",
                "#V#on_failure_next_step",
            ),
        )

        preconditions = _all_relationship_targets(
            step_rels,
            (
                "hasPrecondition",
                "has_precondition",
                "#V#hasPrecondition",
                "#V#has_precondition",
            ),
        )
        effects = _all_relationship_targets(
            step_rels, ("hasEffect", "has_effect", "#V#hasEffect", "#V#has_effect")
        )
        reads_vars = _all_relationship_targets(
            step_rels,
            (
                "readsVariable",
                "reads_variable",
                "#V#readsVariable",
                "#V#reads_variable",
            ),
        )
        writes_vars = _all_relationship_targets(
            step_rels,
            (
                "writesVariable",
                "writes_variable",
                "#V#writesVariable",
                "#V#writes_variable",
            ),
        )

        step_items.append(
            {
                "step_id": step_id,
                "name": doc.get("name"),
                "invokes_action": invokes_action,
                "preconditions": preconditions,
                "effects": effects,
                "reads_variables": reads_vars,
                "writes_variables": writes_vars,
                "control_flow": {
                    "next": next_step,
                    "on_true": on_true,
                    "on_false": on_false,
                    "on_failure": on_failure,
                },
            }
        )

        _edge(step_id, "nextStep", next_step)
        _edge(step_id, "onTrueNextStep", on_true)
        _edge(step_id, "onFalseNextStep", on_false)
        _edge(step_id, "onFailureNextStep", on_failure)

    definition = {
        "representation": "vontology_process_graph_v1",
        "workflow_id": workflow_id,
        "initial_step": initial_step,
        "steps": step_items,
        "edges": edges,
        "warnings": warnings,
    }
    return definition, warnings


@workflows_bp.get("/api/workflows/definitions/<path:workflow_id>")
def api_get_workflow_definition(workflow_id: str):
    definition, warnings = _build_workflow_process_graph(workflow_id)
    raw = _best_effort_workflow_narrative_text(workflow_id)

    if not definition:
        # Backward compatibility: if a workflow only has narrative text, return it.
        # The preferred representation is explicit step/control-flow relationships.
        if raw:
            return jsonify(
                {
                    "workflow_id": workflow_id,
                    "definition": {"representation": "narrative_only"},
                    "raw": raw,
                    "warnings": warnings,
                }
            )

        return (
            jsonify(
                {"error": "workflow_definition_not_found", "workflow_id": workflow_id}
            ),
            404,
        )

    return jsonify(
        {
            "workflow_id": workflow_id,
            "definition": definition,
            "raw": raw,
            "warnings": warnings,
        }
    )


@workflows_bp.get("/api/workflows/executions/<execution_id>")
def api_get_workflow_execution(execution_id: str):
    doc = get_workflow_execution_trace(execution_id)
    if not doc:
        return (
            jsonify(
                {"error": "workflow_execution_not_found", "execution_id": execution_id}
            ),
            404,
        )
    return jsonify(doc)


@workflows_bp.get("/api/workflows/executions/recent")
def api_list_recent_workflow_executions():
    limit_raw = request.args.get("limit", "20")
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 20

    docs = list_recent_workflow_execution_traces(limit=limit)
    return jsonify({"items": docs, "count": len(docs)})
