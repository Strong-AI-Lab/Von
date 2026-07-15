from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import run_operational_certification as certification_script
from scripts import run_authenticated_browser_workflow_replay as replay_script
from src.backend.services.operational_certification_contract_service import (
    parse_operational_certification_contract,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPERATIONAL_SEED_PATH = (
    _REPO_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "operational_certification_benchmark_seed_bundle.json"
)
_CANDIDATE_SAFETY_SEED_PATH = (
    _REPO_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "operational_learning_candidate_safety_benchmark_seed_bundle.json"
)


def _contract():
    return parse_operational_certification_contract(
        {
            "definition_schema_version": "benchmark_suite_definition.v1",
            "suite_id": "unit_operational_certification",
            "suite_concept_id": "#V#unit_operational_certification",
            "source": "vontology",
            "default_case_set": "campaign",
            "case_sets": {
                "campaign": [
                    {
                        "schema_version": "operational_certification_scenario.v1",
                        "scenario_id": "represented_lookup",
                        "family_id": "grounded_retrieval",
                        "depends_on": [],
                        "execution": {
                            "adapter_id": (
                                certification_script.AUTHENTICATED_GENERATE_ADAPTER_ID
                            ),
                            "inputs": {"prompt": "Inspect the represented target."},
                        },
                        "evaluator_specs": [
                            {
                                "evaluator_id": "#V#strict_operational_evaluator",
                                "workflow_id": "#V#strict_operational_evaluator",
                                "result_schema_version": (
                                    "represented_operational_evaluator_result.v1"
                                ),
                                "allowed_verdicts": ["pass", "fail", "blocked"],
                                "passing_verdicts": ["pass"],
                                "evidence_required": True,
                                "checks": [
                                    {
                                        "matcher_id": "terminal_state_present",
                                        "kind": "exists",
                                        "path": "/terminal_state",
                                        "expected": True,
                                    }
                                ],
                            }
                        ],
                        "checks": [
                            {
                                "matcher_id": "path_analysis_present",
                                "kind": "exists",
                                "path": "/path_analysis",
                                "expected": True,
                            }
                        ],
                        "minefields": [],
                        "budgets": [],
                        "reset_policy": {"mode": "new_chat_session"},
                        "metadata": {"read_only": True},
                    }
                ]
            },
            "rubric": {
                "operational_certification_policy": {
                    "schema_version": "operational_certification_policy.v1",
                    "trial_count": 5,
                    "pass_windows": [1, 3, 5],
                    "strict_represented_evaluator_results": True,
                    "certification_gates": [
                        {
                            "gate_id": "complete_evaluator_coverage",
                            "matcher": {
                                "matcher_id": "coverage_complete",
                                "kind": "exact",
                                "path": "/represented_evaluator_coverage_rate",
                                "expected": 1.0,
                            },
                        }
                    ],
                }
            },
        }
    )


def _repo_seed_contract(*, case_set: str | None = None):
    return parse_operational_certification_contract(
        json.loads(_OPERATIONAL_SEED_PATH.read_text(encoding="utf-8")),
        case_set=case_set,
    )


def test_generic_runtime_bindings_preserve_exact_json_values_and_find_gaps() -> None:
    bindings = certification_script._parse_runtime_binding_items(
        [
            "candidate_id=candidate-1",
            'candidate_context={"release":"abc","safe":true}',
            "attempt_limit=5",
        ]
    )

    rendered = certification_script._substitute_trial_values(
        {
            "candidate_id": "{{candidate_id}}",
            "candidate_context": "{{candidate_context}}",
            "message": "trial {{trial_index}} for {{candidate_id}}",
            "unresolved": "{{release_sha256}}",
        },
        trial_index=2,
        isolation_id="isolation-2",
        runtime_bindings=bindings,
    )

    assert rendered["candidate_id"] == "candidate-1"
    assert rendered["candidate_context"] == {"release": "abc", "safe": True}
    assert rendered["message"] == "trial 2 for candidate-1"
    assert certification_script._unresolved_runtime_placeholders(rendered) == (
        "release_sha256",
    )


def test_generic_runtime_bindings_reject_reserved_or_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="operational_runtime_binding_invalid"):
        certification_script._parse_runtime_binding_items(["trial_index=7"])
    with pytest.raises(ValueError, match="operational_runtime_binding_duplicate"):
        certification_script._parse_runtime_binding_items(
            ["candidate_id=one", "candidate_id=two"]
        )


def test_candidate_safety_suite_declares_all_exact_runtime_bindings() -> None:
    contract = parse_operational_certification_contract(
        json.loads(_CANDIDATE_SAFETY_SEED_PATH.read_text(encoding="utf-8"))
    )

    assert certification_script._runtime_binding_requirements(contract) == (
        "candidate_id",
        "release_sha256",
        "affected_artifact",
        "namespace",
        "user_id",
        "org_id",
    )


def test_learning_release_candidate_binding_is_projected_into_experiment_metadata() -> (
    None
):
    metadata = certification_script._learning_release_candidate_experiment_metadata(
        {
            "evidence_sha256": "a" * 64,
            "evaluated_learning_release_candidate_bindings": [
                {
                    "candidate_id": "candidate-1",
                    "candidate_release_sha256": "B" * 64,
                }
            ],
        }
    )

    assert metadata == {
        "learning_release_candidate_id": "candidate-1",
        "learning_release_candidate_release_sha256": "b" * 64,
        "learning_release_candidate_binding_source": "represented_campaign_evidence",
        "learning_release_candidate_binding_source_sha256": "a" * 64,
    }


def test_learning_release_candidate_metadata_requires_one_exact_binding() -> None:
    assert (
        certification_script._learning_release_candidate_experiment_metadata(
            {
                "evaluated_learning_release_candidate_bindings": [
                    {
                        "candidate_id": "candidate-1",
                        "candidate_release_sha256": "not-a-sha256",
                    }
                ]
            }
        )
        == {}
    )
    assert (
        certification_script._learning_release_candidate_experiment_metadata(
            {
                "evaluated_learning_release_candidate_bindings": [
                    {
                        "candidate_id": "candidate-1",
                        "candidate_release_sha256": "a" * 64,
                    },
                    {
                        "candidate_id": "candidate-2",
                        "candidate_release_sha256": "b" * 64,
                    },
                ]
            }
        )
        == {}
    )


def test_every_repo_unique_state_scenario_declares_executable_absence_probe() -> None:
    contract = _repo_seed_contract()

    unique_state_scenarios = [
        scenario
        for scenario in contract.scenarios
        if scenario.reset_policy.get("mode") == "unique_state"
    ]

    assert unique_state_scenarios
    for scenario in unique_state_scenarios:
        probe = scenario.reset_policy.get("authoritative_absence_probe")
        assert isinstance(probe, dict)
        assert str(probe.get("workflow_id") or "").startswith("#V#")
        assert "{{isolation_id}}" in json.dumps(probe.get("inputs") or {})
        required_action_ids = probe.get("required_action_ids")
        assert isinstance(required_action_ids, list) and required_action_ids
        assert "llm.action" not in required_action_ids


def test_pilot_unique_state_scenario_declares_exact_postcondition_probe() -> None:
    contract = _repo_seed_contract(case_set="trusted_sail_pilot_v1")
    scenario = next(
        candidate
        for candidate in contract.scenarios
        if candidate.scenario_id == "pilot_unique_state_marker_create_and_read_back"
    )

    probe = scenario.reset_policy["authoritative_postcondition_probe"]
    assert probe == {
        "workflow_id": "#V#operational_marker_readback_probe_workflow",
        "inputs": {"isolation_id": "{{isolation_id}}"},
        "required_action_ids": [
            "workflow_mcp.invoke_tool",
            "workflow_control.context_project",
        ],
        "required_tool_names": [
            "resolve_concept_by_name",
            "fetch_concept",
            "get_text_relations",
        ],
        "expected_cardinality": 1,
        "expected_name": "Operational certification {{isolation_id}}",
        "expected_description": (
            "Trusted SAIL pilot certification marker {{isolation_id}}"
        ),
    }


def _postcondition_probe_fixture(
    *,
    isolation_id: str = "isolation-123",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, str]]:
    workflow_id = "#V#synthetic_postcondition_probe_workflow"
    expected_name = f"Synthetic target {isolation_id}"
    expected_description = f"Synthetic description {isolation_id}"
    resolved_concept_id = "#V#synthetic_target"
    probe_spec = {
        "workflow_id": workflow_id,
        "inputs": {"isolation_id": isolation_id},
        "required_action_ids": [
            "workflow_mcp.invoke_tool",
            "workflow_control.context_project",
        ],
        "required_tool_names": [
            "synthetic_resolve",
            "synthetic_fetch",
            "synthetic_text_read",
        ],
        "expected_cardinality": 1,
        "expected_name": expected_name,
        "expected_description": expected_description,
    }
    probe_inputs = {"isolation_id": isolation_id}
    scope = {
        "namespace": "unit-namespace",
        "user_id": "#V#unit_user",
        "org_id": "#V#unit_org",
        "source_event_type": "operational_certification_postcondition_probe",
        "source_event_id": "campaign:scenario:1:postcondition",
        "event_idempotency_key": "postcondition-idempotency-key",
    }
    probe_result = {
        "schema_version": "represented_operational_state_probe_result.v1",
        "isolation_id": isolation_id,
        "namespace": scope["namespace"],
        "target_absent": False,
        "target_present": True,
        "resolution_status": "resolved",
        "resolved_concept_id": resolved_concept_id,
        "resolution_candidates": [resolved_concept_id],
        "expected_name": expected_name,
        "expected_description": expected_description,
        "readback_concept_id": resolved_concept_id,
        "names": [{"name": expected_name, "language": "en-NZ"}],
        "content": expected_description,
        "text_relation_groups": [
            {"predicate": "synthetic_name", "texts": [expected_name]},
            {
                "predicate": "synthetic_description",
                "texts": [expected_description],
            },
        ],
        "evidence": [
            {"kind": "resolution", "tool": "synthetic_resolve"},
            {
                "kind": "readback",
                "tools": ["synthetic_fetch", "synthetic_text_read"],
            },
        ],
    }
    result_sha256 = certification_script.stable_payload_digest(probe_result)
    action_evidence = [
        {
            "action_id": "workflow_mcp.invoke_tool",
            "status": "success",
            "requested_tool_name": tool_name,
            "resolved_tool_name": tool_name,
            "inputs_sha256": f"{tool_name}-inputs",
            "outputs_sha256": f"{tool_name}-outputs",
        }
        for tool_name in probe_spec["required_tool_names"]
    ]
    action_evidence.append(
        {
            "action_id": "workflow_control.context_project",
            "status": "success",
            "inputs_sha256": "project-inputs",
            "outputs_sha256": "project-outputs",
            "state_probe_result_sha256": result_sha256,
        }
    )
    workflow_output = {"represented_operational_state_probe_result": probe_result}
    payload = {
        "success": True,
        "trace_persisted": True,
        "workflow_authority_source": "vontology",
        "workflow_id": workflow_id,
        "workflow_definition_identity_sha256": "definition-digest",
        "workflow_output_sha256": certification_script.stable_payload_digest(
            workflow_output
        ),
        "execution_trace_id": "postcondition-trace",
        "workflow_inputs_sha256": certification_script.stable_payload_digest(
            probe_inputs
        ),
        "exact_scope": scope,
        "exact_scope_sha256": certification_script.stable_payload_digest(scope),
        "workflow_action_evidence": action_evidence,
        "workflow_output": {"represented_operational_state_probe_result": probe_result},
    }
    return probe_spec, probe_inputs, payload, scope


def _evaluate_postcondition_fixture(
    probe_spec: Mapping[str, Any],
    probe_inputs: Mapping[str, Any],
    payload: Mapping[str, Any],
    scope: Mapping[str, str],
) -> dict[str, Any]:
    return certification_script._evaluate_authoritative_postcondition_probe_execution(
        probe_spec=probe_spec,
        probe_inputs=probe_inputs,
        payload=payload,
        isolation_id=str(probe_inputs["isolation_id"]),
        namespace=scope["namespace"],
        user_id=scope["user_id"],
        org_id=scope["org_id"],
        source_event_type=scope["source_event_type"],
        source_event_id=scope["source_event_id"],
        event_idempotency_key=scope["event_idempotency_key"],
    )


