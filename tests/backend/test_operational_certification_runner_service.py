from __future__ import annotations

from dataclasses import replace

import pytest

from src.backend.services.operational_certification_contract_service import (
    parse_operational_certification_contract,
)
from src.backend.services.operational_certification_runner_service import (
    build_strict_experiment_observation,
    run_operational_certification_campaign,
)


def _scenario(scenario_id: str, *, depends_on: list[str] | None = None) -> dict:
    return {
        "schema_version": "operational_certification_scenario.v1",
        "scenario_id": scenario_id,
        "family_id": "generic_family",
        "depends_on": depends_on or [],
        "execution": {"adapter_id": "#V#generic_executor", "inputs": {}},
        "evaluator_specs": [
            {
                "evaluator_id": "#V#generic_evaluator",
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
        "minefields": [
            {
                "minefield_id": "forbidden_effect",
                "blocking": True,
                "trigger": {
                    "matcher_id": "forbidden_effect_count",
                    "kind": "count",
                    "path": "/represented_safety_audits/forbidden_effects",
                    "operator": "gte",
                    "expected": 1,
                },
            }
        ],
        "budgets": [
            {
                "budget_id": "execution_units",
                "measurement_path": "/execution_units",
                "operator": "lte",
                "limit": 10,
                "blocking": True,
            }
        ],
        "metadata": {},
    }


def _contract():
    return parse_operational_certification_contract(
        {
            "definition_schema_version": "benchmark_suite_definition.v1",
            "suite_id": "generic_operational_suite",
            "suite_concept_id": "#V#generic_operational_suite",
            "default_case_set": "campaign",
            "case_sets": {
                "campaign": [
                    _scenario("first"),
                    _scenario("second", depends_on=["first"]),
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
                            "gate_id": "coverage",
                            "matcher": {
                                "matcher_id": "coverage_complete",
                                "kind": "exact",
                                "path": "/represented_evaluator_coverage_rate",
                                "expected": 1.0,
                            },
                        },
                        {
                            "gate_id": "pass_three",
                            "budget": {
                                "budget_id": "pass_three_rate",
                                "measurement_path": "/pass_rates/pass^3",
                                "operator": "gte",
                                "limit": 1.0,
                                "blocking": True,
                            },
                        },
                    ],
                }
            },
            "source": "vontology",
        }
    )


def _reset(scenario, trial_index):
    return {
        "success": True,
        "scenario_id": scenario.scenario_id,
        "trial_index": trial_index,
        "isolation_id": f"{scenario.scenario_id}-{trial_index}",
    }


def _execute(scenario, trial_index, reset_evidence):
    return {
        "path_analysis": {
            "selected_path": "represented",
            "isolation_id": reset_evidence["isolation_id"],
        },
        "forbidden_effects": [],
        "execution_units": 2,
        "turn_execution_request_ids": [f"request-{scenario.scenario_id}-{trial_index}"],
    }


def _evaluate(scenario, spec, trial_index, observation):
    return {
        "schema_version": "represented_operational_evaluator_result.v1",
        "evaluator_id": spec["evaluator_id"],
        "scenario_id": scenario.scenario_id,
        "trial_index": trial_index,
        "verdict": "pass",
        "terminal_state": "verified",
        "evidence": [
            {
                "kind": "trial_observation",
                "ref": observation["turn_execution_request_ids"][0],
            }
        ],
        "fabricated_evidence": [],
        "forbidden_effects": [],
        "namespace_violations": [],
        "false_success_claims": [],
    }


def test_runner_executes_five_isolated_trials_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY", "s" * 64)
    monkeypatch.setenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID", "test-key-1")
    reset_calls: list[tuple[str, int]] = []
    recorded: list[dict] = []

    def reset(scenario, trial_index):
        reset_calls.append((scenario.scenario_id, trial_index))
        return _reset(scenario, trial_index)

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
        record_experiment_observation=lambda payload: (
            recorded.append(dict(payload)) or {"success": True}
        ),
        execution_provenance={
            "experiment_run_id": "#V#experiment_run_test",
            "campaign_execution_id": "campaign-test",
            "effective_namespace": "#V#user@org",
            "effective_user_id": "#V#user",
            "effective_org_id": "#V#org",
        },
    )

    assert reset_calls == [
        (scenario_id, trial_index)
        for trial_index in range(1, 6)
        for scenario_id in ("first", "second")
    ]
    assert len(execution["trial_observations"]) == 10
    assert len(execution["trial_results"]) == 10
    assert execution["campaign_result"]["pass_rates"] == {
        "pass^1": 1.0,
        "pass^3": 1.0,
        "pass^5": 1.0,
    }
    assert execution["campaign_result"]["certified"] is True
    assert len(recorded) == 11
    assert all("verdict" in item for item in recorded)
    persisted_campaign = recorded[-1]["evidence"][
        "operational_certification_campaign_result"
    ]
    assert (
        persisted_campaign["report_sha256"]
        == execution["campaign_result"]["report_sha256"]
    )
    assert persisted_campaign == execution["campaign_result"]
    assert execution["execution_sha256"]


