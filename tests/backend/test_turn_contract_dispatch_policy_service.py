"""Tests for the represented turn-contract dispatch policy (JVNAUTOSCI-2365)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.backend.services.turn_contract_dispatch_policy_service import (
    DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW,
    TURN_CONTRACT_DISPATCH_POLICY_SCHEMA,
    evaluate_turn_contract_dispatch_rules,
    resolve_turn_contract_dispatch_policy,
)

_SERVICE = "src.backend.services.turn_contract_dispatch_policy_service"
_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "turn_contract_dispatch_policy.json"
)


def _fixture_payload() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _resolve_with(payload) -> tuple:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    with patch(
        f"{_SERVICE}.get_texts_for_concept",
        return_value=[{"text": text}],
    ):
        return resolve_turn_contract_dispatch_policy()


def test_canonical_fixture_resolves_complete():
    rules, diagnostics = _resolve_with(_fixture_payload())
    assert rules is not None
    assert diagnostics["completeness"] == "policy_complete"
    assert diagnostics["rule_count"] == 7


def test_missing_content_fails_closed():
    with patch(f"{_SERVICE}.get_texts_for_concept", return_value=[]):
        rules, diagnostics = resolve_turn_contract_dispatch_policy()
    assert rules is None
    assert diagnostics["error"] == "dispatch_policy_content_missing"
    assert diagnostics["completeness"] == "policy_unavailable"


def test_invalid_schema_and_unknown_condition_fail_closed():
    payload = _fixture_payload()
    payload["schema"] = "wrong.v9"
    rules, diagnostics = _resolve_with(payload)
    assert rules is None
    assert diagnostics["error"] == "dispatch_policy_schema_invalid"

    payload = _fixture_payload()
    payload["rules"][0]["when"]["prompt_mentions_email"] = True
    rules, diagnostics = _resolve_with(payload)
    assert rules is None
    assert "unknown_condition:prompt_mentions_email" in diagnostics["error"]


def test_override_rule_requires_override_reason():
    payload = _fixture_payload()
    del payload["rules"][1]["override_reason"]
    rules, diagnostics = _resolve_with(payload)
    assert rules is None
    assert "missing_override_reason" in diagnostics["error"]


def _facts(**overrides) -> dict:
    facts = {
        "has_required_tools": True,
        "selected_prefers_direct_response": False,
        "selected_uses_tool_pipeline_contract": False,
        "has_external_surface_families": True,
        "selector_requests_custom_workflow": True,
        "required_surface_family_count": 3,
    }
    facts.update(overrides)
    return facts


def test_rule_walk_reproduces_ladder_semantics():
    rules, _ = _resolve_with(_fixture_payload())
    assert rules is not None

    outcome = evaluate_turn_contract_dispatch_rules(
        rules, _facts(has_required_tools=False)
    )
    assert outcome is not None and outcome["status"] == "no_contract_requirements"

    outcome = evaluate_turn_contract_dispatch_rules(
        rules, _facts(selected_prefers_direct_response=True)
    )
    assert outcome is not None
    assert outcome["decision"] == DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW
    assert (
        outcome["override_reason"]
        == "direct_response_route_cannot_satisfy_required_turn_tools"
    )

    outcome = evaluate_turn_contract_dispatch_rules(
        rules, _facts(required_surface_family_count=1)
    )
    assert outcome is not None and outcome["status"] == "single_surface_contract"

    outcome = evaluate_turn_contract_dispatch_rules(
        rules, _facts(has_external_surface_families=False)
    )
    assert outcome is not None
    assert outcome["status"] == "no_external_surface_requirement"

    outcome = evaluate_turn_contract_dispatch_rules(
        rules, _facts(selected_uses_tool_pipeline_contract=True)
    )
    assert outcome is not None
    assert outcome["status"] == "selected_workflow_satisfies_contract"

    outcome = evaluate_turn_contract_dispatch_rules(
        rules, _facts(selector_requests_custom_workflow=False)
    )
    assert outcome is not None and outcome["status"] == "non_custom_route_selected"

    outcome = evaluate_turn_contract_dispatch_rules(rules, _facts())
    assert outcome is not None
    assert outcome["status"] == "override_required"
    assert outcome["decision"] == DECISION_OVERRIDE_TO_GENERAL_TOOL_WORKFLOW
    assert outcome["rule_id"] == "multi_surface_override"


def test_reordered_rules_change_the_decision():
    """The ladder order is authored policy: representation controls outcome."""

    payload = _fixture_payload()
    rules_list = payload["rules"]
    # Move the tool-pipeline acceptance rule ahead of the direct-response
    # override; a direct-response+pipeline workflow is then allowed.
    pipeline_rule = next(
        rule
        for rule in rules_list
        if rule["rule_id"] == "selected_workflow_satisfies_contract"
    )
    rules_list.remove(pipeline_rule)
    rules_list.insert(1, pipeline_rule)
    rules, _ = _resolve_with(payload)
    assert rules is not None

    outcome = evaluate_turn_contract_dispatch_rules(
        rules,
        _facts(
            selected_prefers_direct_response=True,
            selected_uses_tool_pipeline_contract=True,
        ),
    )
    assert outcome is not None
    assert outcome["status"] == "selected_workflow_satisfies_contract"


def test_schema_constant_matches_fixture():
    assert _fixture_payload()["schema"] == TURN_CONTRACT_DISPATCH_POLICY_SCHEMA