def test_authoritative_postcondition_probe_accepts_exact_represented_readback() -> None:
    probe_spec, probe_inputs, payload, scope = _postcondition_probe_fixture()

    evidence = _evaluate_postcondition_fixture(
        probe_spec,
        probe_inputs,
        payload,
        scope,
    )

    assert evidence["verified"] is True
    assert evidence["reason"] is None
    assert all(evidence["checks"].values())
    assert evidence["observed_state"]["cardinality"] == 1
    assert evidence["observed_state"]["resolved_concept_id"] == ("#V#synthetic_target")
    assert set(evidence["represented_evidence"]["tool_names"]) == {
        "synthetic_resolve",
        "synthetic_fetch",
        "synthetic_text_read",
    }
    evidence_without_hash = dict(evidence)
    evidence_sha256 = evidence_without_hash.pop("evidence_sha256")
    assert evidence_sha256 == certification_script.stable_payload_digest(
        evidence_without_hash
    )
    encoded = json.dumps(evidence)
    assert probe_spec["expected_name"] not in encoded
    assert probe_spec["expected_description"] not in encoded


@pytest.mark.parametrize(
    ("mismatch", "failed_check"),
    [
        ("name", "name_readback_exact"),
        ("description", "description_readback_exact"),
    ],
)
def test_authoritative_postcondition_probe_fails_closed_on_exact_value_mismatch(
    mismatch: str,
    failed_check: str,
) -> None:
    probe_spec, probe_inputs, payload, scope = _postcondition_probe_fixture()
    probe_result = payload["workflow_output"][
        "represented_operational_state_probe_result"
    ]
    if mismatch == "name":
        probe_result["names"] = [{"name": "different name"}]
        probe_result["text_relation_groups"][0]["texts"] = ["different name"]
    else:
        probe_result["content"] = "different description"
        probe_result["text_relation_groups"][1]["texts"] = ["different description"]
    result_sha256 = certification_script.stable_payload_digest(probe_result)
    payload["workflow_action_evidence"][-1]["state_probe_result_sha256"] = result_sha256
    payload["workflow_output_sha256"] = certification_script.stable_payload_digest(
        payload["workflow_output"]
    )

    evidence = _evaluate_postcondition_fixture(
        probe_spec,
        probe_inputs,
        payload,
        scope,
    )

    assert evidence["verified"] is False
    assert evidence["reason"] == "authoritative_postcondition_probe_unverified"
    assert evidence["checks"][failed_check] is False
    assert evidence["checks"]["projected_result_causal_action_evidence"] is True


def test_authoritative_postcondition_probe_fails_closed_on_cardinality_mismatch() -> (
    None
):
    probe_spec, probe_inputs, payload, scope = _postcondition_probe_fixture()
    probe_result = payload["workflow_output"][
        "represented_operational_state_probe_result"
    ]
    probe_result["resolution_candidates"].append("#V#duplicate_target")
    result_sha256 = certification_script.stable_payload_digest(probe_result)
    payload["workflow_action_evidence"][-1]["state_probe_result_sha256"] = result_sha256
    payload["workflow_output_sha256"] = certification_script.stable_payload_digest(
        payload["workflow_output"]
    )

    evidence = _evaluate_postcondition_fixture(
        probe_spec,
        probe_inputs,
        payload,
        scope,
    )

    assert evidence["verified"] is False
    assert evidence["observed_state"]["cardinality"] == 2
    assert evidence["checks"]["cardinality_exact"] is False
    assert evidence["checks"]["name_readback_exact"] is True
    assert evidence["checks"]["description_readback_exact"] is True


@pytest.mark.parametrize(
    ("mismatch", "failed_check"),
    [
        ("isolation", "isolation_id_exact"),
        ("scope", "exact_authenticated_scope"),
    ],
)
def test_authoritative_postcondition_probe_fails_closed_on_scope_mismatch(
    mismatch: str,
    failed_check: str,
) -> None:
    probe_spec, probe_inputs, payload, scope = _postcondition_probe_fixture()
    if mismatch == "isolation":
        probe_result = payload["workflow_output"][
            "represented_operational_state_probe_result"
        ]
        probe_result["isolation_id"] = "different-isolation"
        payload["workflow_action_evidence"][-1]["state_probe_result_sha256"] = (
            certification_script.stable_payload_digest(probe_result)
        )
        payload["workflow_output_sha256"] = certification_script.stable_payload_digest(
            payload["workflow_output"]
        )
    else:
        payload["exact_scope"] = {**payload["exact_scope"], "namespace": "other"}
        payload["exact_scope_sha256"] = certification_script.stable_payload_digest(
            payload["exact_scope"]
        )

    evidence = _evaluate_postcondition_fixture(
        probe_spec,
        probe_inputs,
        payload,
        scope,
    )

    assert evidence["verified"] is False
    assert evidence["checks"][failed_check] is False


@pytest.mark.parametrize(
    ("mismatch", "failed_checks"),
    [
        (
            "action",
            {"required_actions_executed", "projected_result_causal_action_evidence"},
        ),
        ("tool", {"required_tools_executed"}),
    ],
)
def test_authoritative_postcondition_probe_requires_causal_action_and_tool_evidence(
    mismatch: str,
    failed_checks: set[str],
) -> None:
    probe_spec, probe_inputs, payload, scope = _postcondition_probe_fixture()
    if mismatch == "action":
        payload["workflow_action_evidence"] = payload["workflow_action_evidence"][:-1]
    else:
        payload["workflow_action_evidence"][0]["resolved_tool_name"] = "different_tool"

    evidence = _evaluate_postcondition_fixture(
        probe_spec,
        probe_inputs,
        payload,
        scope,
    )

    assert evidence["verified"] is False
    assert all(evidence["checks"][check] is False for check in failed_checks)


def _args(**overrides: Any) -> argparse.Namespace:
    values = {
        "base_url": "http://127.0.0.1:5001",
        "namespace": "unit-namespace",
        "user_concept_id": "#V#unit_user",
        "organisation_concept_id": "#V#unit_org",
        "model": "",
        "timeout_seconds": 10.0,
        "poll_interval_seconds": 0.0,
        "allow_non_agent_test_server": False,
        "experiment_run_id": "",
        "campaign_evidence_concept_id": "#V#unit_campaign_evidence",
        "migration_fixture": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _represented_selector_diagnostics() -> dict[str, Any]:
    return {
        "workflow_selection": {
            "selected_workflow_id": "#V#entity_representation_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "workflow_selector",
        },
        "workflow_routing_diagnostics": {
            "selected_workflow_id": "#V#entity_representation_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "selector",
            "selector": {
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "prompt_provenance": {
                    "source": "vontology_prompt_relation",
                    "resolved_prompt_id": "#V#chat_turn_classifier_prompt",
                    "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                },
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "model_name": "gpt-5.6-luna",
                "selection_resolution": "candidate_label_exact_match",
                "selection_metadata": {
                    "selected_workflow_id": "#V#entity_representation_workflow",
                    "requested_candidate_workflow_id": (
                        "#V#entity_representation_workflow"
                    ),
                },
                "telemetry_completeness": {
                    "prompt_present": True,
                    "candidate_list_present": True,
                    "response_present": True,
                    "candidate_entries_present": True,
                    "context_lineage_present": True,
                    "selector_prompt_entry_count": 1,
                    "selector_response_entry_count": 1,
                    "missing_fields": [],
                },
            },
        },
        "decision_attribution": {
            "schema_version": "turn_decision_attribution.v1",
            "decisions": [
                {
                    "decision_kind": "selection",
                    "authority": "represented",
                    "concept_ids": ["#V#entity_representation_workflow"],
                    "evidence": {
                        "selected_workflow_id": ("#V#entity_representation_workflow"),
                    },
                }
            ],
        },
    }


def test_represented_selector_evidence_accepts_final_represented_selection() -> None:
    evidence = certification_script._represented_selector_evidence(
        _represented_selector_diagnostics()
    )

    assert evidence["complete"] is True
    assert evidence["missing_fields"] == []
    assert evidence["selector_source"] == "workflow_selector"
    assert evidence["selector_verdict"] == "rag_selected"
    assert evidence["selection_resolution"] == "candidate_label_exact_match"
    assert evidence["selection_authority"] == "represented"
    assert evidence["selector_selected_workflow_id"] == (
        "#V#entity_representation_workflow"
    )
    assert evidence["final_workflow_ids"] == ["#V#entity_representation_workflow"]
    assert evidence["selection_attribution_workflow_ids"] == [
        "#V#entity_representation_workflow"
    ]


def test_represented_selector_evidence_rejects_complete_python_fallback() -> None:
    turn_record = _represented_selector_diagnostics()
    turn_record["workflow_selection"].update(
        {
            "selector_source": "selector_override",
            "selector_verdict": "rag_selected",
        }
    )
    routing = turn_record["workflow_routing_diagnostics"]
    routing["selector_source"] = "selector_override"
    routing["selector"][
        "selection_resolution"
    ] = "single_specialised_candidate_recovery_from_selector_fallback"
    turn_record["decision_attribution"]["decisions"][0]["authority"] = "python_fallback"

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selector_source_represented",
        "selection_resolution_represented",
        "selection_attribution_represented",
    ]
    assert evidence["field_status"]["prompt_present"] is True
    assert evidence["field_status"]["response_present"] is True
    assert evidence["field_status"]["model_name_present"] is True
    assert evidence["selection_authority"] == "python_fallback"


def test_represented_selector_evidence_fails_closed_on_missing_lineage() -> None:
    turn_record = _represented_selector_diagnostics()
    turn_record["workflow_routing_diagnostics"]["selector"]["telemetry_completeness"][
        "context_lineage_present"
    ] = False

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == ["context_lineage_present"]
    assert evidence["requested_replay_mode"] == "represented_selector_llm"


def test_represented_selector_evidence_requires_prompt_provenance() -> None:
    turn_record = _represented_selector_diagnostics()
    selector = turn_record["workflow_routing_diagnostics"]["selector"]
    selector["prompt_id"] = None
    selector["prompt_provenance"] = {}
    selector["requested_prompt_ids"] = []

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selector_prompt_id_present",
        "selector_prompt_provenance_bound",
        "selector_prompt_requested_id_bound",
    ]


def test_represented_selector_evidence_rejects_unbound_prompt_provenance() -> None:
    turn_record = _represented_selector_diagnostics()
    selector = turn_record["workflow_routing_diagnostics"]["selector"]
    selector["prompt_provenance"] = {"truncated": False}

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selector_prompt_provenance_bound",
        "selector_prompt_requested_id_bound",
    ]


def test_represented_selector_evidence_binds_selected_workflow_identity() -> None:
    turn_record = _represented_selector_diagnostics()
    turn_record["workflow_selection"]["selected_workflow_id"] = "#V#final_workflow"
    turn_record["workflow_routing_diagnostics"][
        "selected_workflow_id"
    ] = "#V#final_workflow"

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == ["final_selection_identity_bound"]
    assert evidence["selector_selected_workflow_id"] == (
        "#V#entity_representation_workflow"
    )
    assert evidence["final_workflow_ids"] == ["#V#final_workflow"]


def test_represented_selector_evidence_binds_attribution_identity() -> None:
    turn_record = _represented_selector_diagnostics()
    selection = turn_record["decision_attribution"]["decisions"][0]
    selection["concept_ids"] = ["#V#different_workflow"]
    selection["evidence"]["selected_workflow_id"] = "#V#different_workflow"

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selection_attribution_identity_bound",
        "selection_attribution_concept_identity_bound",
        "selection_attribution_evidence_identity_bound",
    ]
    assert evidence["selection_attribution_workflow_ids"] == ["#V#different_workflow"]


