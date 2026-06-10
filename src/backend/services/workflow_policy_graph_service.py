"""Service to resolve workflow model policy from Vontology graph representation.

This module provides an alternative (graph-first) resolution path for workflow model
policies, complementing the JSON-based fallback in the orchestrator.

Per JVNAUTOSCI-998, the policy is now modelled explicitly in Vontology with:
- Stage instances (#V#*_stage) as instances of #V#workflow_stage
- Stage configuration instances (#V#default_*_config) as instances of #V#workflow_stage_configuration
- Predicates: #V#applies_to_workflow_stage, #V#has_primary_model, #V#has_fallback_model,
  #V#has_local_only_constraint, #V#has_stage_configuration, #V#has_max_fallback_hops
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from ..db.repositories.concepts_repository import ConceptsRepository
from .text_value_service import get_texts_for_concept

logger = logging.getLogger(__name__)

# Canonical predicate IDs
PRED_HAS_STAGE_CONFIG = "#V#has_stage_configuration"
PRED_APPLIES_TO_STAGE = "#V#applies_to_workflow_stage"
PRED_HAS_PRIMARY_MODEL = "#V#has_primary_model"
PRED_HAS_FALLBACK_MODEL = "#V#has_fallback_model"
PRED_HAS_LOCAL_ONLY = "#V#has_local_only_constraint"
PRED_HAS_MAX_FALLBACK_HOPS = "#V#has_max_fallback_hops"
PRED_HAS_MODEL_POLICY_JSON = "#V#has_model_policy_json"
PRED_HAS_RUNTIME_STAGE_NAME = "#V#has_runtime_stage_name"

GRAPH_POLICY_COMPLETE = "graph_complete"
GRAPH_POLICY_INCOMPLETE = "graph_incomplete"

_BOOLEAN_TEXT_VALUES = {
    "true": True,
    "1": True,
    "yes": True,
    "false": False,
    "0": False,
    "no": False,
}


def _resolve_stage_name(stage_concept_id: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve the runtime stage name for a stage concept.

    Returns ``(stage_name, source)`` where source is
    ``represented_runtime_stage_name`` when the stage concept carries an
    explicit #V#has_runtime_stage_name text relation, or
    ``derived_from_concept_id`` when the name is derived structurally from the
    concept's own ``#V#<name>_stage`` identifier. No Python stage table is
    consulted (JVNAUTOSCI-2496).
    """

    represented = _get_text_value(stage_concept_id, PRED_HAS_RUNTIME_STAGE_NAME)
    if represented:
        return represented, "represented_runtime_stage_name"
    if stage_concept_id.startswith("#V#") and stage_concept_id.endswith("_stage"):
        derived = stage_concept_id[3:-6]
        if derived:
            return derived, "derived_from_concept_id"
    return None, None


def _get_text_value(concept_id: str, predicate: str) -> Optional[str]:
    """Fetch a single text value for a concept+predicate."""
    texts = get_texts_for_concept(concept_id, predicate=predicate, limit=1)
    if texts and texts[0].get("text"):
        return texts[0]["text"].strip()
    return None


def _get_text_values(concept_id: str, predicate: str) -> List[str]:
    """Fetch all text values for a concept+predicate."""
    texts = get_texts_for_concept(concept_id, predicate=predicate, limit=100)
    return [t["text"].strip() for t in texts if t.get("text")]


def _get_related_concept_ids(concept_id: str, predicate: str) -> List[str]:
    """Fetch concept IDs related via a structural relationship stored as linked_to or similar."""
    concept = ConceptsRepository.find_one({"concept_id": concept_id})
    if not concept:
        return []

    # Check relationships for the predicate
    relationships = concept.get("relationships", {})

    # Try standard relationship storage
    if predicate in relationships:
        val = relationships[predicate]
        if isinstance(val, list):
            return [v for v in val if isinstance(v, str)]
        elif isinstance(val, str):
            return [val]

    # Check linked_to with predicate metadata
    # This is for predicates that store as linked relations
    linked_to = relationships.get("linked_to", [])
    if isinstance(linked_to, list):
        results = []
        for item in linked_to:
            if isinstance(item, dict) and item.get("predicate") == predicate:
                target = item.get("target") or item.get("concept_id")
                if target:
                    results.append(target)
        if results:
            return results

    # Query text_relations for concept-to-concept relations stored there
    # (some predicates store concept IDs as text values)
    text_vals = _get_text_values(concept_id, predicate)
    concept_ids = [v for v in text_vals if v.startswith("#V#")]
    return concept_ids


