from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.backend.services.required_tool_identity_service import (
    canonical_required_tool_key,
    required_tool_names_match,
    resolve_required_tool_identity,
)
from src.backend.services.required_tool_obligation_service import (
    build_required_tool_obligation_ledger,
)
from src.backend.services.selected_workflow_handoff_service import (
    evaluate_workflow_required_effects_tool_policy,
)
from src.backend.services.turn_execution_record_service import _tool_requirement_key
from src.backend.workflows.tool_invocation_evidence import (
    derive_tool_invocation_records_from_step_envelopes,
)


def test_registered_family_qualifier_resolves_without_losing_authored_name() -> None:
    identity = resolve_required_tool_identity("vontology:fetch_concept")

    assert identity.original_name == "vontology:fetch_concept"
    assert identity.canonical_key == "fetch_concept"
    assert identity.operation_name == "fetch_concept"
    assert identity.family_qualifier == "vontology"
    assert identity.canonicalisation_source == "family_qualified_registry_match"
    assert identity.resolution_status == "resolved"


def test_mismatched_or_unknown_qualifier_remains_visible_and_unresolved() -> None:
    identity = resolve_required_tool_identity(
        "gmail:fetch_concept",
        known_tool_names=["fetch_concept"],
    )

    assert identity.canonical_key == "gmail:fetch_concept"
    assert identity.resolution_status == "unresolved"
    assert identity.unresolved_reason == (
        "family_qualifier_does_not_match_registered_surface"
    )
    assert not required_tool_names_match("gmail:fetch_concept", "fetch_concept")


def test_workflow_action_identity_is_exact_and_not_blindly_tail_stripped() -> None:
    identity = resolve_required_tool_identity(
        "scholarly_paper.verify_representation",
        known_tool_names=["scholarly_paper.verify_representation"],
    )

    assert identity.canonical_key == "scholarly_paper.verify_representation"
    assert identity.operation_name == "scholarly_paper.verify_representation"


def _successful_ledger(required: str, observed: str) -> dict:
    return build_required_tool_obligation_ledger(
        required_tools=[required],
        allowed_tools=[observed],
        method_catalogue={observed: {"category": "read"}},
        planned_tool_calls=[{"tool": observed}],
        invocations=[{"tool": observed, "status": "ok", "payload": {}}],
    )


def test_family_qualified_gateway_operations_close_the_same_obligation() -> None:
    for required, observed in (
        ("vontology:fetch_concept", "fetch_concept"),
        ("vontology:read_file_copy", "read_file_copy"),
        ("arxiv:download_paper", "download_paper"),
    ):
        ledger = _successful_ledger(required, observed)
        obligation = ledger["obligations"][0]

        assert obligation["original_required_tool_name"] == required
        assert obligation["canonical_operation_key"] == observed
        assert obligation["allowed_by_workflow_policy"] is True
        assert obligation["available_on_gateway"] is True
        assert obligation["availability_surfaces"] == [
            "gateway",
            "observed_invocation",
        ]
        assert obligation["matched_gateway_names"] == [observed]
        assert obligation["matched_observed_names"] == [observed]
        assert obligation["satisfied"] is True


def test_exact_workflow_action_success_is_distinct_from_gateway_availability() -> None:
    action_id = "scholarly_paper.verify_representation"
    ledger = build_required_tool_obligation_ledger(
        required_tools=[action_id],
        allowed_tools=[action_id],
        method_catalogue={},
        observed_equivalent_successful_executions=[
            {
                "action_id": action_id,
                "workflow_id": "#V#scholarly_paper_representation_workflow",
                "state_id": "verify",
                "status": "success",
                "source": "workflow_step_envelope",
            }
        ],
    )

    obligation = ledger["obligations"][0]
    assert obligation["canonical_operation_key"] == action_id
    assert obligation["available_on_gateway"] is False
    assert obligation["available_on_any_surface"] is True
    assert "workflow_action" in obligation["availability_surfaces"]
    assert obligation["satisfied"] is True
    assert obligation["blocking_reason"] == ""


