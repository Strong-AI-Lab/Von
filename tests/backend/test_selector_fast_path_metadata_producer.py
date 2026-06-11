"""Tests for the represented selector fast-path metadata producer
(JVNAUTOSCI-2406).

The producer stamps the Vontology-authored fast-path policy and structural
contract-coverage flags into discovery payloads; the JVNAUTOSCI-2401 selector
hook consumes them. Python never decides that a prompt implies a workflow:
coverage is a structural subset comparison between the contract projection's
required tools/actions and each candidate's represented capability metadata.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.backend.services.workflow_discovery_service import (
    SELECTOR_FAST_PATH_POLICY_SCHEMA,
    _attach_selector_fast_path_metadata,
)
from src.backend.workflows.workflow_selector import (
    WorkflowSelectionPrompt,
    WorkflowSelector,
)

_TEXT_VALUE_SERVICE = "src.backend.services.text_value_service"


def _policy_text(**overrides) -> str:
    payload = {
        "schema": SELECTOR_FAST_PATH_POLICY_SCHEMA,
        "enabled": True,
        "rule": "single_unique_executable_candidate",
        "require_contract_coverage": True,
        "treat_missing_required_actions_as_covered": True,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _candidate(concept_id: str, declared_tools, *, action_ids=None) -> dict:
    metadata = {}
    if declared_tools is not None:
        metadata["required_tools"] = declared_tools
    if action_ids is not None:
        metadata["workflow_action_ids"] = action_ids
    return {
        "concept_id": concept_id,
        "name": concept_id,
        "is_executable": True,
        "is_policy_safe": True,
        "routing_eligible": True,
        "routing_index_metadata": metadata,
    }


def _payload(candidates, *, contract_tools, contract_actions=()) -> dict:
    return {
        "matches": candidates,
        "routing_matches": candidates,
        "candidates": candidates,
        "contract_projection": {
            "required_tools": list(contract_tools),
            "required_actions": list(contract_actions),
        },
    }


def _attach(payload, *, policy_text):
    rows = [{"text": policy_text}] if policy_text is not None else []
    with patch(
        f"{_TEXT_VALUE_SERVICE}.get_texts_for_concept", return_value=rows
    ):
        return _attach_selector_fast_path_metadata(payload)


def test_policy_absent_fails_closed_with_diagnostics():
    payload = _attach(
        _payload([_candidate("#V#wf_a", ["search_arxiv"])],
                 contract_tools=["search_arxiv"]),
        policy_text=None,
    )
    assert "selector_fast_path_policy" not in payload
    diagnostics = payload["selector_fast_path_policy_diagnostics"]
    assert diagnostics["error"] == "selector_fast_path_policy_content_missing"


def test_policy_invalid_schema_or_rule_fails_closed():
    payload = _attach(
        _payload([], contract_tools=["search_arxiv"]),
        policy_text=_policy_text(schema="wrong.v2"),
    )
    assert "selector_fast_path_policy" not in payload
    assert (
        payload["selector_fast_path_policy_diagnostics"]["error"]
        == "selector_fast_path_policy_schema_invalid"
    )

    payload = _attach(
        _payload([], contract_tools=["search_arxiv"]),
        policy_text=_policy_text(rule="prompt_keyword_match"),
    )
    assert "selector_fast_path_policy" not in payload
    assert (
        payload["selector_fast_path_policy_diagnostics"]["error"]
        == "selector_fast_path_policy_rule_unsupported"
    )


def test_disabled_policy_is_not_stamped():
    payload = _attach(
        _payload([], contract_tools=["search_arxiv"]),
        policy_text=_policy_text(enabled=False),
    )
    assert "selector_fast_path_policy" not in payload
    assert (
        payload["selector_fast_path_policy_diagnostics"]["status"]
        == "policy_disabled"
    )


def test_coverage_is_structural_subset_with_provenance():
    covered = _candidate(
        "#V#arxiv_workflow", ["search_arxiv", "get_paper_metadata", "extra_tool"]
    )
    uncovered = _candidate("#V#other_workflow", ["fetch_concept"])
    no_metadata = _candidate("#V#opaque_workflow", None)
    payload = _attach(
        _payload(
            [covered, uncovered, no_metadata],
            contract_tools=["search_arxiv", "get_paper_metadata"],
        ),
        policy_text=_policy_text(),
    )

    policy = payload["selector_fast_path_policy"]
    assert policy["authority_concept_id"] == "#V#selector_fast_path_policy"
    assert policy["enabled"] is True

    assert covered["covers_expected_tool_set"] is True
    assert covered["covers_success_contract"] is True
    provenance = covered["selector_fast_path_coverage"]
    assert provenance["policy_concept_id"] == "#V#selector_fast_path_policy"
    assert provenance["tool_coverage_source"] == (
        "routing_index_metadata.required_tools"
    )
    assert provenance["action_coverage_source"] == (
        "represented_policy:treat_missing_required_actions_as_covered"
    )

    assert uncovered["covers_expected_tool_set"] is False
    # No represented capability metadata -> no claim either way (fail closed).
    assert "covers_expected_tool_set" not in no_metadata
    assert "selector_fast_path_coverage" not in no_metadata


def test_required_actions_coverage_uses_workflow_action_ids():
    candidate = _candidate(
        "#V#wf_a",
        ["search_arxiv"],
        action_ids=["arxiv.search", "arxiv.summarise"],
    )
    payload = _attach(
        _payload(
            [candidate],
            contract_tools=["search_arxiv"],
            contract_actions=["arxiv.search"],
        ),
        policy_text=_policy_text(),
    )
    assert candidate["covers_success_contract"] is True

    candidate = _candidate("#V#wf_a", ["search_arxiv"], action_ids=["other.action"])
    payload = _attach(
        _payload(
            [candidate],
            contract_tools=["search_arxiv"],
            contract_actions=["arxiv.search"],
        ),
        policy_text=_policy_text(),
    )
    assert candidate["covers_success_contract"] is False
    assert payload["selector_fast_path_coverage_status"] == "annotated"


def test_no_contract_required_tools_skips_annotation():
    candidate = _candidate("#V#wf_a", ["search_arxiv"])
    payload = _attach(
        _payload([candidate], contract_tools=[]),
        policy_text=_policy_text(),
    )
    assert "covers_expected_tool_set" not in candidate
    assert (
        payload["selector_fast_path_coverage_status"]
        == "no_contract_required_tools"
    )


def test_end_to_end_fast_path_applies_through_selector_hook():
    """Producer output drives the JVNAUTOSCI-2401 hook to an applied fast path
    when exactly one represented candidate covers the contract."""

    covered = _candidate(
        "#V#arxiv_workflow", ["search_arxiv", "get_paper_metadata"]
    )
    uncovered = _candidate("#V#other_workflow", ["fetch_concept"])
    payload = _attach(
        _payload(
            [covered, uncovered],
            contract_tools=["search_arxiv", "get_paper_metadata"],
        ),
        policy_text=_policy_text(),
    )

    selector = WorkflowSelector.__new__(WorkflowSelector)
    selection_prompt = WorkflowSelectionPrompt(
        prompt_id="#V#test_selector_prompt",
        prompt_text="select",
        discovered_workflow_ids=("#V#arxiv_workflow", "#V#other_workflow"),
        candidate_entries=tuple(payload["matches"]),
        policy_recommendation={
            "selector_fast_path_policy": payload["selector_fast_path_policy"]
        },
    )
    evaluation = selector.evaluate_represented_fast_path(
        selection_prompt=selection_prompt
    )

    assert evaluation["applied"] is True
    assert evaluation["selected_workflow_id"] == "#V#arxiv_workflow"
    assert evaluation["authority_source"] == "#V#selector_fast_path_policy"

    # Two covered candidates -> ambiguous -> falls back to the selector LLM.
    second_covered = _candidate(
        "#V#arxiv_workflow_b", ["search_arxiv", "get_paper_metadata"]
    )
    payload = _attach(
        _payload(
            [covered, second_covered],
            contract_tools=["search_arxiv", "get_paper_metadata"],
        ),
        policy_text=_policy_text(),
    )
    selection_prompt = WorkflowSelectionPrompt(
        prompt_id="#V#test_selector_prompt",
        prompt_text="select",
        discovered_workflow_ids=("#V#arxiv_workflow", "#V#arxiv_workflow_b"),
        candidate_entries=tuple(payload["matches"]),
        policy_recommendation={
            "selector_fast_path_policy": payload["selector_fast_path_policy"]
        },
    )
    evaluation = selector.evaluate_represented_fast_path(
        selection_prompt=selection_prompt
    )
    assert evaluation["applied"] is False
    assert evaluation["reason"] == "ambiguous_represented_fast_path_candidates"