def test_represented_selector_evidence_requires_model_selected_identity() -> None:
    turn_record = _represented_selector_diagnostics()
    metadata = turn_record["workflow_routing_diagnostics"]["selector"][
        "selection_metadata"
    ]
    metadata.pop("selected_workflow_id")

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selector_selected_workflow_id_present",
        "final_selection_identity_bound",
        "selection_attribution_identity_bound",
        "selection_attribution_concept_identity_bound",
        "selection_attribution_evidence_identity_bound",
    ]


def test_represented_selector_evidence_requires_both_final_identity_surfaces() -> None:
    turn_record = _represented_selector_diagnostics()
    turn_record["workflow_selection"].pop("selected_workflow_id")

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "workflow_selection_id_present",
        "final_selection_identity_bound",
    ]


def test_represented_selector_evidence_rejects_contradictory_routing_source() -> None:
    turn_record = _represented_selector_diagnostics()
    routing = turn_record["workflow_routing_diagnostics"]
    routing["selector_source"] = "selector_override"
    routing["selector_verdict"] = "tool_contract_override"

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selector_source_represented",
        "selector_source_surfaces_bound",
        "selector_verdict_surfaces_bound",
    ]


@pytest.mark.parametrize(
    "selection_resolution",
    ["launch_contract_override", "reasoning_candidate_override"],
)
def test_represented_selector_evidence_rejects_nonrepresented_selection_resolution(
    selection_resolution: str,
) -> None:
    turn_record = _represented_selector_diagnostics()
    turn_record["workflow_routing_diagnostics"]["selector"]["selection_resolution"] = (
        selection_resolution
    )

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == ["selection_resolution_represented"]


def test_represented_selector_evidence_requires_both_attribution_id_surfaces() -> None:
    missing_fields_by_removed_field = {
        "concept_ids": [
            "selection_attribution_concept_identity_bound",
        ],
        "evidence": [
            "selection_attribution_evidence_identity_bound",
        ],
    }
    for (
        removed_field,
        expected_missing_fields,
    ) in missing_fields_by_removed_field.items():
        turn_record = _represented_selector_diagnostics()
        selection = turn_record["decision_attribution"]["decisions"][0]
        selection.pop(removed_field)

        evidence = certification_script._represented_selector_evidence(turn_record)

        assert evidence["complete"] is False
        assert evidence["missing_fields"] == expected_missing_fields


def test_represented_selector_evidence_requires_distinct_events_and_model() -> None:
    turn_record = _represented_selector_diagnostics()
    selector = turn_record["workflow_routing_diagnostics"]["selector"]
    selector["model_name"] = None
    selector["telemetry_completeness"]["selector_prompt_entry_count"] = 0
    selector["telemetry_completeness"]["selector_response_entry_count"] = 0

    evidence = certification_script._represented_selector_evidence(turn_record)

    assert evidence["complete"] is False
    assert evidence["missing_fields"] == [
        "selector_prompt_event_present",
        "selector_response_event_present",
        "model_name_present",
    ]


def _patch_live_preflight(
    monkeypatch: pytest.MonkeyPatch,
    *,
    actor_contract: Any | None = None,
) -> None:
    experiment_spec_state = {"experiment_spec_id": ""}

    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY",
        "unit-test-certification-secret-1234567890",
    )
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID",
        "unit-test-key-1",
    )
    monkeypatch.setattr(certification_script.requests, "Session", object)
    monkeypatch.setattr(
        certification_script,
        "collect_run_environment",
        lambda **_kwargs: {"server_agent_test_instance": True},
    )
    monkeypatch.setattr(
        certification_script,
        "require_agent_test_server",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        certification_script,
        "_collect_runtime_authority_alignment",
        lambda **_kwargs: {
            "schema_version": "operational_certification_runtime_alignment.v1",
            "verified": True,
            "alignment_sha256": "runtime-alignment-digest",
            "server_git_commit": "a" * 40,
            "local_git_commit": "a" * 40,
        },
    )
    monkeypatch.setattr(
        certification_script,
        "get_auth_status",
        lambda **_kwargs: {"authenticated": True},
    )
    monkeypatch.setattr(
        certification_script,
        "apply_target_session_context",
        lambda **_kwargs: {
            "target_session_context_ready": True,
            "session_context": {
                "authenticated": True,
                "user_id": "#V#unit_user",
                "organisation_id": "#V#unit_org",
                "namespace": "unit-namespace",
            },
        },
    )
    monkeypatch.setattr(
        certification_script,
        "_canonical_tool_catalogue_digest",
        lambda: "tool-catalogue-digest",
    )

    def _create_experiment_spec(**kwargs: Any) -> dict[str, Any]:
        experiment_spec_id = kwargs["experiment_spec_id"]
        experiment_spec_state["experiment_spec_id"] = experiment_spec_id
        return {"success": True, "experiment_spec_id": experiment_spec_id}

    monkeypatch.setattr(
        certification_script,
        "create_experiment_spec",
        _create_experiment_spec,
    )
    monkeypatch.setattr(
        certification_script,
        "start_experiment_run",
        lambda **_kwargs: {"success": True, "run_id": "run-1"},
    )
    monkeypatch.setattr(
        certification_script,
        "compute_experiment_verdict",
        lambda **_kwargs: {"success": True, "verdict": "pass"},
    )

    def _completed_experiment_state(run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "experiment_spec_id": experiment_spec_state["experiment_spec_id"],
            "namespace": "unit-namespace",
            "user_id": "#V#unit_user",
            "org_id": "#V#unit_org",
            "status": "completed",
            "verdict": "pass",
            "observations": [{"index": index} for index in range(6)],
        }

    monkeypatch.setattr(
        certification_script,
        "get_experiment_run_state",
        _completed_experiment_state,
    )
    monkeypatch.setattr(
        certification_script,
        "load_represented_operational_campaign_evidence",
        lambda **_kwargs: {
            "source": "vontology",
            "authority": {"concept_id": "#V#unit_campaign_evidence"},
            "evidence_sha256": "campaign-evidence-digest",
        },
    )
    monkeypatch.setattr(
        certification_script,
        "load_operational_certification_contract",
        lambda **_kwargs: actor_contract or _contract(),
    )


def test_offline_observation_dossier_is_never_release_eligible(
    tmp_path: Path,
) -> None:
    dossier_path = tmp_path / "observations.json"
    dossier_path.write_text(
        json.dumps({"trial_observations": []}),
        encoding="utf-8",
    )

    execution = certification_script._offline_execution(_contract(), dossier_path)

    assert execution["mode"] == "offline_evidence_evaluation"
    assert execution["release_eligibility"] == {
        "eligible": False,
        "reason_code": "offline_evidence_not_release_eligible",
    }
    campaign = execution["campaign_result"]
    assert campaign["certified"] is False
    assert "live_release_evidence_eligible" in campaign["failed_certification_gate_ids"]
    assert "offline_evidence_not_release_eligible" in {
        blocker["code"] for blocker in campaign["blockers"]
    }


@pytest.mark.parametrize(
    ("failure_stage", "expected_code"),
    [
        ("spec", "certification_experiment_spec_persistence_failed"),
        ("run", "certification_experiment_run_persistence_failed"),
    ],
)
def test_live_execution_requires_explicit_persistence_success_receipts(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_code: str,
) -> None:
    _patch_live_preflight(monkeypatch)
    campaign_called = False

    if failure_stage == "spec":
        monkeypatch.setattr(
            certification_script,
            "create_experiment_spec",
            lambda **_kwargs: {"experiment_spec_id": "spec-without-success"},
        )
    else:
        monkeypatch.setattr(
            certification_script,
            "start_experiment_run",
            lambda **_kwargs: {"run_id": "run-without-success"},
        )

    def unexpected_campaign(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal campaign_called
        campaign_called = True
        return {}

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        unexpected_campaign,
    )

    execution = certification_script._live_execution(_args(), _contract())

    assert campaign_called is False
    assert execution["release_eligibility"] == {
        "eligible": False,
        "reason_code": expected_code,
    }
    assert execution["campaign_result"]["blockers"][0]["code"] == expected_code


def test_live_trials_use_unique_sessions_and_bind_turn_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    chat_session_ids: list[str] = []
    submissions: list[dict[str, Any]] = []
    fetched_records: list[tuple[str, str]] = []
    evaluator_calls: list[dict[str, Any]] = []
    persisted_observations: list[dict[str, Any]] = []
    experiment_spec_ids: list[str] = []

    def create_spec(**kwargs: Any) -> dict[str, Any]:
        experiment_spec_id = kwargs["experiment_spec_id"]
        experiment_spec_ids.append(experiment_spec_id)
        return {"success": True, "experiment_spec_id": experiment_spec_id}

    def create_chat(**_kwargs: Any) -> dict[str, Any]:
        session_id = f"chat-{len(chat_session_ids) + 1}"
        chat_session_ids.append(session_id)
        return {"session_id": session_id}

    def submit(**kwargs: Any) -> dict[str, Any]:
        submissions.append(dict(kwargs))
        index = len(submissions)
        return {"task_id": f"task-{index}", "request_id": f"request-{index}"}

    def fetch(**kwargs: Any) -> dict[str, Any]:
        fetched_records.append((kwargs["chat_session_id"], kwargs["request_id"]))
        return {
            "schema_version": "turn_execution_record.v1",
            "session_id": kwargs["chat_session_id"],
            "request_id": kwargs["request_id"],
            "namespace": "unit-namespace",
            "completion_gate": {
                "decision": "complete",
                "safe_to_claim_completion": True,
            },
            "terminal_outcome_receipt": {"committed_effects": []},
            "final_response": {"completion_claim_detected": False},
            **_represented_selector_diagnostics(),
        }

    def execute_evaluator_workflow(**kwargs: Any) -> dict[str, Any]:
        evaluator_calls.append(dict(kwargs))
        inputs = kwargs["inputs"]
        observation = inputs["trial_observation"]
        return {
            "success": True,
            "trace_persisted": True,
            "workflow_authority_source": "vontology",
            "workflow_definition_identity_sha256": "definition-digest",
            "trace_document_sha256": "trace-document-digest",
            "evaluator_policy_identity": {
                "schema_version": "represented_evaluator_policy_identity.v1",
                "identity_sha256": "policy-digest",
            },
            "transport": "synchronous_workflow_executor",
            "execution_trace_id": f"trace-{inputs['trial_index']}",
            "workflow_output_sha256": f"output-{inputs['trial_index']}",
            "workflow_output": {
                "represented_operational_evaluator_result": {
                    "schema_version": "represented_operational_evaluator_result.v1",
                    "evaluator_id": "#V#strict_operational_evaluator",
                    "scenario_id": "represented_lookup",
                    "trial_index": inputs["trial_index"],
                    "verdict": "pass",
                    "terminal_state": "verified",
                    "evidence": [
                        {
                            "kind": "turn_execution_record",
                            "ref": observation["turn_execution_request_ids"][0],
                        }
                    ],
                    "fabricated_evidence": [],
                    "forbidden_effects": [],
                    "namespace_violations": [],
                    "false_success_claims": [],
                }
            },
        }

    monkeypatch.setattr(certification_script, "create_replay_chat_session", create_chat)
    monkeypatch.setattr(certification_script, "create_experiment_spec", create_spec)
    monkeypatch.setattr(certification_script, "submit_background_generate", submit)
    monkeypatch.setattr(
        certification_script,
        "poll_replay_task",
        lambda **kwargs: {
            "task_result": {"request_id": kwargs["request_id"], "answer": "done"},
            "last_task_status": {"status": "completed"},
            "task_statuses": [{"status": "completed"}],
            "progress_snapshots": [],
            "timed_out": False,
        },
    )
    monkeypatch.setattr(certification_script, "fetch_turn_record", fetch)
    monkeypatch.setattr(
        certification_script,
        "extract_visible_answer",
        lambda _payload: "done",
    )
    monkeypatch.setattr(
        certification_script,
        "_execute_represented_workflow_synchronously",
        execute_evaluator_workflow,
    )
    monkeypatch.setattr(
        certification_script,
        "record_experiment_observation",
        lambda **kwargs: (
            persisted_observations.append(dict(kwargs))
            or {"success": True, "observation_id": f"obs-{len(persisted_observations)}"}
        ),
    )

    def _canonical_completed_run(run_id: str) -> dict[str, Any]:
        contract = _contract()
        return {
            "run_id": run_id,
            "experiment_spec_id": experiment_spec_ids[-1],
            "namespace": "unit-namespace",
            "user_id": "#V#unit_user",
            "org_id": "#V#unit_org",
            "status": "completed",
            "verdict": "partial",
            "metadata": {
                "suite_concept_id": contract.suite_concept_id,
                "contract_sha256": contract.contract_sha256,
                "source_definition_sha256": contract.source_definition_sha256,
            },
            "observations": [entry["observations"] for entry in persisted_observations],
        }

    monkeypatch.setattr(
        certification_script,
        "get_experiment_run_state",
        _canonical_completed_run,
    )
    monkeypatch.setattr(
        certification_script,
        "compute_experiment_verdict",
        lambda **_kwargs: {"success": True, "verdict": "partial"},
    )

    execution = certification_script._live_execution(_args(), _contract())

    assert execution["campaign_result"]["certified"] is True
    assert execution["release_eligibility"]["eligible"] is True
    assert execution["experiment_finalisation"]["verdict"] == "partial"
    assert len(chat_session_ids) == 5
    assert len(set(chat_session_ids)) == 5
    assert [call["conversation_session_id"] for call in submissions] == (
        chat_session_ids
    )
    assert all(
        call["agent_test_selector_replay_mode"] == "represented_selector_llm"
        for call in submissions
    )
    assert fetched_records == [
        (session_id, f"request-{index}")
        for index, session_id in enumerate(chat_session_ids, start=1)
    ]
    reset_evidence = [
        observation["reset_evidence"] for observation in execution["trial_observations"]
    ]
    assert all(item["state_isolation_verified"] is True for item in reset_evidence)
    assert len({item["isolation_id"] for item in reset_evidence}) == 5
    assert len(evaluator_calls) == 5
    assert len({call["event_idempotency_key"] for call in evaluator_calls}) == 5
    assert all(call["namespace"] == "unit-namespace" for call in evaluator_calls)
    assert all(call["user_id"] == "#V#unit_user" for call in evaluator_calls)
    assert all(call["org_id"] == "#V#unit_org" for call in evaluator_calls)
    assert all(
        call["execution_metadata"]["scenario_id"] == "represented_lookup"
        for call in evaluator_calls
    )
    assert len(persisted_observations) == 6
    assert all(
        observation["path_analysis"]["selector_path_evidence"]["complete"] is True
        for observation in execution["trial_observations"]
    )