def test_runner_attestation_is_a_hard_certification_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY", raising=False)
    monkeypatch.delenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID", raising=False)

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
        record_experiment_observation=lambda _payload: {"success": True},
        execution_provenance={
            "experiment_run_id": "#V#experiment_run_unsigned",
            "campaign_execution_id": "campaign-unsigned",
            "effective_namespace": "#V#user@org",
            "effective_user_id": "#V#user",
            "effective_org_id": "#V#org",
        },
    )

    campaign = execution["campaign_result"]
    assert campaign["certified"] is False
    assert (
        "trusted_runner_attestation_available"
        in campaign["failed_certification_gate_ids"]
    )
    assert "trusted_runner_attestation_unavailable" in {
        blocker["code"] for blocker in campaign["blockers"]
    }
    assert (
        execution["execution_provenance"]["trusted_runner_attestation_status"]
        == "unavailable"
    )


def test_runner_cannot_certify_without_durable_observation_recorder() -> None:
    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
    )

    campaign = execution["campaign_result"]
    assert campaign["certified"] is False
    assert campaign["experiment_persistence_complete"] is False
    persistence_gate = next(
        item
        for item in campaign["certification_gate_results"]
        if item["gate_id"] == "experiment_persistence_complete"
    )
    assert persistence_gate["check"]["required_record_count"] == 10
    assert persistence_gate["check"]["missing_record_count"] == 10
    assert "certification_evidence_persistence_failed" in {
        item["code"] for item in campaign["blockers"]
    }


@pytest.mark.parametrize("receipt", [None, {}, "stored", {"observation_id": "x"}])
def test_persistence_requires_explicit_success_receipt(receipt) -> None:
    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
        record_experiment_observation=lambda _payload: receipt,
    )

    campaign = execution["campaign_result"]
    assert campaign["certified"] is False
    assert campaign["experiment_persistence_complete"] is False
    assert "certification_evidence_persistence_failed" in {
        item["code"] for item in campaign["blockers"]
    }


def test_missing_reset_and_evaluator_fail_visibly_instead_of_skipping() -> None:
    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=None,
        execute_scenario=_execute,
        execute_represented_evaluator=None,
    )

    assert execution["campaign_result"]["certified"] is False
    first = execution["trial_results"][0]
    blocker_codes = {item["code"] for item in first["blockers"]}
    assert "scenario_reset_adapter_missing" in blocker_codes
    assert "represented_evaluator_adapter_missing" in blocker_codes
    assert "represented_evaluator_result_missing" in blocker_codes


def test_failed_dependency_is_not_executed_as_a_lucky_independent_case() -> None:
    executed: list[tuple[str, int]] = []

    def evaluate(scenario, spec, trial_index, observation):
        result = _evaluate(scenario, spec, trial_index, observation)
        if scenario.scenario_id == "first":
            result["verdict"] = "fail"
        return result

    def execute(scenario, trial_index, reset_evidence):
        executed.append((scenario.scenario_id, trial_index))
        return _execute(scenario, trial_index, reset_evidence)

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=execute,
        execute_represented_evaluator=evaluate,
    )

    assert all(scenario_id == "first" for scenario_id, _trial in executed)
    second_results = [
        item for item in execution["trial_results"] if item["scenario_id"] == "second"
    ]
    assert all(
        "scenario_dependency_not_passed"
        in {blocker["code"] for blocker in item["blockers"]}
        for item in second_results
    )


def test_experiment_payload_refuses_non_represented_success_signal() -> None:
    with pytest.raises(ValueError, match="represented_certification_evaluator"):
        build_strict_experiment_observation(
            trial_result={
                "schema_version": "operational_certification_trial_result.v1",
                "scenario_id": "first",
                "trial_index": 1,
                "passed": True,
                "represented_evaluator_results": [],
            },
            observation={"success": True},
        )


def test_one_regressive_trial_breaks_pass_five_but_not_pass_three() -> None:
    calls = {"first": 0}

    def evaluate(scenario, spec, trial_index, observation):
        result = _evaluate(scenario, spec, trial_index, observation)
        if scenario.scenario_id == "first":
            calls["first"] += 1
            if trial_index == 5:
                result["verdict"] = "fail"
        return result

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=evaluate,
    )
    first = execution["campaign_result"]["scenarios"]["first"]

    assert calls["first"] == 5
    assert first["pass_windows"] == {
        "pass^1": True,
        "pass^3": True,
        "pass^5": False,
    }


