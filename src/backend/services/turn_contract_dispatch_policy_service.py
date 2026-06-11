"""Represented turn-contract dispatch policy (JVNAUTOSCI-2365).

The dispatch-preflight decision ladder - whether the selected route can stand
or dispatch must override to the general tool workflow to satisfy the turn
expected-outcome contract - is authored behaviour. This service loads that
ladder from the Vontology concept ``#V#turn_contract_dispatch_policy``
(``hasContent`` JSON, schema ``turn_contract_dispatch_policy.v1``), validates
it against a closed vocabulary of structural condition facts, and evaluates it
by generic first-match walking.

Python here is a support surface: it extracts structural facts, validates
types, walks the represented rules, and reports provenance. It does not
supply missing rules; when the represented policy is unavailable or invalid,
resolution reports an explicitly incomplete result and the caller fails
closed (no Python-invented override), with the represented completion gate
remaining the safety net for unsatisfied contracts.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from .text_value_service import get_texts_for_concept

TURN_CONTRACT_DISPATCH_POLICY_CONCEPT_ID = "#V#turn_contract_dispatch_policy"
TURN_CONTRACT_DISPATCH_POLICY_SCHEMA = "turn_contract_dispatch_policy.v1"

DECISION_ALLOW_SELECTED = "allow_selected"
DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW = "override_to_general_tool_workflow"
_VALID_DECISIONS = frozenset(
    {DECISION_ALLOW_SELECTED, DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW}
)

# Closed vocabulary of structural facts the orchestrator can supply. Boolean
# facts match on equality; ``max_required_surface_families`` holds when the
# observed count is less than or equal to the rule's value. Conditions outside
# this vocabulary make the policy invalid (fail closed), never silently true.
_BOOLEAN_FACT_KEYS = frozenset(
    {
        "has_required_tools",
        "selected_prefers_direct_response",
        "selected_uses_tool_pipeline_contract",
        "has_external_surface_families",
        "selector_requests_custom_workflow",
    }
)
_INT_CEILING_FACT_KEYS = frozenset({"max_required_surface_families"})


def _safe_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _validate_rule(rule: Any, index: int) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(rule, Mapping):
        return None, f"rule_{index}_not_a_mapping"
    rule_id = _safe_str(rule.get("rule_id"))
    if not rule_id:
        return None, f"rule_{index}_missing_rule_id"
    decision = _safe_str(rule.get("decision"))
    if decision not in _VALID_DECISIONS:
        return None, f"rule_{rule_id}_invalid_decision"
    status = _safe_str(rule.get("status"))
    if not status:
        return None, f"rule_{rule_id}_missing_status"
    when = rule.get("when")
    if not isinstance(when, Mapping):
        return None, f"rule_{rule_id}_missing_when"
    conditions: dict[str, Any] = {}
    for key, value in when.items():
        key_text = _safe_str(key)
        if key_text in _BOOLEAN_FACT_KEYS:
            if not isinstance(value, bool):
                return None, f"rule_{rule_id}_condition_{key_text}_not_boolean"
            conditions[key_text] = value
        elif key_text in _INT_CEILING_FACT_KEYS:
            if not isinstance(value, int) or isinstance(value, bool):
                return None, f"rule_{rule_id}_condition_{key_text}_not_integer"
            conditions[key_text] = value
        else:
            return None, f"rule_{rule_id}_unknown_condition:{key_text}"
    override_reason = _safe_str(rule.get("override_reason"))
    if decision == DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW and not override_reason:
        return None, f"rule_{rule_id}_missing_override_reason"
    can_satisfy = rule.get("can_satisfy_contract")
    if can_satisfy is not None and not isinstance(can_satisfy, bool):
        return None, f"rule_{rule_id}_invalid_can_satisfy_contract"
    return (
        {
            "rule_id": rule_id,
            "when": conditions,
            "decision": decision,
            "status": status,
            "override_reason": override_reason,
            "reasoning": _safe_str(rule.get("reasoning")),
            "can_satisfy_contract": can_satisfy,
        },
        None,
    )


def resolve_turn_contract_dispatch_policy(
    *,
    policy_concept_id: str | None = None,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any]]:
    """Load and validate the represented dispatch-policy rule ladder.

    Returns ``(rules, diagnostics)``. ``rules`` is None whenever the policy is
    unavailable or invalid; diagnostics carries the reason and provenance.
    """

    concept_id = _safe_str(policy_concept_id) or TURN_CONTRACT_DISPATCH_POLICY_CONCEPT_ID
    diagnostics: dict[str, Any] = {
        "policy_concept_id": concept_id,
        "completeness": "policy_unavailable",
        "error": None,
        "rule_count": 0,
    }
    try:
        rows = get_texts_for_concept(concept_id, predicate="hasContent", limit=1)
    except Exception as exc:
        diagnostics["error"] = f"dispatch_policy_lookup_failed:{exc}"
        return None, diagnostics

    raw_text = None
    if rows and isinstance(rows[0], Mapping):
        raw_text = _safe_str(rows[0].get("text"))
    if not raw_text:
        diagnostics["error"] = "dispatch_policy_content_missing"
        return None, diagnostics

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        diagnostics["error"] = f"dispatch_policy_json_invalid:{exc}"
        diagnostics["completeness"] = "policy_invalid"
        return None, diagnostics

    if not isinstance(payload, Mapping):
        diagnostics["error"] = "dispatch_policy_payload_not_object"
        diagnostics["completeness"] = "policy_invalid"
        return None, diagnostics

    schema = _safe_str(payload.get("schema") or payload.get("schema_version"))
    if schema != TURN_CONTRACT_DISPATCH_POLICY_SCHEMA:
        diagnostics["error"] = "dispatch_policy_schema_invalid"
        diagnostics["schema"] = schema
        diagnostics["completeness"] = "policy_invalid"
        return None, diagnostics

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        diagnostics["error"] = "dispatch_policy_rules_missing"
        diagnostics["completeness"] = "policy_invalid"
        return None, diagnostics

    rules: list[dict[str, Any]] = []
    for index, raw_rule in enumerate(raw_rules):
        rule, error = _validate_rule(raw_rule, index)
        if error:
            diagnostics["error"] = error
            diagnostics["completeness"] = "policy_invalid"
            return None, diagnostics
        assert rule is not None
        rules.append(rule)

    diagnostics["completeness"] = "policy_complete"
    diagnostics["rule_count"] = len(rules)
    return rules, diagnostics


def evaluate_turn_contract_dispatch_rules(
    rules: Sequence[Mapping[str, Any]],
    facts: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Walk the represented rules; return the first matching rule's outcome.

    Returns None when no rule matches (the authored policy should normally end
    with a catch-all rule whose ``when`` is empty).
    """

    for rule in rules:
        conditions = rule.get("when")
        if not isinstance(conditions, Mapping):
            continue
        matched = True
        for key, expected in conditions.items():
            if key in _BOOLEAN_FACT_KEYS:
                if bool(facts.get(key)) is not expected:
                    matched = False
                    break
            elif key in _INT_CEILING_FACT_KEYS:
                observed = facts.get("required_surface_family_count")
                if not isinstance(observed, int) or observed > int(expected):
                    matched = False
                    break
            else:
                matched = False
                break
        if matched:
            return {
                "rule_id": rule.get("rule_id"),
                "decision": rule.get("decision"),
                "status": rule.get("status"),
                "override_reason": rule.get("override_reason"),
                "reasoning": rule.get("reasoning"),
                "can_satisfy_contract": rule.get("can_satisfy_contract"),
            }
    return None


__all__ = [
    "DECISION_ALLOW_SELECTED",
    "DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW",
    "TURN_CONTRACT_DISPATCH_POLICY_CONCEPT_ID",
    "TURN_CONTRACT_DISPATCH_POLICY_SCHEMA",
    "evaluate_turn_contract_dispatch_rules",
    "resolve_turn_contract_dispatch_policy",
]