def test_reused_chat_session_is_not_accepted_as_isolated_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    reset_results: list[dict[str, Any]] = []
    monkeypatch.setattr(
        certification_script,
        "create_replay_chat_session",
        lambda **_kwargs: {"session_id": "reused-chat"},
    )

    def inspect_resets(contract, *, reset_scenario, **_kwargs):
        scenario = contract.scenarios[0]
        reset_results.extend(
            [dict(reset_scenario(scenario, 1)), dict(reset_scenario(scenario, 2))]
        )
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        inspect_resets,
    )

    certification_script._live_execution(_args(), _contract())

    assert reset_results[0]["success"] is True
    assert reset_results[1]["success"] is False
    assert reset_results[1]["state_isolation_verified"] is False
    assert reset_results[1]["reason"] == "conversation_session_missing_or_reused"


@pytest.mark.parametrize(
    ("adapter_id", "inputs", "reset_policy", "expected_reason"),
    [
        (
            certification_script.AUTHENTICATED_GENERATE_ADAPTER_ID,
            {"prompt": "Mutate the same fixed target."},
            {"mode": "unique_state"},
            "unique_state_template_required",
        ),
        (
            certification_script.AUTHENTICATED_GENERATE_ADAPTER_ID,
            {"prompt": "Mutate target {{isolation_id}}."},
            {
                "mode": "unique_state",
                "isolation_binding_paths": ["/prompt"],
            },
            "authoritative_absence_probe_required",
        ),
        (
            certification_script.DURABLE_WORKFLOW_ADAPTER_ID,
            {
                "workflow_id": "#V#unit_durable_workflow",
                "workflow_inputs": {"target": "fixed-target"},
            },
            {"mode": "read_only"},
            "durable_reset_requires_read_only_or_unique_state_template",
        ),
        (
            certification_script.DURABLE_WORKFLOW_ADAPTER_ID,
            {
                "workflow_id": "#V#unit_durable_workflow",
                "workflow_inputs": {"target": "fixed-target"},
                "irrelevant_note": "{{isolation_id}}",
            },
            {"mode": "unique_state"},
            "durable_reset_requires_read_only_or_unique_state_template",
        ),
    ],
)
def test_mutating_reset_requires_an_executable_unique_state_strategy(
    monkeypatch: pytest.MonkeyPatch,
    adapter_id: str,
    inputs: dict[str, Any],
    reset_policy: dict[str, Any],
    expected_reason: str,
) -> None:
    base_contract = _contract()
    scenario = replace(
        base_contract.scenarios[0],
        execution={"adapter_id": adapter_id, "inputs": inputs},
        reset_policy=reset_policy,
        permitted_effects=({"effect_type": "mutation"},),
        metadata={"read_only": False},
    )
    contract = replace(base_contract, scenarios=(scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    monkeypatch.setattr(
        certification_script,
        "create_replay_chat_session",
        lambda **_kwargs: {"session_id": "unique-chat"},
    )
    reset_result: dict[str, Any] = {}

    def inspect_reset(run_contract, *, reset_scenario, **_kwargs: Any):
        reset_result.update(reset_scenario(run_contract.scenarios[0], 1))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        inspect_reset,
    )

    certification_script._live_execution(_args(), contract)

    assert reset_result["success"] is False
    assert reset_result.get("state_isolation_verified") is not True
    assert reset_result["reason"] == expected_reason


@pytest.mark.parametrize(
    ("resolver_status", "expected_success"),
    [("not_found", True), ("resolved", False)],
)
def test_unique_state_reset_requires_causal_authoritative_absence_readback(
    monkeypatch: pytest.MonkeyPatch,
    resolver_status: str,
    expected_success: bool,
) -> None:
    base_contract = _contract()
    scenario = replace(
        base_contract.scenarios[0],
        execution={
            "adapter_id": certification_script.AUTHENTICATED_GENERATE_ADAPTER_ID,
            "inputs": {"prompt": "Mutate target {{isolation_id}}."},
        },
        reset_policy={
            "mode": "unique_state",
            "isolation_binding_paths": ["/prompt"],
            "authoritative_absence_probe": {
                "workflow_id": "#V#operational_marker_absence_probe_workflow",
                "inputs": {"isolation_id": "{{isolation_id}}"},
                "required_action_ids": [
                    "workflow_mcp.invoke_tool",
                    "workflow_control.context_project",
                ],
            },
        },
        permitted_effects=({"effect_type": "mutation"},),
        metadata={"read_only": False},
    )
    contract = replace(base_contract, scenarios=(scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    monkeypatch.setattr(
        certification_script,
        "create_replay_chat_session",
        lambda **_kwargs: {"session_id": "isolated-chat"},
    )

    def _probe(**kwargs: Any) -> dict[str, Any]:
        isolation_id = kwargs["inputs"]["isolation_id"]
        assert kwargs["require_policy_identity"] is False
        reported_resolution_lineage = {
            "schema_version": "operational_absence_probe_resolution_lineage.v1",
            "tool": "resolve_concept_by_name",
            "target_name": f"Operational certification {isolation_id}",
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
        }
        action_resolution_lineage = {
            **reported_resolution_lineage,
            "status": resolver_status,
            "resolved_concept_id": (
                "#V#existing_marker" if resolver_status == "resolved" else None
            ),
        }
        probe_result = {
            "schema_version": "represented_operational_state_probe_result.v1",
            "isolation_id": isolation_id,
            "namespace": "unit-namespace",
            "target_absent": True,
            "evidence": [
                {
                    **reported_resolution_lineage,
                    "kind": "canonical_exact_name_resolution",
                }
            ],
        }
        return {
            "success": True,
            "trace_persisted": True,
            "workflow_authority_source": "vontology",
            "execution_trace_id": "probe-trace",
            "workflow_definition_identity_sha256": "probe-definition-digest",
            "workflow_output_sha256": "probe-output-digest",
            "workflow_action_evidence": [
                {
                    "action_id": "workflow_mcp.invoke_tool",
                    "status": "success",
                    "resolution_lineage_sha256": (
                        certification_script.stable_payload_digest(
                            action_resolution_lineage
                        )
                    ),
                },
                {
                    "action_id": "workflow_control.context_project",
                    "status": "success",
                    "state_probe_result_sha256": (
                        certification_script.stable_payload_digest(probe_result)
                    ),
                },
            ],
            "workflow_output": {
                "represented_operational_state_probe_result": probe_result
            },
        }

    monkeypatch.setattr(
        certification_script,
        "_execute_represented_workflow_synchronously",
        _probe,
    )
    reset_result: dict[str, Any] = {}

    def _inspect_reset(run_contract, *, reset_scenario, **_kwargs: Any):
        reset_result.update(reset_scenario(run_contract.scenarios[0], 1))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        _inspect_reset,
    )

    certification_script._live_execution(_args(), contract)

    assert reset_result["success"] is expected_success
    assert reset_result["state_isolation_verified"] is expected_success
    absence_probe = reset_result["authoritative_absence_probe"]
    assert absence_probe["execution_trace_id"] == "probe-trace"
    assert absence_probe["checks"]["resolver_output_causal_lineage_exact"] is (
        expected_success
    )
    if expected_success:
        assert (
            reset_result["pre_state_snapshot"][
                "authoritative_absence_readback_verified"
            ]
            is True
        )
    else:
        assert reset_result["reason"] == "authoritative_absence_probe_unverified"


def test_declared_postcondition_probe_runs_after_primary_trial_and_is_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_contract = _contract()
    scenario = replace(
        base_contract.scenarios[0],
        execution={
            "adapter_id": certification_script.AUTHENTICATED_GENERATE_ADAPTER_ID,
            "inputs": {"prompt": "Mutate synthetic target {{isolation_id}}."},
        },
        reset_policy={
            "mode": "unique_state",
            "isolation_binding_paths": ["/prompt"],
            "authoritative_absence_probe": {
                "workflow_id": "#V#synthetic_absence_probe_workflow",
                "inputs": {"isolation_id": "{{isolation_id}}"},
                "required_action_ids": [
                    "workflow_mcp.invoke_tool",
                    "workflow_control.context_project",
                ],
            },
            "authoritative_postcondition_probe": {
                "workflow_id": "#V#synthetic_postcondition_probe_workflow",
                "inputs": {"isolation_id": "{{isolation_id}}"},
                "required_action_ids": [
                    "workflow_mcp.invoke_tool",
                    "workflow_control.context_project",
                ],
                "required_tool_names": [
                    "synthetic_resolve",
                    "synthetic_fetch",
                    "synthetic_text_read",
                ],
                "expected_cardinality": 1,
                "expected_name": "Synthetic target {{isolation_id}}",
                "expected_description": ("Synthetic description {{isolation_id}}"),
            },
        },
        permitted_effects=({"effect_type": "synthetic_mutation"},),
        metadata={"read_only": False},
    )
    contract = replace(base_contract, scenarios=(scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    order: list[str] = []
    observed_execution: dict[str, Any] = {}

    monkeypatch.setattr(
        certification_script,
        "create_replay_chat_session",
        lambda **_kwargs: {"session_id": "isolated-chat"},
    )

    def _represented_workflow(**kwargs: Any) -> dict[str, Any]:
        isolation_id = kwargs["inputs"]["isolation_id"]
        if kwargs["source_event_type"] == "operational_certification_absence_probe":
            order.append("absence_probe")
            lineage = {
                "schema_version": "operational_absence_probe_resolution_lineage.v1",
                "tool": "synthetic_resolve",
                "target_name": f"Synthetic target {isolation_id}",
                "status": "not_found",
                "resolved_concept_id": None,
                "candidates": [],
            }
            probe_result = {
                "schema_version": "represented_operational_state_probe_result.v1",
                "isolation_id": isolation_id,
                "namespace": "unit-namespace",
                "target_absent": True,
                "evidence": [{**lineage, "kind": "resolution"}],
            }
            return {
                "success": True,
                "trace_persisted": True,
                "workflow_authority_source": "vontology",
                "execution_trace_id": "absence-trace",
                "workflow_definition_identity_sha256": "absence-definition",
                "workflow_output_sha256": "absence-output",
                "workflow_action_evidence": [
                    {
                        "action_id": "workflow_mcp.invoke_tool",
                        "status": "success",
                        "resolution_lineage_sha256": (
                            certification_script.stable_payload_digest(lineage)
                        ),
                    },
                    {
                        "action_id": "workflow_control.context_project",
                        "status": "success",
                        "state_probe_result_sha256": (
                            certification_script.stable_payload_digest(probe_result)
                        ),
                    },
                ],
                "workflow_output": {
                    "represented_operational_state_probe_result": probe_result
                },
            }

        assert kwargs["source_event_type"] == (
            "operational_certification_postcondition_probe"
        )
        order.append("postcondition_probe")
        _probe_spec, _probe_inputs, payload, _scope = _postcondition_probe_fixture(
            isolation_id=isolation_id
        )
        exact_scope = {
            "namespace": kwargs["namespace"],
            "user_id": kwargs["user_id"],
            "org_id": kwargs["org_id"],
            "source_event_type": kwargs["source_event_type"],
            "source_event_id": kwargs["source_event_id"],
            "event_idempotency_key": kwargs["event_idempotency_key"],
        }
        payload["exact_scope"] = exact_scope
        payload["exact_scope_sha256"] = certification_script.stable_payload_digest(
            exact_scope
        )
        payload["workflow_inputs_sha256"] = certification_script.stable_payload_digest(
            kwargs["inputs"]
        )
        return payload

    monkeypatch.setattr(
        certification_script,
        "_execute_represented_workflow_synchronously",
        _represented_workflow,
    )

    def _submit(**_kwargs: Any) -> dict[str, Any]:
        order.append("primary_trial")
        return {"task_id": "task-1", "request_id": "request-1"}

    monkeypatch.setattr(
        certification_script,
        "submit_background_generate",
        _submit,
    )
    monkeypatch.setattr(
        certification_script,
        "poll_replay_task",
        lambda **_kwargs: {
            "task_result": {"answer": "synthetic mutation completed"},
            "last_task_status": {"status": "completed"},
            "task_statuses": [{"status": "completed"}],
            "progress_snapshots": [],
            "timed_out": False,
        },
    )
    monkeypatch.setattr(
        certification_script,
        "fetch_turn_record",
        lambda **kwargs: {
            "schema_version": "turn_execution_record.v1",
            "session_id": kwargs["chat_session_id"],
            "request_id": kwargs["request_id"],
            "namespace": kwargs["namespace"],
            "completion_gate": {
                "decision": "complete",
                "safe_to_claim_completion": True,
            },
            "terminal_outcome_receipt": {
                "outcome": "complete",
                "committed_effects": [],
            },
            "final_response": {"completion_claim_detected": False},
            **_represented_selector_diagnostics(),
        },
    )
    monkeypatch.setattr(
        certification_script,
        "extract_visible_answer",
        lambda _payload: "synthetic mutation completed",
    )

    def _inspect_execution(
        run_contract,
        *,
        reset_scenario,
        execute_scenario,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        run_scenario = run_contract.scenarios[0]
        reset = reset_scenario(run_scenario, 1)
        assert reset["success"] is True
        observed_execution.update(execute_scenario(run_scenario, 1, reset))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        _inspect_execution,
    )

    certification_script._live_execution(_args(), contract)

    assert order == ["absence_probe", "primary_trial", "postcondition_probe"]
    postcondition = observed_execution["authoritative_postcondition_probe"]
    assert postcondition["verified"] is True
    assert postcondition["checks"]["exact_authenticated_scope"] is True
    assert postcondition["checks"]["required_tools_executed"] is True
    assert (
        observed_execution["final_state_snapshot"][
            "authoritative_postcondition_probe_verified"
        ]
        is True
    )
    assert (
        observed_execution["final_state_snapshot"][
            "authoritative_postcondition_probe_evidence_sha256"
        ]
        == postcondition["evidence_sha256"]
    )


def test_multi_turn_scenarios_reuse_one_isolated_session_and_preserve_each_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_contract = _contract()
    base_scenario = base_contract.scenarios[0]
    multi_scenario = replace(
        base_scenario,
        execution={
            "adapter_id": certification_script.AUTHENTICATED_MULTI_TURN_ADAPTER_ID,
            "inputs": {
                "turns": [
                    {"prompt": "Inspect the represented target."},
                    {"prompt": "Now verify the same target from prior evidence."},
                ]
            },
        },
    )
    contract = replace(base_contract, scenarios=(multi_scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    chat_session_ids: list[str] = []
    submitted_turns: list[tuple[str, str]] = []

    def create_chat(**_kwargs: Any) -> dict[str, Any]:
        session_id = f"multi-chat-{len(chat_session_ids) + 1}"
        chat_session_ids.append(session_id)
        return {"session_id": session_id}

    def submit(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["agent_test_selector_replay_mode"] == ("represented_selector_llm")
        submitted_turns.append(
            (kwargs["conversation_session_id"], kwargs["case"].prompt)
        )
        index = len(submitted_turns)
        return {"task_id": f"task-{index}", "request_id": f"request-{index}"}

    def fetch(**kwargs: Any) -> dict[str, Any]:
        return {
            "schema_version": "turn_execution_record.v1",
            "session_id": kwargs["chat_session_id"],
            "request_id": kwargs["request_id"],
            "namespace": "unit-namespace",
            "completion_gate": {
                "decision": "complete",
                "safe_to_claim_completion": True,
            },
            "terminal_outcome_receipt": {"committed_effects": []},
            "final_response": {"completion_claim_detected": False},
            **_represented_selector_diagnostics(),
        }

    def evaluator(**kwargs: Any) -> dict[str, Any]:
        inputs = kwargs["inputs"]
        return {
            "success": True,
            "trace_persisted": True,
            "workflow_authority_source": "vontology",
            "workflow_definition_identity_sha256": "definition-digest",
            "trace_document_sha256": "trace-document-digest",
            "evaluator_policy_identity": {
                "schema_version": "represented_evaluator_policy_identity.v1",
                "identity_sha256": "policy-digest",
            },
            "transport": "synchronous_workflow_executor",
            "execution_trace_id": f"trace-{inputs['trial_index']}",
            "workflow_output_sha256": f"output-{inputs['trial_index']}",
            "workflow_output": {
                "represented_operational_evaluator_result": {
                    "schema_version": "represented_operational_evaluator_result.v1",
                    "evaluator_id": "#V#strict_operational_evaluator",
                    "scenario_id": "represented_lookup",
                    "trial_index": inputs["trial_index"],
                    "verdict": "pass",
                    "terminal_state": "verified",
                    "evidence": [{"kind": "multi_turn_path", "ref": "turns"}],
                    "fabricated_evidence": [],
                    "forbidden_effects": [],
                    "namespace_violations": [],
                    "false_success_claims": [],
                }
            },
        }

    monkeypatch.setattr(certification_script, "create_replay_chat_session", create_chat)
    monkeypatch.setattr(certification_script, "submit_background_generate", submit)
    monkeypatch.setattr(
        certification_script,
        "poll_replay_task",
        lambda **kwargs: {
            "task_result": {"request_id": kwargs["request_id"], "answer": "done"},
            "last_task_status": {"status": "completed"},
            "task_statuses": [{"status": "completed"}],
            "progress_snapshots": [],
            "timed_out": False,
        },
    )
    monkeypatch.setattr(certification_script, "fetch_turn_record", fetch)
    monkeypatch.setattr(
        certification_script,
        "extract_visible_answer",
        lambda payload: f"answer-{payload['request_id']}",
    )
    monkeypatch.setattr(
        certification_script,
        "_execute_represented_workflow_synchronously",
        evaluator,
    )
    monkeypatch.setattr(
        certification_script,
        "record_experiment_observation",
        lambda **_kwargs: {"success": True, "observation_id": "stored"},
    )

    execution = certification_script._live_execution(_args(), contract)

    assert len(chat_session_ids) == 5
    assert len(submitted_turns) == 10
    for trial_index, session_id in enumerate(chat_session_ids):
        trial_submissions = submitted_turns[trial_index * 2 : trial_index * 2 + 2]
        assert [item[0] for item in trial_submissions] == [session_id, session_id]
        assert [item[1] for item in trial_submissions] == [
            "Inspect the represented target.",
            "Now verify the same target from prior evidence.",
        ]
    assert all(
        observation["conversation_turn_count"] == 2
        and len(observation["turn_execution_request_ids"]) == 2
        and len(observation["path_analysis"]["turns"]) == 2
        and observation["operational_metrics"]["follow_up_request_count"] == 0
        and observation["operational_metrics"]["follow_up_request_count_evidence_kind"]
        == "completion_gate_proxy"
        and observation["operational_metrics"]["correction_count"] is None
        and observation["operational_metrics"]["correction_count_complete"] is False
        for observation in execution["trial_observations"]
    )


def test_repo_seed_breadth_scenarios_match_supported_runner_adapter_contracts() -> None:
    contract = _repo_seed_contract(case_set="executable_engineering_seed")
    scenarios = {scenario.scenario_id: scenario for scenario in contract.scenarios}

    multi_turn = scenarios["represented_workflow_concept_same_session_followup"]
    multi_inputs = multi_turn.execution["inputs"]
    turns = multi_inputs["turns"]
    assert multi_turn.execution["adapter_id"] == (
        certification_script.AUTHENTICATED_MULTI_TURN_ADAPTER_ID
    )
    assert multi_turn.reset_policy == {"mode": "new_chat_session"}
    assert multi_turn.metadata["read_only"] is True
    assert multi_turn.permitted_effects == ()
    assert len(turns) == 3
    assert all(str(turn.get("prompt") or "").strip() for turn in turns)
    assert [turn["role"] for turn in turns] == [
        "task",
        "bare_followup",
        "explicit_continuation",
    ]

    durable = scenarios["durable_concept_profile_resume_and_idempotence"]
    durable_inputs = durable.execution["inputs"]
    submission_plan = durable_inputs["submission_plan"]
    assert (
        durable.execution["adapter_id"]
        == certification_script.DURABLE_WORKFLOW_ADAPTER_ID
    )
    assert durable.reset_policy == {"mode": "read_only"}
    assert durable.metadata["read_only"] is True
    assert durable.permitted_effects == ()
    assert durable_inputs["workflow_id"] == (
        "#V#concept_search_instance_retrieval_workflow"
    )
    assert str(durable_inputs["workflow_inputs"]["prompt"]).strip()
    assert [step["await_terminal"] for step in submission_plan] == [False, True]
    assert [step["timeout_seconds"] for step in submission_plan] == [0.0, 120.0]


def _synthetic_turn_result(
    index: int,
    *,
    request_id: str | None = None,
    model_cost_units: int | None = 1,
    tool_evidence_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "terminal_state": "verified_success",
        "visible_answer": f"answer-{index}",
        "path_analysis": {
            "selected_workflow_ids": [f"#V#workflow_{index}"],
            "observed_workflow_ids": [f"#V#workflow_{index}"],
            "observed_tool_names": ["get_concept"],
            "progress_fact_count": 1,
        },
        "forbidden_mutations": [],
        "namespace_violations": [],
        "false_success_claims": [],
        "execution_budget_units": 1,
        "operational_metrics": {
            "duration_ms": 10,
            "timeout": False,
            "model_cost_units": model_cost_units,
            "tool_cost_units": 1,
            "follow_up_request_count": 0,
            "follow_up_request_count_applicable": True,
            "follow_up_request_count_complete": True,
            "follow_up_request_count_evidence_kind": "completion_gate_proxy",
            "clarification_count": 0,
            "clarification_count_applicable": True,
            "clarification_count_complete": True,
            "clarification_count_evidence_kind": "follow_up_request_proxy",
            "correction_count": None,
            "correction_count_applicable": True,
            "correction_count_complete": False,
            "correction_count_evidence_kind": "missing",
        },
        "tool_evidence_projection": tool_evidence_projection,
        "turn_execution_request_ids": [request_id or f"request-{index}"],
        "submission": {"task_id": f"task-{index}"},
        "task_evidence": {},
        "turn_execution_record": {"request_id": request_id or f"request-{index}"},
        "final_state_snapshot": {"isolation_id": "isolation"},
    }


def test_multi_turn_aggregation_rejects_reused_request_evidence() -> None:
    with pytest.raises(
        ValueError,
        match="multi_turn_evidence_identifier_reused:request_id",
    ):
        certification_script._aggregate_authenticated_multi_turn_results(
            [
                _synthetic_turn_result(1, request_id="reused-request"),
                _synthetic_turn_result(2, request_id="reused-request"),
            ]
        )


def test_multi_turn_aggregation_marks_partial_cost_measurement_incomplete() -> None:
    aggregated = certification_script._aggregate_authenticated_multi_turn_results(
        [
            _synthetic_turn_result(1, model_cost_units=2),
            _synthetic_turn_result(2, model_cost_units=None),
        ]
    )

    metrics = aggregated["operational_metrics"]
    assert metrics["model_cost_units"] is None
    assert metrics["model_cost_units_complete"] is False
    assert metrics["tool_cost_units"] == 2
    assert metrics["tool_cost_units_complete"] is True


def test_multi_turn_aggregation_collects_bounded_tool_evidence_in_turn_order() -> None:
    projections = [
        {
            "schema_version": "tool_evidence_projection_reachability.v1",
            "projection_count": 1,
            "entries": [
                {
                    "tool": "synthetic_lookup",
                    "projected_payload": {"fact": f"fact-{index}"},
                }
            ],
        }
        for index in (1, 2)
    ]

    aggregated = certification_script._aggregate_authenticated_multi_turn_results(
        [
            _synthetic_turn_result(1, tool_evidence_projection=projections[0]),
            _synthetic_turn_result(2, tool_evidence_projection=projections[1]),
        ]
    )

    assert aggregated["tool_evidence_projection_count"] == 2
    assert [
        item["tool_evidence_projection"]["entries"][0]["projected_payload"]["fact"]
        for item in aggregated["tool_evidence_projections"]
    ] == ["fact-1", "fact-2"]
    assert all(
        turn["tool_evidence_projection_available"] is True
        for turn in aggregated["path_analysis"]["turns"]
    )
    metrics = aggregated["operational_metrics"]
    assert metrics["follow_up_request_count"] == 0
    assert metrics["follow_up_request_count_complete"] is True
    assert metrics["clarification_count"] == 0
    assert metrics["clarification_count_evidence_kind"] == ("follow_up_request_proxy")
    assert metrics["correction_count"] is None
    assert metrics["correction_count_complete"] is False


def test_turn_record_evaluator_projection_includes_only_public_tool_evidence() -> None:
    private_prompt = "private evaluator prompt"
    turn_record = {
        "schema_version": "turn_execution_record.v1",
        "request_id": "request-1",
        "final_answer_synthesis": {
            "request": {"prompt": private_prompt},
            "tool_evidence_projection": {
                "schema_version": "tool_evidence_projection_reachability.v1",
                "projection_count": 1,
                "tools": ["synthetic_lookup"],
                "entries": [
                    {
                        "tool": "synthetic_lookup",
                        "projected_payload": {"fact": "represented safe fact"},
                    }
                ],
            },
        },
    }

    projection = certification_script._turn_record_evidence_projection(turn_record)

    assert projection["tool_evidence_projection"]["entries"][0][
        "projected_payload"
    ] == {"fact": "represented safe fact"}
    assert "final_answer_synthesis" not in projection
    assert private_prompt not in json.dumps(projection)


def test_turn_record_must_match_submitted_session_and_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    execution_errors: list[str] = []
    monkeypatch.setattr(
        certification_script,
        "create_replay_chat_session",
        lambda **_kwargs: {"session_id": "chat-1"},
    )
    monkeypatch.setattr(
        certification_script,
        "submit_background_generate",
        lambda **_kwargs: {"task_id": "task-1", "request_id": "request-1"},
    )
    monkeypatch.setattr(
        certification_script,
        "poll_replay_task",
        lambda **_kwargs: {
            "task_result": {},
            "last_task_status": {"status": "completed"},
        },
    )
    monkeypatch.setattr(
        certification_script,
        "fetch_turn_record",
        lambda **_kwargs: {
            "session_id": "different-chat",
            "request_id": "different-request",
        },
    )

    def inspect_execution(
        contract,
        *,
        reset_scenario,
        execute_scenario,
        **_kwargs,
    ):
        scenario = contract.scenarios[0]
        reset = reset_scenario(scenario, 1)
        try:
            execute_scenario(scenario, 1, reset)
        except RuntimeError as exc:
            execution_errors.append(str(exc))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        inspect_execution,
    )

    certification_script._live_execution(_args(), _contract())

    assert execution_errors == [
        "turn_execution_record_binding_mismatch:request_id,session_id,namespace"
    ]


def test_durable_submission_plan_preserves_resume_and_idempotency_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.integrations.internal_mcp import catalogue

    base_contract = _contract()
    durable_scenario = replace(
        base_contract.scenarios[0],
        execution={
            "adapter_id": certification_script.DURABLE_WORKFLOW_ADAPTER_ID,
            "inputs": {
                "workflow_id": "#V#unit_durable_workflow",
                "workflow_inputs": {"target": "stable-read-only-target"},
                "submission_plan": [
                    {"await_terminal": False, "timeout_seconds": 0.0},
                    {"await_terminal": True, "timeout_seconds": 5.0},
                ],
            },
        },
        reset_policy={"mode": "read_only"},
        metadata={"read_only": True},
    )
    contract = replace(base_contract, scenarios=(durable_scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    workflow_calls: list[dict[str, Any]] = []
    observed_execution: dict[str, Any] = {}

    def workflow_execute(**kwargs: Any) -> dict[str, Any]:
        workflow_calls.append(dict(kwargs))
        terminal = kwargs["await_terminal"] is True
        return {
            "success": True,
            "instance_id": "shared-instance-1",
            "created_new": not terminal,
            "status": "reused" if terminal else "pending",
            "final_status": "completed" if terminal else None,
            "timed_out": False,
            "workflow_instance": {
                "instance_id": "shared-instance-1",
                "namespace": "unit-namespace",
                "user_id": "#V#unit_user",
                "org_id": "#V#unit_org",
                "status": "completed" if terminal else "pending",
                "min_worker_build": "a" * 40,
                "claimed_by_build": (
                    {"git_commit": "a" * 40} if terminal else None
                ),
            },
            "workflow_execution": {
                "instance_id": "shared-instance-1",
                "final_status": "completed" if terminal else None,
            },
        }

    def inspect_execution(
        run_contract,
        *,
        reset_scenario,
        execute_scenario,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        scenario = run_contract.scenarios[0]
        reset = reset_scenario(scenario, 1)
        observed_execution.update(execute_scenario(scenario, 1, reset))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(catalogue, "_workflow_execute", workflow_execute)
    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        inspect_execution,
    )

    certification_script._live_execution(
        _args(model="gpt-5.4-mini"),
        contract,
    )

    assert [call["await_terminal"] for call in workflow_calls] == [False, True]
    assert [call["timeout_seconds"] for call in workflow_calls] == [0.0, 5.0]
    assert all(call["required_worker_build"] == "a" * 40 for call in workflow_calls)
    assert all(
        call["inputs"]
        == {
            "target": "stable-read-only-target",
            "requested_model": "gpt-5.4-mini",
        }
        for call in workflow_calls
    )
    assert len({call["event_idempotency_key"] for call in workflow_calls}) == 1
    assert observed_execution["terminal_state"] == "completed"
    assert observed_execution["path_analysis"]["submission_count"] == 2
    assert observed_execution["path_analysis"]["required_worker_build"] == "a" * 40
    assert observed_execution["path_analysis"]["workflow_instance_ids"] == [
        "shared-instance-1",
        "shared-instance-1",
    ]
    assert (
        observed_execution["path_analysis"]["idempotent_instance_reuse_observed"]
        is True
    )
    assert observed_execution["operational_metrics"]["timeout"] is False
    assert observed_execution["operational_metrics"]["follow_up_request_count"] is None
    assert (
        observed_execution["operational_metrics"]["follow_up_request_count_applicable"]
        is False
    )
    assert (
        observed_execution["operational_metrics"][
            "follow_up_request_count_evidence_kind"
        ]
        == "not_applicable"
    )
    assert observed_execution["operational_metrics"]["correction_count"] is None
    assert (
        observed_execution["operational_metrics"]["correction_count_evidence_kind"]
        == "not_applicable"
    )
    assert observed_execution["final_state_snapshot"]["submission_count"] == 2


def test_durable_submission_plan_certifies_same_instance_checkpoint_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.integrations.internal_mcp import catalogue

    base_contract = _contract()
    durable_scenario = replace(
        base_contract.scenarios[0],
        execution={
            "adapter_id": certification_script.DURABLE_WORKFLOW_ADAPTER_ID,
            "inputs": {
                "workflow_id": "#V#unit_checkpoint_pause_workflow",
                "workflow_inputs": {"probe": "represented"},
                "submission_plan": [
                    {
                        "operation": "execute",
                        "await_terminal": False,
                        "timeout_seconds": 10_000.0,
                    },
                    {
                        "operation": "await_status",
                        "expected_status": "paused",
                        "expected_state": "after_pause",
                        "timeout_seconds": 1.0,
                        "poll_interval_seconds": 0.0,
                    },
                    {"operation": "resume", "timeout_seconds": 0.0},
                    {
                        "operation": "execute",
                        "await_terminal": True,
                        "timeout_seconds": 5.0,
                    },
                ],
            },
        },
        reset_policy={"mode": "read_only"},
        metadata={"read_only": True},
    )
    contract = replace(base_contract, scenarios=(durable_scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    workflow_calls: list[dict[str, Any]] = []
    status_poll_calls: list[str] = []
    sleep_calls: list[float] = []
    observed_execution: dict[str, Any] = {}
    exact_worker_build = "a" * 40
    pause_receipt = {
        "schema_version": "workflow_checkpoint_pause_receipt.v1",
        "instance_id": "checkpoint-instance-1",
        "workflow_id": "#V#unit_checkpoint_pause_workflow",
        "status": "paused",
        "checkpoint_state": "after_pause",
        "checkpoint_step_index": 1,
        "pause_mode": "cooperative_checkpoint",
        "manual_resume_required": True,
        "claim_token_sha256": "b" * 64,
    }
    resume_receipt = {
        "schema_version": "workflow_checkpoint_resume_receipt.v1",
        "instance_id": "checkpoint-instance-1",
        "workflow_id": "#V#unit_checkpoint_pause_workflow",
        "status": "pending",
        "checkpoint_state": "after_pause",
        "checkpoint_step_index": 1,
        "pause_receipt_sha256": certification_script.stable_payload_digest(
            pause_receipt
        ),
        "same_instance_resume": True,
        "resume_count": 1,
    }

    def workflow_execute(**kwargs: Any) -> dict[str, Any]:
        workflow_calls.append(dict(kwargs))
        terminal = kwargs["await_terminal"] is True
        return {
            "success": True,
            "instance_id": "checkpoint-instance-1",
            "created_new": not terminal,
            "status": "reused" if terminal else "pending",
            "final_status": "completed" if terminal else None,
            "timed_out": False,
            "workflow_instance": {
                "instance_id": "checkpoint-instance-1",
                "namespace": "unit-namespace",
                "user_id": "#V#unit_user",
                "org_id": "#V#unit_org",
                "status": "completed" if terminal else "pending",
                "min_worker_build": exact_worker_build,
                "claimed_by_build": (
                    {"git_commit": exact_worker_build} if terminal else None
                ),
                "checkpoint_pause_receipt": pause_receipt if terminal else None,
                "checkpoint_resume_receipt": resume_receipt if terminal else None,
            },
            "workflow_execution": {
                "instance_id": "checkpoint-instance-1",
                "final_status": "completed" if terminal else None,
            },
        }

    def workflow_get_instance(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"instance_id": "checkpoint-instance-1"}
        status_poll_calls.append("poll")
        if len(status_poll_calls) % 2 == 1:
            return {
                "success": True,
                "instance_id": "checkpoint-instance-1",
                "workflow_id": "#V#unit_checkpoint_pause_workflow",
                "namespace": "unit-namespace",
                "user_id": "#V#unit_user",
                "org_id": "#V#unit_org",
                "status": "running",
                "current_state": "request_checkpoint_pause",
                "step_index": 0,
                "min_worker_build": exact_worker_build,
                "claimed_by_build": {"git_commit": exact_worker_build},
            }
        return {
            "success": True,
            "instance_id": "checkpoint-instance-1",
            "workflow_id": "#V#unit_checkpoint_pause_workflow",
            "namespace": "unit-namespace",
            "user_id": "#V#unit_user",
            "org_id": "#V#unit_org",
            "status": "paused",
            "current_state": "after_pause",
            "step_index": 1,
            "min_worker_build": exact_worker_build,
            "claimed_by_build": {"git_commit": exact_worker_build},
            "checkpoint_pause_receipt": pause_receipt,
        }

    def workflow_resume_instance(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {"instance_id": "checkpoint-instance-1"}
        return {
            "success": True,
            "instance_id": "checkpoint-instance-1",
            "status": "pending",
            "same_instance_resume": True,
            "checkpoint_resume_receipt": resume_receipt,
            "instance_status": {
                "instance_id": "checkpoint-instance-1",
                "status": "pending",
                "current_state": "after_pause",
                "checkpoint_pause_receipt": pause_receipt,
                "checkpoint_resume_receipt": resume_receipt,
            },
        }

    def inspect_execution(
        run_contract,
        *,
        reset_scenario,
        execute_scenario,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        scenario = run_contract.scenarios[0]
        reset = reset_scenario(scenario, 1)
        observed_execution.update(execute_scenario(scenario, 1, reset))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(catalogue, "_workflow_execute", workflow_execute)
    monkeypatch.setattr(catalogue, "_workflow_get_instance", workflow_get_instance)
    monkeypatch.setattr(
        catalogue,
        "_workflow_resume_instance",
        workflow_resume_instance,
    )
    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        inspect_execution,
    )
    monkeypatch.setattr(
        certification_script.time,
        "sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    certification_script._live_execution(_args(), contract)

    assert len(workflow_calls) == 2
    assert workflow_calls[0]["timeout_seconds"] == 600.0
    assert sleep_calls == [0.05]
    assert observed_execution["terminal_state"] == "completed"
    path = observed_execution["path_analysis"]
    assert path["submission_operations"] == [
        "execute",
        "await_status",
        "resume",
        "execute",
    ]
    assert path["workflow_instance_ids"] == [
        "checkpoint-instance-1",
        "checkpoint-instance-1",
        "checkpoint-instance-1",
        "checkpoint-instance-1",
    ]
    assert path["pause_resume_continuity_observed"] is True
    assert path["idempotent_instance_reuse_observed"] is True
    assert path["checkpoint_pause_receipts"]
    assert path["checkpoint_pause_receipts"][0]["claim_token_sha256"] == "b" * 64
    assert path["checkpoint_resume_receipts"]
    assert observed_execution["final_state_snapshot"][
        "pause_resume_continuity_observed"
    ] is True

    workflow_calls.clear()
    observed_execution.clear()
    resume_receipt["pause_receipt_sha256"] = "0" * 64

    certification_script._live_execution(_args(), contract)

    assert len(workflow_calls) == 2
    assert sleep_calls == [0.05, 0.05]
    assert observed_execution["path_analysis"][
        "pause_resume_continuity_observed"
    ] is False


def test_synchronous_scenario_binds_and_records_represented_agent_test_fault_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.integrations.internal_mcp.agent_test_fault_plan import (
        maybe_inject_agent_test_mcp_fault,
    )

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    base_contract = _contract()
    durable_scenario = replace(
        base_contract.scenarios[0],
        execution={
            "adapter_id": certification_script.SYNCHRONOUS_WORKFLOW_ADAPTER_ID,
            "inputs": {
                "workflow_id": "#V#unit_fault_recovery_workflow",
                "workflow_inputs": {"isolation_id": "{{isolation_id}}"},
                "timeout_seconds": 5.0,
            },
        },
        reset_policy={"mode": "read_only"},
        fault_injection={
            "fault_class": "timeout",
            "recoverable": True,
            "require_all_fault_rules_consumed": True,
            "agent_test_mcp_fault_plan": {
                "schema_version": "agent_test_mcp_fault_plan.v1",
                "plan_id": "fault-plan-{{trial_index}}-{{isolation_id}}",
                "faults": [
                    {
                        "fault_id": "first-resolution-timeout",
                        "tool_name": "resolve_concept_by_name",
                        "fault_class": "timeout",
                        "max_activations": 1,
                    }
                ],
            },
        },
        metadata={"read_only": True, "designated_recoverable_fault": True},
    )
    contract = replace(base_contract, scenarios=(durable_scenario,))
    _patch_live_preflight(monkeypatch, actor_contract=contract)
    observed_execution: dict[str, Any] = {}

    def workflow_execute(**kwargs: Any) -> dict[str, Any]:
        first = maybe_inject_agent_test_mcp_fault("resolve_concept_by_name")
        second = maybe_inject_agent_test_mcp_fault("resolve_concept_by_name")
        assert first is not None
        assert first.error_code == "tool_timeout"
        assert second is None
        assert kwargs["workflow_id"] == "#V#unit_fault_recovery_workflow"
        assert kwargs["require_policy_identity"] is False
        assert kwargs["timeout_seconds"] == 5.0
        return {
            "success": True,
            "final_state": "completed",
            "execution_trace_id": "fault-recovery-trace",
            "workflow_output_sha256": "a" * 64,
            "exact_scope": {
                "namespace": "unit-namespace",
                "user_id": "#V#unit_user",
                "org_id": "#V#unit_org",
            },
            "workflow_action_evidence": [
                {
                    "action_id": "workflow_mcp.invoke_tool",
                    "status": "failed",
                    "requested_tool_name": "resolve_concept_by_name",
                    "resolved_tool_name": "resolve_concept_by_name",
                },
                {
                    "action_id": "workflow_mcp.invoke_tool",
                    "status": "success",
                    "requested_tool_name": "resolve_concept_by_name",
                    "resolved_tool_name": "resolve_concept_by_name",
                },
            ],
            "workflow_output": {
                "represented_operational_fault_recovery_probe_result": {
                    "schema_version": (
                        "represented_operational_fault_recovery_probe_result.v1"
                    ),
                    "recovery_attempt_count": 1,
                    "final_resolution_status": "not_found",
                }
            },
        }

    def inspect_execution(
        run_contract,
        *,
        reset_scenario,
        execute_scenario,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        scenario = run_contract.scenarios[0]
        reset = reset_scenario(scenario, 1)
        observed_execution.update(execute_scenario(scenario, 1, reset))
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": False,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "_execute_represented_workflow_synchronously",
        workflow_execute,
    )
    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        inspect_execution,
    )

    certification_script._live_execution(_args(), contract)

    events = observed_execution["agent_test_fault_events"]
    assert len(events) == 1
    assert events[0]["fault_id"] == "first-resolution-timeout"
    assert events[0]["fault_class"] == "timeout"
    assert events[0]["activation_index"] == 1
    assert observed_execution["operational_metrics"]["timeout"] is False
    assert observed_execution["operational_metrics"]["injected_fault_count"] == 1
    assert observed_execution["operational_metrics"]["injected_timeout_count"] == 1
    assert observed_execution["path_analysis"]["execution_transport"] == (
        "synchronous_workflow_executor"
    )
    assert observed_execution["path_analysis"]["observed_tool_names"] == [
        "resolve_concept_by_name"
    ]
    assert observed_execution["final_state_snapshot"][
        "agent_test_fault_event_sha256"
    ] == certification_script.stable_payload_digest(events)


def test_fault_plan_consumption_requires_every_activation_in_declared_order() -> None:
    plan = {
        "faults": [
            {"fault_id": "first", "max_activations": 2},
            {"fault_id": "second", "max_activations": 1},
        ]
    }
    exact_events = [
        {"fault_id": "first"},
        {"fault_id": "first"},
        {"fault_id": "second"},
    ]

    certification_script._require_exact_agent_test_fault_consumption(
        fault_plan=plan,
        fault_events=exact_events,
    )

    with pytest.raises(RuntimeError, match="not_fully_consumed"):
        certification_script._require_exact_agent_test_fault_consumption(
            fault_plan=plan,
            fault_events=[{"fault_id": "first"}, {"fault_id": "second"}],
        )
    with pytest.raises(RuntimeError, match="not_fully_consumed"):
        certification_script._require_exact_agent_test_fault_consumption(
            fault_plan=plan,
            fault_events=[
                {"fault_id": "first"},
                {"fault_id": "second"},
                {"fault_id": "first"},
            ],
        )


@pytest.mark.parametrize(
    ("final_state", "expected"),
    [
        ("completed", False),
        ("verified", False),
        ("failed", True),
        ("subworkflow_failed", True),
        ("error", True),
        ("cancelled", True),
    ],
)
def test_synchronous_workflow_terminal_failure_detection(
    final_state: str,
    expected: bool,
) -> None:
    assert certification_script._workflow_final_state_is_failure(final_state) is (
        expected
    )


def test_evaluator_transport_uses_stable_correlation_and_failed_campaign_is_not_releasable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    evaluator_calls: list[dict[str, Any]] = []

    def execute_evaluator_workflow(**kwargs: Any) -> dict[str, Any]:
        evaluator_calls.append(dict(kwargs))
        return {
            "success": True,
            "trace_persisted": True,
            "workflow_authority_source": "vontology",
            "workflow_definition_identity_sha256": "definition-digest",
            "trace_document_sha256": "trace-document-digest",
            "evaluator_policy_identity": {
                "schema_version": "represented_evaluator_policy_identity.v1",
                "identity_sha256": "policy-digest",
            },
            "transport": "synchronous_workflow_executor",
            "execution_trace_id": "trace-evaluator",
            "workflow_output_sha256": "output-evaluator",
            "workflow_output": {
                "represented_operational_evaluator_result": {
                    "schema_version": "represented_operational_evaluator_result.v1",
                    "evaluator_id": "#V#strict_operational_evaluator",
                    "scenario_id": "represented_lookup",
                    "trial_index": 1,
                    "verdict": "blocked",
                    "terminal_state": "inconclusive",
                    "evidence": [{"kind": "typed_blocker", "ref": "blocker-1"}],
                }
            },
        }

    monkeypatch.setattr(
        certification_script,
        "_execute_represented_workflow_synchronously",
        execute_evaluator_workflow,
    )

    def replay_evaluator(
        contract,
        *,
        execute_represented_evaluator,
        **_kwargs,
    ):
        scenario = contract.scenarios[0]
        evaluator_spec = scenario.evaluator_specs[0]
        observation = {
            "scenario_id": scenario.scenario_id,
            "trial_index": 1,
            "turn_execution_request_ids": ["request-1"],
        }
        first = execute_represented_evaluator(
            scenario,
            evaluator_spec,
            1,
            observation,
        )
        second = execute_represented_evaluator(
            scenario,
            evaluator_spec,
            1,
            observation,
        )
        assert first == second
        return {
            "campaign_result": {
                "certified": False,
                "experiment_persistence_complete": True,
            }
        }

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        replay_evaluator,
    )

    execution = certification_script._live_execution(_args(), _contract())

    assert len(evaluator_calls) == 2
    first, second = evaluator_calls
    assert first["event_idempotency_key"] == second["event_idempotency_key"]
    assert first["source_event_id"] == second["source_event_id"]
    assert first["source_event_type"] == "operational_certification_evaluation"
    assert first["workflow_id"] == "#V#strict_operational_evaluator"
    assert first["namespace"] == "unit-namespace"
    assert first["user_id"] == "#V#unit_user"
    assert first["org_id"] == "#V#unit_org"
    assert first["execution_metadata"]["evaluator_id"] == (
        "#V#strict_operational_evaluator"
    )
    assert first["execution_metadata"]["observation_sha256"]
    assert execution["campaign_result"]["certified"] is False
    assert execution["campaign_result"]["experiment_persistence_complete"] is True
    assert execution["release_eligibility"]["eligible"] is False


def test_redacted_evidence_can_be_written_to_a_safe_default_artifact(
    tmp_path: Path,
) -> None:
    safe_evidence = certification_script._bounded_safe_evidence(
        {
            "Authorization": "Bearer do-not-write-this",
            "message_body": "private message contents",
            "nested": {
                "api_token": "also-secret",
                "claim_token": "raw-worker-claim",
                "claim_token_sha256": "a" * 64,
            },
            "malformed_digest": {
                "claim_token_sha256": "not-a-digest-secret"
            },
        }
    )
    encoded = json.dumps(safe_evidence)

    assert "do-not-write-this" not in encoded
    assert "private message contents" not in encoded
    assert "also-secret" not in encoded
    assert "raw-worker-claim" not in encoded
    assert "not-a-digest-secret" not in encoded
    assert safe_evidence["nested"]["claim_token_sha256"] == "a" * 64
    assert safe_evidence["malformed_digest"]["claim_token_sha256"] == {
        "redacted": True,
        "value_present": True,
    }
    assert safe_evidence["Authorization"] == {
        "redacted": True,
        "value_present": True,
    }
    assert safe_evidence["message_body"]["length"] == len("private message contents")

    stem = certification_script._safe_artifact_stem("../../#V:run/unsafe")
    assert stem == "V-run-unsafe"
    assert "/" not in stem
    target = tmp_path / "reports" / f"{stem}.json"
    certification_script._write_json(str(target), {"evidence": safe_evidence})

    assert target.is_file()
    written = target.read_text(encoding="utf-8")
    assert "do-not-write-this" not in written
    assert json.loads(written)["evidence"] == safe_evidence


def test_task_evidence_projection_excludes_embedded_result_and_debug_payload() -> None:
    huge_debug_payload = {"raw_response": "private-debug" * 50_000}
    task_evidence = {
        "last_task_status": {
            "status": "completed",
            "request_id": "request-1",
            "result": {"llm_debug_data": huge_debug_payload},
        },
        "task_statuses": [{"status": "running"}, {"status": "completed"}],
        "progress_snapshots": [{"stage": "selector"}],
        "timed_out": False,
    }

    projected = certification_script._task_evidence_projection(task_evidence)

    assert projected["last_task_status"] == {
        "status": "completed",
        "request_id": "request-1",
    }
    assert projected["last_task_status_sha256"]
    assert projected["task_status_count"] == 2
    assert projected["progress_snapshot_count"] == 1
    assert len(json.dumps(projected)) < 1_000
    assert "private-debug" not in json.dumps(projected)


def test_namespace_audit_ignores_bounded_trace_sentinel_but_detects_foreign_scope() -> (
    None
):
    expected = "#V#unit_user@unit_org"
    violations, audit = certification_script._namespace_audit(
        {
            "namespace": expected,
            "nested": [
                {"namespace": "[truncated: max depth reached]"},
                {"namespace": "#V#foreign_user@foreign_org"},
            ],
        },
        effective_namespace=expected,
    )

    assert violations == ["#V#foreign_user@foreign_org"]
    assert audit["truncation_sentinel_count"] == 1
    assert audit["noncanonical_value_count"] == 1
    assert audit["violation_count"] == 1


def test_runtime_alignment_requires_exact_clean_code_and_mongo_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "version_details": {
                    "git_commit": "a" * 40,
                    "git_dirty": False,
                },
                "mongo": {
                    "effective_mongo_location": {
                        "classification": "local",
                        "sanitized_uri": "mongodb://127.0.0.1:27017/von",
                        "using_fallback": False,
                    },
                    "effective_database_name_sha256": (
                        certification_script.hashlib.sha256(b"von").hexdigest()
                    ),
                },
            }

    class Session:
        def get(self, _url: str, *, timeout: float) -> Response:
            assert timeout == 45.0
            return Response()

    from src.backend.db import mongo_client, mongo_uri_redaction

    monkeypatch.setattr(
        mongo_client,
        "get_effective_mongo_uri",
        lambda: "mongodb://127.0.0.1:27017/von",
    )
    monkeypatch.setattr(mongo_client, "is_using_fallback_uri", lambda: False)
    monkeypatch.setattr(mongo_client, "get_configured_database_name", lambda: "von")
    monkeypatch.setattr(
        mongo_uri_redaction,
        "build_safe_mongo_connection_location",
        lambda *_args, **_kwargs: {
            "classification": "local",
            "sanitized_uri": "mongodb://127.0.0.1:27017/von",
            "using_fallback": False,
        },
    )

    aligned = certification_script._collect_runtime_authority_alignment(
        session=Session(),
        base_url="http://127.0.0.1:5010",
        environment={
            "server_agent_test_instance": True,
            "local_repo_git_head": "a" * 40,
            "local_repo_git_dirty": False,
        },
    )

    assert aligned["verified"] is True
    assert all(aligned["checks"].values())

    dirty_response = Response()
    dirty_response.json = lambda: {
        **Response().json(),
        "version_details": {"git_commit": "a" * 40, "git_dirty": True},
    }
    dirty_session = Session()
    dirty_session.get = lambda *_args, **_kwargs: dirty_response
    misaligned = certification_script._collect_runtime_authority_alignment(
        session=dirty_session,
        base_url="http://127.0.0.1:5010",
        environment={
            "server_agent_test_instance": True,
            "local_repo_git_head": "a" * 40,
            "local_repo_git_dirty": False,
        },
    )
    assert misaligned["verified"] is False
    assert misaligned["checks"]["clean_server_build"] is False


def test_runtime_alignment_honours_explicit_non_agent_test_server_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "version_details": {"git_commit": "a" * 40, "git_dirty": False},
                "mongo": {
                    "effective_mongo_location": {"classification": "local"},
                    "effective_database_name_sha256": (
                        certification_script.hashlib.sha256(b"von").hexdigest()
                    ),
                },
            }

    class Session:
        def get(self, _url: str, *, timeout: float) -> Response:
            assert timeout == 45.0
            return Response()

    from src.backend.db import mongo_client, mongo_uri_redaction

    monkeypatch.setattr(
        mongo_client,
        "get_effective_mongo_uri",
        lambda: "mongodb://127.0.0.1:27017/von",
    )
    monkeypatch.setattr(mongo_client, "is_using_fallback_uri", lambda: False)
    monkeypatch.setattr(mongo_client, "get_configured_database_name", lambda: "von")
    monkeypatch.setattr(
        mongo_uri_redaction,
        "build_safe_mongo_connection_location",
        lambda *_args, **_kwargs: {"classification": "local"},
    )

    aligned = certification_script._collect_runtime_authority_alignment(
        session=Session(),
        base_url="http://127.0.0.1:5000",
        environment={
            "server_agent_test_instance": False,
            "local_repo_git_head": "a" * 40,
            "local_repo_git_dirty": False,
        },
        allow_non_agent_test_server=True,
    )

    assert aligned["verified"] is True
    assert aligned["checks"]["approved_server_mode"] is True


@pytest.mark.parametrize(("stdout", "expected"), [("", False), ("?? x.py\n", True)])
def test_replay_environment_git_status_distinguishes_clean_from_failure(
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: bool,
) -> None:
    class Completed:
        returncode = 0

        def __init__(self, output: str) -> None:
            self.stdout = output

    monkeypatch.setattr(
        replay_script.subprocess,
        "run",
        lambda *_args, **_kwargs: Completed(stdout),
    )

    assert replay_script._git_status_dirty() is expected


def test_live_execution_fails_before_auth_when_runtime_alignment_is_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    monkeypatch.setattr(
        certification_script,
        "_collect_runtime_authority_alignment",
        lambda **_kwargs: {
            "schema_version": "operational_certification_runtime_alignment.v1",
            "verified": False,
            "checks": {"exact_mongo_authority_location": False},
            "alignment_sha256": "misaligned",
        },
    )
    monkeypatch.setattr(
        certification_script,
        "get_auth_status",
        lambda **_kwargs: pytest.fail("auth must not run before alignment passes"),
    )

    execution = certification_script._live_execution(_args(), _contract())

    assert execution["release_eligibility"]["eligible"] is False
    assert execution["release_eligibility"]["reason_code"] == (
        "certification_runtime_authority_alignment_unverified"
    )
    assert execution["trial_results"] == []


def test_reused_experiment_run_is_rejected_until_atomic_claim_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    campaign_called = False

    def _unexpected_campaign(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal campaign_called
        campaign_called = True
        return {}

    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        _unexpected_campaign,
    )

    execution = certification_script._live_execution(
        _args(experiment_run_id="#V#already_terminal"),
        _contract(),
    )

    assert campaign_called is False
    assert execution["release_eligibility"]["reason_code"] == (
        "experiment_run_reuse_not_supported"
    )


def test_release_requires_terminal_pass_experiment_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_live_preflight(monkeypatch)
    monkeypatch.setattr(
        certification_script,
        "run_operational_certification_campaign",
        lambda *_args, **_kwargs: {
            "campaign_result": {
                "certified": True,
                "experiment_persistence_complete": True,
            }
        },
    )
    monkeypatch.setattr(
        certification_script,
        "compute_experiment_verdict",
        lambda **_kwargs: {"success": False, "error": "write_failed"},
    )

    execution = certification_script._live_execution(_args(), _contract())

    assert execution["campaign_result"]["certified"] is True
    assert execution["experiment_finalisation"]["success"] is False
    assert execution["release_eligibility"]["eligible"] is False


def test_artifact_projection_redacts_private_trial_and_trace_content() -> None:
    private_text = "pilot-private-answer-never-write-verbatim"
    artifact = certification_script._artifact_safe_execution_projection(
        {
            "execution_sha256": "source-digest",
            "trial_observations": [
                {
                    "visible_answer": private_text,
                    "final_response": {"text": private_text},
                    "rationale": private_text,
                    "tool_evidence_projection": {
                        "entries": [
                            {
                                "projected_payload": {
                                    "semantic_fact": private_text,
                                }
                            }
                        ]
                    },
                }
            ],
            "workflow_trace": {
                "messages": [{"content": private_text}],
                "prompt": private_text,
                "actions": [
                    {
                        "action_id": "safe.action",
                        "inputs": {
                            "query": private_text,
                            "variables": {"arbitrary": private_text},
                        },
                        "outputs": {
                            "content": private_text,
                            "preview": private_text,
                            "text": private_text,
                        },
                    }
                ],
            },
        }
    )

    encoded = json.dumps(artifact)
    assert private_text not in encoded
    assert artifact["artifact_projection"]["private_content_redacted"] is True
