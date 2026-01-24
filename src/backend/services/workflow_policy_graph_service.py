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

# Stage name to concept_id mapping
STAGE_CONCEPT_MAP = {
    "planner": "#V#planner_stage",
    "tool_call": "#V#tool_call_stage",
    "tool_recovery": "#V#tool_recovery_stage",
    "classifier": "#V#classifier_stage",
    "critic": "#V#critic_stage",
    "screen_backfill": "#V#screen_backfill_stage",
    "narration": "#V#narration_stage",
    "summariser": "#V#summariser_stage",
    "buttonify": "#V#buttonify_stage",
}

# Reverse mapping for stage name lookup
CONCEPT_TO_STAGE_MAP = {v: k for k, v in STAGE_CONCEPT_MAP.items()}


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

    Returns a policy dict in the same shape as the JSON policy, or None if
    the graph representation is incomplete.

    Args:
        policy_concept_id: The concept ID of the policy (e.g., #V#default_workflow_model_policy)

    Returns:
        Policy dict with stages, constraints, etc., or None if not resolvable.
    """
    try:
        policy_concept = ConceptsRepository.find_one({"concept_id": policy_concept_id})
        if not policy_concept:
            logger.debug(f"Policy concept not found: {policy_concept_id}")
            return None

        # Get stage configurations linked to this policy
        config_ids = _get_related_concept_ids(policy_concept_id, PRED_HAS_STAGE_CONFIG)
        if not config_ids:
            logger.debug(f"No stage configurations found for policy: {policy_concept_id}")
            return None

        stages: Dict[str, Dict[str, Any]] = {}
        local_only_stages: List[str] = []

        for config_id in config_ids:
            config_concept = ConceptsRepository.find_one({"concept_id": config_id})
            if not config_concept:
                continue

            # Find which stage this config applies to
            stage_ids = _get_related_concept_ids(config_id, PRED_APPLIES_TO_STAGE)
            if not stage_ids:
                continue

            stage_concept_id = stage_ids[0]
            stage_name = CONCEPT_TO_STAGE_MAP.get(stage_concept_id)
            if not stage_name:
                # Try to derive from concept_id
                if stage_concept_id.startswith("#V#") and stage_concept_id.endswith("_stage"):
                    stage_name = stage_concept_id[3:-6]  # Strip #V# prefix and _stage suffix

            if not stage_name:
                logger.debug(f"Unknown stage for config {config_id}: {stage_concept_id}")
                continue

            # Get model settings
            primary = _get_text_value(config_id, PRED_HAS_PRIMARY_MODEL) or "active_llm"
            fallbacks = _get_text_values(config_id, PRED_HAS_FALLBACK_MODEL)
            local_only_text = _get_text_value(config_id, PRED_HAS_LOCAL_ONLY)
            local_only = local_only_text and local_only_text.lower() in ("true", "1", "yes")

            stages[stage_name] = {
                "primary": primary,
                "fallback": fallbacks,
                "constraints": {"local_only": local_only},
            }

            if local_only:
                local_only_stages.append(stage_name)

        if not stages:
            logger.debug(f"No valid stages resolved for policy: {policy_concept_id}")
            return None

        # Get global constraints
        max_fallback_hops_text = _get_text_value(policy_concept_id, PRED_HAS_MAX_FALLBACK_HOPS)
        max_fallback_hops = 2  # default
        if max_fallback_hops_text:
            try:
                max_fallback_hops = int(max_fallback_hops_text)
            except ValueError:
                pass

        policy = {
            "policy_id": policy_concept_id,
            "scope": "global",
            "inherit_from": None,
            "stages": stages,
            "constraints": {
                "local_only_stages": local_only_stages,
                "max_fallback_hops": max_fallback_hops,
            },
            "compatibility": {
                "single_model_default": True,
                "notes": "Policy resolved from Vontology graph representation.",
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

    report: Dict[str, Any] = {
        "policy_concept_id": policy_concept_id,
        "json_available": json_policy is not None,
        "graph_available": graph_policy is not None,
        "mismatches": [],
        "json_only_stages": [],
        "graph_only_stages": [],
        "matching_stages": [],
    }

    if not json_policy and not graph_policy:
        report["status"] = "neither_available"
        return report

    if not json_policy:
        report["status"] = "json_missing"
        return report

    if not graph_policy:
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