def resolve_policy_from_graph(policy_concept_id: str) -> Optional[Mapping[str, Any]]:
    """Resolve a workflow model policy from Vontology graph representation.

    This resolver is a support surface: it loads represented policy, validates
    and normalises types, and reports provenance. It does not supply missing
    policy values (JVNAUTOSCI-2496). When required represented values are
    absent or invalid, the returned payload is explicitly marked
    ``graph_incomplete`` with per-value reasons, so callers can distinguish
    fully represented policy from an incomplete graph and fall back to the
    represented JSON policy path with telemetry-visible cause.

    Returns ``None`` only when the policy concept itself does not exist
    (graph absent). Returns a payload with ``completeness`` of
    ``graph_complete`` or ``graph_incomplete`` otherwise.
    """
    try:
        policy_concept = ConceptsRepository.find_one({"concept_id": policy_concept_id})
        if not policy_concept:
            logger.debug(f"Policy concept not found: {policy_concept_id}")
            return None

        incomplete_reasons: List[str] = []
        stage_provenance: Dict[str, Dict[str, Any]] = {}

        config_ids = _get_related_concept_ids(policy_concept_id, PRED_HAS_STAGE_CONFIG)
        if not config_ids:
            incomplete_reasons.append("no_stage_configurations")

        stages: Dict[str, Dict[str, Any]] = {}
        local_only_stages: List[str] = []

        for config_id in config_ids:
            config_concept = ConceptsRepository.find_one({"concept_id": config_id})
            if not config_concept:
                incomplete_reasons.append(f"stage_configuration_missing:{config_id}")
                continue

            stage_ids = _get_related_concept_ids(config_id, PRED_APPLIES_TO_STAGE)
            if not stage_ids:
                incomplete_reasons.append(f"stage_link_missing:{config_id}")
                continue

            stage_concept_id = stage_ids[0]
            stage_name, stage_name_source = _resolve_stage_name(stage_concept_id)
            if not stage_name:
                incomplete_reasons.append(
                    f"stage_name_unresolved:{stage_concept_id}"
                )
                continue

            primary = _get_text_value(config_id, PRED_HAS_PRIMARY_MODEL)
            if not primary:
                incomplete_reasons.append(f"missing_primary_model:{config_id}")
                continue

            fallbacks = _get_text_values(config_id, PRED_HAS_FALLBACK_MODEL)
            local_only_text = _get_text_value(config_id, PRED_HAS_LOCAL_ONLY)
            local_only = False
            local_only_source = None
            if local_only_text is not None:
                normalised = _BOOLEAN_TEXT_VALUES.get(local_only_text.lower())
                if normalised is None:
                    incomplete_reasons.append(
                        f"invalid_local_only_value:{config_id}"
                    )
                else:
                    local_only = normalised
                    local_only_source = config_id

            stages[stage_name] = {
                "primary": primary,
                "fallback": fallbacks,
                "constraints": {"local_only": local_only},
            }
            stage_provenance[stage_name] = {
                "config_concept_id": config_id,
                "stage_concept_id": stage_concept_id,
                "stage_name_source": stage_name_source,
                "primary_source": config_id,
                "local_only_source": local_only_source,
            }

            if local_only:
                local_only_stages.append(stage_name)

        constraints: Dict[str, Any] = {"local_only_stages": local_only_stages}
        max_fallback_hops_text = _get_text_value(
            policy_concept_id, PRED_HAS_MAX_FALLBACK_HOPS
        )
        max_fallback_hops_source = None
        if max_fallback_hops_text is None:
            incomplete_reasons.append("missing_max_fallback_hops")
        else:
            try:
                constraints["max_fallback_hops"] = int(max_fallback_hops_text)
                max_fallback_hops_source = policy_concept_id
            except ValueError:
                incomplete_reasons.append("invalid_max_fallback_hops")

        if not stages and not incomplete_reasons:
            incomplete_reasons.append("no_valid_stages")

        completeness = (
            GRAPH_POLICY_COMPLETE
            if stages and not incomplete_reasons
            else GRAPH_POLICY_INCOMPLETE
        )

        policy = {
            "policy_id": policy_concept_id,
            "stages": stages,
            "constraints": constraints,
            "completeness": completeness,
            "incomplete_reasons": incomplete_reasons,
            "provenance": {
                "stages": stage_provenance,
                "max_fallback_hops_source": max_fallback_hops_source,
            },
            "_source": "graph",
        }

        return policy

    except Exception as e:
        logger.warning(f"Error resolving policy from graph: {e}")
        return None