def test_selected_workflow_policy_matches_qualified_requirement_to_action() -> None:
    action = SimpleNamespace(
        action_id="fetch_concept",
        is_llm_step=False,
        llm_policy=None,
    )
    workflow_def = SimpleNamespace(
        metadata={
            "required_effects_contract": {
                "required_effects": [{"required_tools": ["vontology:fetch_concept"]}]
            }
        },
        states={"run": SimpleNamespace(actions=[action])},
    )

    policy = evaluate_workflow_required_effects_tool_policy(workflow_def)

    assert policy["ok"] is True
    assert policy["unavailable_required_tools"] == []
    assert policy["required_tool_identities"][0]["canonical_key"] == "fetch_concept"
    assert policy["direct_action_tool_canonical_keys"] == ["fetch_concept"]


@pytest.mark.parametrize(
    "input_key",
    ("tool_name", "method_name", "mcp_tool", "mcp_method", "tool"),
)
def test_selected_workflow_policy_accepts_static_workflow_mcp_tool_binding(
    input_key: str,
) -> None:
    action = SimpleNamespace(
        action_id="workflow_mcp.invoke_tool",
        inputs={input_key: "read_file_copy"},
        is_llm_step=False,
        llm_policy=None,
    )
    workflow_def = SimpleNamespace(
        metadata={
            "required_effects_contract": {
                "required_effects": [
                    {"required_tools": ["vontology:read_file_copy"]}
                ]
            }
        },
        states={"run": SimpleNamespace(actions=[action])},
    )

    policy = evaluate_workflow_required_effects_tool_policy(workflow_def)

    assert policy["ok"] is True
    assert policy["unavailable_required_tools"] == []
    assert "read_file_copy" in policy["direct_action_tool_canonical_keys"]


@pytest.mark.parametrize(
    ("inputs", "execution_mode", "is_llm_step"),
    (
        ({"tool_name": "get_text_relations_summary"}, "deterministic", False),
        ({}, "deterministic", False),
        (
            {"tool_name": {"$context_key": "selected_tool_name"}},
            "deterministic",
            False,
        ),
        ({"tool_name": "read_file_copy"}, "subworkflow", False),
        ({"tool_name": "read_file_copy"}, "llm", True),
    ),
)
def test_selected_workflow_policy_rejects_other_or_dynamic_workflow_mcp_tool(
    inputs: dict[str, object],
    execution_mode: str,
    is_llm_step: bool,
) -> None:
    action = SimpleNamespace(
        action_id="workflow_mcp.invoke_tool",
        inputs=inputs,
        execution_mode=execution_mode,
        is_llm_step=is_llm_step,
        llm_policy=None,
    )
    workflow_def = SimpleNamespace(
        metadata={
            "required_effects_contract": {
                "required_effects": [{"required_tools": ["read_file_copy"]}]
            }
        },
        states={"run": SimpleNamespace(actions=[action])},
    )

    policy = evaluate_workflow_required_effects_tool_policy(workflow_def)

    assert policy["ok"] is False
    assert policy["unavailable_required_tools"] == ["read_file_copy"]
    assert policy["reason_code"] == "workflow_required_effect_tool_not_allowed"


def test_step_evidence_uses_the_shared_identity() -> None:
    records = derive_tool_invocation_records_from_step_envelopes(
        [
            {
                "workflow_id": "#V#example",
                "state_id": "fetch",
                "action_id": "fetch_concept",
                "action_outcome": "success",
                "output_payload": {"concept_id": "#V#example"},
            }
        ],
        required_tools=["vontology:fetch_concept"],
    )
    assert len(records) == 1
    assert records[0]["tool"] == "vontology:fetch_concept"


def test_turn_record_requirement_key_delegates_to_shared_identity() -> None:
    assert _tool_requirement_key("vontology:fetch_concept") == "fetch_concept"
    assert _tool_requirement_key("fetch_concept") == "fetch_concept"
    assert canonical_required_tool_key("arxiv:download_paper") == "download_paper"


def test_distinct_predicate_tools_do_not_close_each_others_obligations() -> None:
    assert not required_tool_names_match(
        "get_predicate_extent",
        "get_predicate_incidence",
    )
