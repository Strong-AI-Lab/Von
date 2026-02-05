from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple, Callable

from ..db.repositories.concepts_repository import ConceptsRepository
from ..services.text_value_service import get_texts_for_concept
from .engine import (
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowActionInvocation,
    WorkflowTransitionSpec,
)

logger = logging.getLogger(__name__)


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


def best_effort_workflow_narrative_text(workflow_id: str) -> Optional[str]:
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


def build_workflow_process_graph(
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


def load_workflow_definition_from_vontology(
    workflow_id: str,
) -> Optional[WorkflowDefinition]:
    """Load an executable WorkflowDefinition from Vontology."""
    graph, warnings = build_workflow_process_graph(workflow_id)
    if not graph:
        return None

    initial_state_id = graph.get("initial_state")
    # fallback if initial_state (from graph) is None but steps exist
    if not initial_state_id:
        steps = graph.get("steps")
        if steps and len(steps) > 0:
            initial_state_id = steps[0]["step_id"]

    if not initial_state_id:
        return None

    states: Dict[str, WorkflowStateSpec] = {}

    for step in graph.get("steps", []):
        step_id = step.get("step_id")
        invokes_action = step.get("invokes_action")
        control_flow = step.get("control_flow", {})

        actions = []
        if invokes_action:
            # We assume inputs are implicit or passed via context for now
            # In a full impl, we'd read 'hasInputMap' from the step
            actions.append(WorkflowActionInvocation(action_id=invokes_action))

        transitions = []

        # Priority: OnFailure -> OnTrue/False -> Next (unconditional)
        if control_flow.get("on_failure"):
            # TODO: Support exception-based transition triggers in engine
            pass

        if control_flow.get("on_true"):
            # Transition if context 'last_step_ok' is True (stub)
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=control_flow["on_true"],
                    condition=lambda ctx: bool(
                        ctx.get("last_step_ok") or ctx.get("result")
                    ),
                    reason="on_true",
                )
            )

        if control_flow.get("on_false"):
            # Transition if context 'last_step_ok' is False
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=control_flow["on_false"],
                    condition=lambda ctx: not bool(
                        ctx.get("last_step_ok") or ctx.get("result")
                    ),
                    reason="on_false",
                )
            )

        if control_flow.get("next"):
            # Unconditional transition (always true fallback)
            transitions.append(
                WorkflowTransitionSpec(
                    to_state=control_flow["next"],
                    condition=lambda ctx: True,
                    reason="next_step",
                )
            )

        # Check for terminal
        is_terminal = not transitions and not control_flow.get("next")

        states[step_id] = WorkflowStateSpec(
            state_id=step_id,
            actions=actions,
            transitions=transitions,
            terminal=is_terminal,
        )

    # Fetch description
    purpose = best_effort_workflow_narrative_text(workflow_id)

    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state=initial_state_id,
        states=states,
        purpose=purpose,
    )


def _get_recursive_subtypes(root_type_id: str, max_depth: int = 3) -> set[str]:
    """Find all concept IDs that are subtypes of the given root type (transitively)."""
    found = set()
    queue = [root_type_id]
    depth = 0

    while queue and depth < max_depth:
        # Find immediate subtypes: concepts where (is_a_type_of == current OR has_subtype contains current?? No.
        # Subtype relationship: Child -> is_a_type_of -> Parent
        # OR Parent -> has_subtype -> Child.
        # We should check both, but efficiently.

        # Query: relationships.is_a_type_of IN queue
        # AND relationships.is_a_type_of is a list of strings

        parents = list(queue)
        queue = []

        # Find children via is_a_type_of (most common)
        cursor = ConceptsRepository.find(
            {"relationships.is_a_type_of": {"$in": parents}}, {"concept_id": 1}
        )
        for doc in cursor:
            cid = doc.get("concept_id")
            if cid and cid not in found and cid != root_type_id:
                found.add(cid)
                queue.append(cid)

        # Find children via has_subtype (less common but possible)
        # We need to query concepts that have has_subtype pointing to... no...
        # We need to query the PARENT documents to see their has_subtype.

        parent_docs = ConceptsRepository.find(
            {"concept_id": {"$in": parents}},
            {"relationships.has_subtype": 1, "relationships.#V#has_subtype": 1},
        )
        for pdoc in parent_docs:
            rels = pdoc.get("relationships") or {}
            targets = _all_relationship_targets(rels, ("has_subtype", "#V#has_subtype"))
            for t in targets:
                if t and t not in found and t != root_type_id:
                    # Verify target exists and is a concept? optional
                    found.add(t)
                    queue.append(t)

        depth += 1

    return found


def discover_workflow_ids() -> List[str]:
    """Find all potential Vontology-defined workflows.

    Heuristic:
    1. Concepts that define a workflow structure (have 'hasInitialStep').
    2. Concepts that are subtypes of known workflow types (#V#durable_workflow).
    """
    candidates = set()

    # 1. Direct property search (best effort, tolerant of DB query limitations)
    try:
        cursor = ConceptsRepository.find(
            {
                "$or": [
                    {"relationships.hasInitialStep": {"$exists": True}},
                    {"relationships.has_initial_step": {"$exists": True}},
                    # Note: Queries for keys with '#' (like #V#has_initial_step)
                    # can be problematic in some MongoDB drivers/versions.
                    # We rely on step 2 for those cases.
                ]
            },
            {"concept_id": 1},
        )
        for doc in cursor:
            if "concept_id" in doc:
                candidates.add(doc["concept_id"])
    except Exception as e:
        logger.warning(f"Error querying workflows by property: {e}")

    # 2. Type-based discovery (more robust for #V# predicates)
    # Find all subtypes of #V#durable_workflow and #V#workflow
    base_types = ["#V#durable_workflow", "#V#workflow"]

    for base in base_types:
        subtypes = _get_recursive_subtypes(base)

        # Fetch them to check if they actually look like workflows (have steps)
        # This filters out abstract categories that don't have steps.
        if subtypes:
            subtype_docs = _fetch_concepts_by_id(list(subtypes))
            for cid, doc in subtype_docs.items():
                rels = doc.get("relationships") or {}
                # Check for initial step predicate in memory (handles #V# correctly)
                initial_step = _first_relationship_target(
                    rels,
                    (
                        "hasInitialStep",
                        "has_initial_step",
                        "#V#hasInitialStep",
                        "#V#has_initial_step",
                    ),
                )
                if initial_step:
                    candidates.add(cid)

    return list(candidates)