def compare_policy_json_vs_graph(
    policy_concept_id: str,
) -> Dict[str, Any]:
    """Compare the JSON policy with the graph-resolved policy for diagnostics.

    Returns a comparison report showing differences between the two representations.
    """
    import json

    # Get JSON policy
    json_policy_text = _get_text_value(policy_concept_id, PRED_HAS_MODEL_POLICY_JSON)
    json_policy = None
    if json_policy_text:
        try:
            json_policy = json.loads(json_policy_text)
        except json.JSONDecodeError:
            pass

    # Get graph policy
    graph_policy = resolve_policy_from_graph(policy_concept_id)
    graph_complete = bool(
        graph_policy
        and graph_policy.get("completeness") == GRAPH_POLICY_COMPLETE
    )

    report: Dict[str, Any] = {
        "policy_concept_id": policy_concept_id,
        "json_available": json_policy is not None,
        "graph_available": graph_complete,
        "graph_incomplete_reasons": (
            list(graph_policy.get("incomplete_reasons") or [])
            if graph_policy
            else []
        ),
        "mismatches": [],
        "json_only_stages": [],
        "graph_only_stages": [],
        "matching_stages": [],
    }

    if not json_policy and not graph_complete:
        report["status"] = "neither_available"
        return report

    if not json_policy:
        report["status"] = "json_missing"
        return report

    if not graph_complete:
        report["status"] = "graph_incomplete"
        return report

    # Compare stages
    json_stages = set(json_policy.get("stages", {}).keys())
    graph_stages = set(graph_policy.get("stages", {}).keys())

    report["json_only_stages"] = list(json_stages - graph_stages)
    report["graph_only_stages"] = list(graph_stages - json_stages)

    common_stages = json_stages & graph_stages
    for stage_name in common_stages:
        json_stage = json_policy["stages"][stage_name]
        graph_stage = graph_policy["stages"][stage_name]

        stage_mismatches = []

        # Compare primary
        if json_stage.get("primary") != graph_stage.get("primary"):
            stage_mismatches.append({
                "field": "primary",
                "json": json_stage.get("primary"),
                "graph": graph_stage.get("primary"),
            })

        # Compare fallback (order-sensitive)
        json_fallback = json_stage.get("fallback", [])
        graph_fallback = graph_stage.get("fallback", [])
        if json_fallback != graph_fallback:
            stage_mismatches.append({
                "field": "fallback",
                "json": json_fallback,
                "graph": graph_fallback,
            })

        # Compare local_only constraint
        json_local = json_stage.get("constraints", {}).get("local_only", False)
        graph_local = graph_stage.get("constraints", {}).get("local_only", False)
        if json_local != graph_local:
            stage_mismatches.append({
                "field": "local_only",
                "json": json_local,
                "graph": graph_local,
            })

        if stage_mismatches:
            report["mismatches"].append({
                "stage": stage_name,
                "differences": stage_mismatches,
            })
        else:
            report["matching_stages"].append(stage_name)

    # Compare global constraints
    json_constraints = json_policy.get("constraints", {})
    graph_constraints = graph_policy.get("constraints", {})

    if json_constraints.get("max_fallback_hops") != graph_constraints.get("max_fallback_hops"):
        report["mismatches"].append({
            "global": "max_fallback_hops",
            "json": json_constraints.get("max_fallback_hops"),
            "graph": graph_constraints.get("max_fallback_hops"),
        })

    json_local_stages = set(json_constraints.get("local_only_stages", []))
    graph_local_stages = set(graph_constraints.get("local_only_stages", []))
    if json_local_stages != graph_local_stages:
        report["mismatches"].append({
            "global": "local_only_stages",
            "json": sorted(json_local_stages),
            "graph": sorted(graph_local_stages),
        })

    report["status"] = "match" if not report["mismatches"] and not report["json_only_stages"] and not report["graph_only_stages"] else "mismatch"

    return report