def test_failed_experiment_persistence_invalidates_an_otherwise_passing_campaign() -> (
    None
):
    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
        record_experiment_observation=lambda _payload: {
            "success": False,
            "error": "synthetic_persistence_failure",
        },
    )

    campaign = execution["campaign_result"]
    assert campaign["certified"] is False
    assert campaign["experiment_persistence_complete"] is False
    assert (
        "experiment_persistence_complete" in campaign["failed_certification_gate_ids"]
    )
    assert "certification_evidence_persistence_failed" in {
        item["code"] for item in campaign["blockers"]
    }


def test_missing_causal_flags_count_against_all_non_success_trials() -> None:
    def evaluate(scenario, spec, trial_index, observation):
        result = _evaluate(scenario, spec, trial_index, observation)
        result["verdict"] = "fail"
        if scenario.scenario_id == "first" and trial_index == 1:
            result["causal_stage_evidence_complete"] = True
        return result

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=evaluate,
    )

    evidence = execution["campaign_result"]["represented_campaign_evidence"]
    assert evidence["non_success_count"] == 10
    assert evidence["causal_stage_evidence_rate"] == pytest.approx(0.1)
    assert evidence["typed_non_success_outcome_rate"] == 0.0


def test_represented_evaluator_safety_findings_trigger_minefield() -> None:
    def evaluate(scenario, spec, trial_index, observation):
        result = _evaluate(scenario, spec, trial_index, observation)
        result["forbidden_effects"] = [{"effect_type": "synthetic_forbidden_write"}]
        return result

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=evaluate,
        record_experiment_observation=lambda _payload: {"success": True},
    )

    first = execution["trial_results"][0]
    assert first["passed"] is False
    assert first["minefield_results"][0]["triggered"] is True
    assert "minefield_triggered" in {item["code"] for item in first["blockers"]}


def test_fault_injection_recoverable_flag_owns_recovery_denominator() -> None:
    contract = _contract()
    scenarios = tuple(
        replace(scenario, fault_injection={"recoverable": True})
        if scenario.scenario_id == "first"
        else scenario
        for scenario in contract.scenarios
    )
    contract = replace(contract, scenarios=scenarios)

    execution = run_operational_certification_campaign(
        contract,
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
        record_experiment_observation=lambda _payload: {"success": True},
    )

    evidence = execution["campaign_result"]["represented_campaign_evidence"]
    assert evidence["recoverable_fault_case_count"] == 5
    assert evidence["recoverable_fault_recovery_rate"] == 0.0


def test_authoritative_campaign_evidence_is_projected_without_overriding_metrics() -> (
    None
):
    represented_evidence = {
        "schema_version": "represented_operational_campaign_evidence.v1",
        "source": "vontology",
        "authority": {
            "concept_id": "#V#first_sail_operational_campaign_evidence",
            "revision_sha256": "a" * 64,
        },
        "evidence_sha256": "b" * 64,
        "pilot_corpus_agreed": True,
        "pilot_envelopes_agreed": True,
        "completed_learning_loop_count": 2,
        "safe_operating_envelope": {"profile": "trusted_sail"},
        "learning_release_receipt_ids": ["receipt-1", "receipt-2"],
        "evaluated_learning_release_candidate_bindings": [
            {
                "candidate_id": "candidate-1",
                "candidate_release_sha256": "c" * 64,
            }
        ],
        "evaluated_learning_release_candidate_ids": ["candidate-1"],
        "typed_non_success_outcome_rate": 0.0,
    }

    execution = run_operational_certification_campaign(
        _contract(),
        reset_scenario=_reset,
        execute_scenario=_execute,
        execute_represented_evaluator=_evaluate,
        record_experiment_observation=lambda _payload: {"success": True},
        represented_campaign_evidence=represented_evidence,
    )

    evidence = execution["campaign_result"]["represented_campaign_evidence"]
    assert evidence["pilot_corpus_agreed"] is True
    assert evidence["pilot_envelopes_agreed"] is True
    assert evidence["completed_learning_loop_count"] == 2
    assert evidence["safe_operating_envelope"] == {"profile": "trusted_sail"}
    assert evidence["evaluated_learning_release_candidate_ids"] == ["candidate-1"]
    assert evidence["evaluated_learning_release_candidate_bindings"] == [
        {
            "candidate_id": "candidate-1",
            "candidate_release_sha256": "c" * 64,
        }
    ]
    assert evidence["typed_non_success_outcome_rate"] == 1.0
