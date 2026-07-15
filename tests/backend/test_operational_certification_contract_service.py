from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.backend.services.benchmark_suite_vontology_service import (
    load_benchmark_suite_case_set,
    load_benchmark_suite_definition_from_seed_fixture,
)
from src.backend.services.operational_certification_contract_service import (
    CertificationContractValidationError,
    REPRESENTED_OPERATIONAL_EVALUATOR_RESULT_SCHEMA_VERSION,
    aggregate_five_trial_campaign,
    evaluate_budget,
    evaluate_matcher,
    evaluate_scenario_trial,
    json_serialisable_projection,
    parse_operational_certification_contract,
    project_represented_evaluator_observation,
    stable_payload_digest,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = (
    _REPO_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "operational_certification_benchmark_seed_bundle.json"
)


def _scenario(
    scenario_id: str = "scenario_a",
    *,
    family_id: str = "family_a",
    depends_on: list[str] | None = None,
    minefield_blocking: bool = True,
) -> dict:
    return {
        "schema_version": "operational_certification_scenario.v1",
        "scenario_id": scenario_id,
        "family_id": family_id,
        "depends_on": list(depends_on or []),
        "execution": {
            "adapter_id": "#V#represented_test_adapter",
            "inputs": {"specimen_id": f"#{scenario_id}"},
        },
        "evaluator_specs": [
            {
                "evaluator_id": "#V#state_evaluator",
                "result_schema_version": (
                    REPRESENTED_OPERATIONAL_EVALUATOR_RESULT_SCHEMA_VERSION
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
                "minefield_id": "forbidden_write",
                "blocking": minefield_blocking,
                "trigger": {
                    "matcher_id": "forbidden_write_seen",
                    "kind": "count",
                    "path": "/forbidden_writes",
                    "operator": "gte",
                    "expected": 1,
                },
            }
        ],
        "budgets": [
            {
                "budget_id": "latency_budget",
                "measurement_path": "/latency_ms",
                "operator": "lte",
                "limit": 100,
                "blocking": True,
            }
        ],
        "metadata": {"author": "represented_fixture"},
    }


def _suite(
    *,
    scenarios: list[dict] | None = None,
    minimum_pass_three_rate: float = 0.5,
) -> dict:
    return {
        "definition_schema_version": "benchmark_suite_definition.v1",
        "suite_id": "unit_operational_certification",
        "suite_concept_id": "#V#unit_operational_certification_suite",
        "source": "vontology",
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
                            "matcher_id": "coverage_is_complete",
                            "kind": "exact",
                            "path": "/represented_evaluator_coverage_rate",
                            "expected": 1.0,
                        },
                    },
                    {
                        "gate_id": "minimum_pass_three_rate",
                        "budget": {
                            "budget_id": "pass_three_floor",
                            "measurement_path": "/pass_rates/pass^3",
                            "operator": "gte",
                            "limit": minimum_pass_three_rate,
                            "blocking": True,
                        },
                    },
                    {
                        "gate_id": "no_blocking_minefields",
                        "matcher": {
                            "matcher_id": "blocking_minefields_zero",
                            "kind": "exact",
                            "path": "/blocking_minefield_trigger_count",
                            "expected": 0,
                        },
                    },
                ],
            }
        },
        "default_case_set": "certification",
        "case_sets": {
            "certification": scenarios or [_scenario()],
        },
    }


def _observation(
    scenario_id: str,
    trial_index: int,
    *,
    verdict: str = "pass",
    schema_version: str = REPRESENTED_OPERATIONAL_EVALUATOR_RESULT_SCHEMA_VERSION,
    evidence: list[dict] | None = None,
    forbidden_writes: list[dict] | None = None,
    latency_ms: int = 10,
) -> dict:
    return {
        "scenario_id": scenario_id,
        "trial_index": trial_index,
        "path_analysis": {
            "candidate_generation": "represented",
            "dispatch": "represented_adapter",
        },
        "forbidden_writes": list(forbidden_writes or []),
        "latency_ms": latency_ms,
        "represented_evaluator_results": {
            "#V#state_evaluator": {
                "schema_version": schema_version,
                "evaluator_id": "#V#state_evaluator",
                "scenario_id": scenario_id,
                "trial_index": trial_index,
                "verdict": verdict,
                "terminal_state": "verified",
                "evidence": (
                    evidence
                    if evidence is not None
                    else [
                        {"kind": "state_readback", "sha256": f"evidence-{trial_index}"}
                    ]
                ),
            }
        },
    }


_MISSING_BUDGET_MEASUREMENT = object()


def _trial_results(
    suite: dict,
    outcomes_by_scenario: dict[str, list[bool]],
) -> tuple[object, dict[str, list[dict]]]:
    contract = parse_operational_certification_contract(suite)
    results: dict[str, list[dict]] = {}
    for scenario_id, outcomes in outcomes_by_scenario.items():
        results[scenario_id] = [
            evaluate_scenario_trial(
                contract,
                scenario_id=scenario_id,
                observation=_observation(
                    scenario_id,
                    index,
                    verdict="pass" if passed else "fail",
                ),
            )
            for index, passed in enumerate(outcomes, start=1)
        ]
    return contract, results


def _blocker_codes(error: CertificationContractValidationError) -> set[str]:
    return {blocker.code for blocker in error.blockers}


def test_parse_contract_projects_stable_digests_and_topological_order() -> None:
    suite = _suite(
        scenarios=[
            _scenario("dependent", family_id="family_b", depends_on=["root"]),
            _scenario("root", family_id="family_a"),
        ]
    )

    contract = parse_operational_certification_contract(suite)

    assert contract.topological_scenario_ids == ("root", "dependent")
    assert len(contract.contract_sha256) == 64
    assert len(contract.source_definition_sha256) == 64
    projection = contract.to_projection()
    assert projection["schema_version"] == (
        "operational_certification_contract_projection.v1"
    )
    assert projection["policy"]["trial_count"] == 5
    assert json.loads(json.dumps(projection)) == projection

    reordered_suite = json.loads(json.dumps(suite, sort_keys=True))
    reordered_contract = parse_operational_certification_contract(reordered_suite)
    assert reordered_contract.contract_sha256 == contract.contract_sha256
    assert reordered_contract.source_definition_sha256 == (
        contract.source_definition_sha256
    )


def test_represented_evaluator_observation_projection_bounds_duplicate_evidence() -> (
    None
):
    scenario = _scenario()
    scenario["evaluator_specs"][0]["observation_projection"] = {
        "schema_version": "represented_evaluator_observation_projection.v1",
        "include_paths": [
            "/terminal_state",
            "/path_analysis/validated_result",
            "/namespace_violations",
        ],
        "required_paths": [
            "/terminal_state",
            "/path_analysis/validated_result",
        ],
        "max_serialised_chars": 10_000,
    }
    contract = parse_operational_certification_contract(_suite(scenarios=[scenario]))
    evaluator_spec = contract.scenarios[0].evaluator_specs[0]
    observation = {
        "terminal_state": "completed",
        "path_analysis": {
            "validated_result": {
                "schema_version": "represented_example_result.v1",
                "verdict": "pass",
            },
            "duplicated_trace": "x" * 500_000,
        },
        "namespace_violations": [],
        "workflow_submissions": [{"duplicated_trace": "x" * 500_000}],
    }

    projected, diagnostics = project_represented_evaluator_observation(
        observation,
        evaluator_spec,
    )

    assert projected["terminal_state"] == "completed"
    assert projected["path_analysis"]["validated_result"]["verdict"] == "pass"
    assert "duplicated_trace" not in projected["path_analysis"]
    assert "workflow_submissions" not in projected
    assert diagnostics["configured"] is True
    assert diagnostics["source_serialised_chars"] > 1_000_000
    assert diagnostics["projected_serialised_chars"] < 10_000
    assert diagnostics["source_observation_sha256"] == stable_payload_digest(
        observation
    )


def test_represented_evaluator_observation_projection_fails_closed_on_missing_evidence() -> (
    None
):
    evaluator_spec = {
        "observation_projection": {
            "schema_version": "represented_evaluator_observation_projection.v1",
            "include_paths": ["/required_evidence"],
            "required_paths": ["/required_evidence"],
            "max_serialised_chars": 10_000,
        }
    }

    with pytest.raises(CertificationContractValidationError) as exc_info:
        project_represented_evaluator_observation({}, evaluator_spec)

    assert "evaluator_observation_projection_required_path_missing" in _blocker_codes(
        exc_info.value
    )


def test_contract_rejects_unbounded_or_malformed_evaluator_projection() -> None:
    scenario = _scenario()
    scenario["evaluator_specs"][0]["observation_projection"] = {
        "schema_version": "wrong.v1",
        "include_paths": ["not-a-pointer"],
        "required_paths": ["/missing-from-includes"],
        "max_serialised_chars": 0,
    }

    with pytest.raises(CertificationContractValidationError) as exc_info:
        parse_operational_certification_contract(_suite(scenarios=[scenario]))

    assert {
        "evaluator_observation_projection_schema_invalid",
        "evaluator_observation_projection_path_invalid",
        "evaluator_observation_projection_required_paths_invalid",
        "evaluator_observation_projection_limit_invalid",
    }.issubset(_blocker_codes(exc_info.value))


def test_parse_contract_accepts_selected_case_set_loader_projection() -> None:
    suite = _suite()
    selected = {
        "suite_id": suite["suite_id"],
        "suite_concept_id": suite["suite_concept_id"],
        "case_set": "certification",
        "cases": suite["case_sets"]["certification"],
        "rubric": suite["rubric"],
        "source": "vontology",
    }

    contract = parse_operational_certification_contract(selected)

    assert contract.case_set == "certification"
    assert contract.scenarios[0].scenario_id == "scenario_a"


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda suite: suite["rubric"].clear(),
            "operational_certification_policy_missing",
        ),
        (
            lambda suite: suite["rubric"]["operational_certification_policy"].update(
                {"strict_represented_evaluator_results": False}
            ),
            "strict_represented_evaluator_results_required",
        ),
        (
            lambda suite: suite["rubric"]["operational_certification_policy"].update(
                {"certification_gates": []}
            ),
            "represented_certification_gates_missing",
        ),
        (
            lambda suite: suite["case_sets"].update({"certification": []}),
            "certification_scenarios_missing",
        ),
        (
            lambda suite: suite["case_sets"]["certification"][0].update(
                {"evaluator_specs": []}
            ),
            "scenario_evaluator_specs_missing",
        ),
    ],
)
def test_contract_requires_represented_authority(
    mutate,
    expected_code: str,
) -> None:
    suite = _suite()
    mutate(suite)

    with pytest.raises(CertificationContractValidationError) as exc_info:
        parse_operational_certification_contract(suite)

    assert expected_code in _blocker_codes(exc_info.value)
    assert json.loads(json.dumps(exc_info.value.to_dict())) == exc_info.value.to_dict()


def test_contract_reports_missing_dependency() -> None:
    suite = _suite(scenarios=[_scenario(depends_on=["absent_scenario"])])

    with pytest.raises(CertificationContractValidationError) as exc_info:
        parse_operational_certification_contract(suite)

    blocker = next(
        item
        for item in exc_info.value.blockers
        if item.code == "scenario_dependency_missing"
    )
    assert blocker.details["dependency_id"] == "absent_scenario"


def test_contract_reports_dependency_cycle_with_path() -> None:
    suite = _suite(
        scenarios=[
            _scenario("a", depends_on=["b"]),
            _scenario("b", depends_on=["c"]),
            _scenario("c", depends_on=["a"]),
        ]
    )

    with pytest.raises(CertificationContractValidationError) as exc_info:
        parse_operational_certification_contract(suite)

    blocker = next(
        item
        for item in exc_info.value.blockers
        if item.code == "scenario_dependency_cycle"
    )
    assert blocker.details["cycle"][0] == blocker.details["cycle"][-1]
    assert set(blocker.details["cycle"][:-1]) == {"a", "b", "c"}


def test_neutral_matchers_cover_exact_subset_existence_and_count() -> None:
    evidence = {
        "state": {"status": "complete", "details": {"count": 2, "extra": True}},
        "events": [
            {"kind": "tool", "id": 1},
            {"kind": "workflow", "id": 2},
            {"kind": "tool", "id": 3},
        ],
    }

    assert (
        evaluate_matcher(
            {
                "matcher_id": "exact_status",
                "kind": "exact",
                "path": "/state/status",
                "expected": "complete",
            },
            evidence,
        )["matched"]
        is True
    )
    assert (
        evaluate_matcher(
            {
                "matcher_id": "nested_subset",
                "kind": "subset",
                "path": "state",
                "expected": {"details": {"count": 2}},
            },
            evidence,
        )["matched"]
        is True
    )
    assert (
        evaluate_matcher(
            {
                "matcher_id": "missing_field_expected",
                "kind": "exists",
                "path": "/state/missing",
                "expected": False,
            },
            evidence,
        )["matched"]
        is True
    )
    count_result = evaluate_matcher(
        {
            "matcher_id": "three_events",
            "kind": "count",
            "path": "/events",
            "operator": "gte",
            "expected": 3,
        },
        evidence,
    )
    assert count_result["matched"] is True
    assert count_result["actual_count"] == 3
    assert len(count_result["result_sha256"]) == 64


def test_exact_matcher_preserves_json_scalar_types() -> None:
    result = evaluate_matcher(
        {
            "matcher_id": "boolean_not_integer",
            "kind": "exact",
            "path": "/value",
            "expected": True,
        },
        {"value": 1},
    )

    assert result["matched"] is False


def test_subset_matcher_uses_distinct_sequence_members() -> None:
    result = evaluate_matcher(
        {
            "matcher_id": "duplicate_subset",
            "kind": "subset",
            "path": "/values",
            "expected": [1, 1],
        },
        {"values": [1]},
    )

    assert result["matched"] is False


def test_count_matcher_reports_non_countable_target() -> None:
    result = evaluate_matcher(
        {
            "matcher_id": "scalar_count",
            "kind": "count",
            "path": "/value",
            "operator": "eq",
            "expected": 1,
        },
        {"value": 42},
    )

    assert result["matched"] is False
    assert result["reason_code"] == "count_matcher_target_not_countable"


def test_invalid_matcher_is_a_typed_contract_error() -> None:
    with pytest.raises(CertificationContractValidationError) as exc_info:
        evaluate_matcher(
            {
                "matcher_id": "bad",
                "kind": "regex",
                "path": "/value",
                "expected": "semantic policy must not be smuggled here",
            },
            {"value": "anything"},
        )

    assert "matcher_kind_invalid" in _blocker_codes(exc_info.value)


def test_budget_evaluation_uses_represented_numeric_limit() -> None:
    budget = {
        "budget_id": "tool_budget",
        "measurement_path": "/metrics/tool_calls",
        "operator": "lte",
        "limit": 4,
        "blocking": True,
    }

    within = evaluate_budget(budget, {"metrics": {"tool_calls": 4}})
    exceeded = evaluate_budget(budget, {"metrics": {"tool_calls": 5}})
    missing = evaluate_budget(budget, {"metrics": {}})
    non_numeric = evaluate_budget(budget, {"metrics": {"tool_calls": "many"}})
    non_finite = evaluate_budget(budget, {"metrics": {"tool_calls": float("inf")}})

    assert within["within_budget"] is True
    assert exceeded["within_budget"] is False
    assert missing["reason_code"] == "budget_measurement_missing"
    assert non_numeric["reason_code"] == "budget_measurement_not_numeric"
    assert non_finite["reason_code"] == "budget_measurement_not_finite"


def test_trial_requires_a_represented_evaluator_result() -> None:
    contract = parse_operational_certification_contract(_suite())
    observation = _observation("scenario_a", 1)
    observation["represented_evaluator_results"] = {}

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=observation,
    )

    assert result["passed"] is False
    assert result["valid_evaluator_result_count"] == 0
    assert {item["code"] for item in result["blockers"]} == {
        "represented_evaluator_result_missing"
    }


def test_trial_rejects_observation_for_a_different_scenario_or_out_of_range() -> None:
    contract = parse_operational_certification_contract(_suite())
    mismatched = _observation("scenario_a", 1)
    mismatched["scenario_id"] = "scenario_b"

    with pytest.raises(CertificationContractValidationError) as mismatch_info:
        evaluate_scenario_trial(
            contract,
            scenario_id="scenario_a",
            observation=mismatched,
        )
    with pytest.raises(CertificationContractValidationError) as range_info:
        evaluate_scenario_trial(
            contract,
            scenario_id="scenario_a",
            observation=_observation("scenario_a", 6),
        )

    assert "trial_observation_scenario_mismatch" in _blocker_codes(mismatch_info.value)
    assert "trial_index_out_of_range" in _blocker_codes(range_info.value)


def test_trial_rejects_wrong_evaluator_schema_and_missing_evidence() -> None:
    contract = parse_operational_certification_contract(_suite())

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=_observation(
            "scenario_a",
            1,
            schema_version="wrong.v1",
            evidence=[],
        ),
    )

    assert result["passed"] is False
    assert {item["code"] for item in result["blockers"]} == {
        "represented_evaluator_result_schema_mismatch",
        "represented_evaluator_evidence_missing",
    }


def test_valid_non_passing_evaluator_verdict_is_an_outcome_not_a_shape_blocker() -> (
    None
):
    contract = parse_operational_certification_contract(_suite())

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=_observation("scenario_a", 1, verdict="fail"),
    )

    assert result["passed"] is False
    assert result["blockers"] == []
    assert result["passing_evaluator_count"] == 0


def test_trial_blocks_on_represented_minefield_and_budget() -> None:
    contract = parse_operational_certification_contract(_suite())

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=_observation(
            "scenario_a",
            1,
            forbidden_writes=[{"kind": "delete"}],
            latency_ms=101,
        ),
    )

    assert result["passed"] is False
    assert result["minefield_results"][0]["triggered"] is True
    assert result["budget_results"][0]["within_budget"] is False
    assert {item["code"] for item in result["blockers"]} == {
        "minefield_triggered",
        "scenario_budget_exceeded",
    }


@pytest.mark.parametrize(
    ("measurement", "reason_code"),
    [
        (_MISSING_BUDGET_MEASUREMENT, "budget_measurement_missing"),
        ("slow", "budget_measurement_not_numeric"),
    ],
)
def test_invalid_budget_measurement_is_not_reported_as_an_exceeded_value(
    measurement: object,
    reason_code: str,
) -> None:
    contract = parse_operational_certification_contract(_suite())
    observation = _observation("scenario_a", 1)
    if measurement is _MISSING_BUDGET_MEASUREMENT:
        observation.pop("latency_ms")
    else:
        observation["latency_ms"] = measurement

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=observation,
    )

    budget_result = result["budget_results"][0]
    budget_blocker = next(
        item
        for item in result["blockers"]
        if item["code"].startswith("scenario_budget")
    )
    assert result["passed"] is False
    assert budget_result["reason_code"] == reason_code
    assert budget_blocker["code"] == f"scenario_{reason_code}"
    assert budget_blocker["details"] == {
        "budget_id": "latency_budget",
        "reason_code": reason_code,
    }


def test_campaign_separates_budget_measurement_failures_from_exceedances() -> None:
    contract = parse_operational_certification_contract(_suite())
    missing = _observation("scenario_a", 1)
    missing.pop("latency_ms")
    observations = [
        missing,
        _observation("scenario_a", 2, latency_ms=101),
        *[_observation("scenario_a", index) for index in range(3, 6)],
    ]
    trial_results = {
        "scenario_a": [
            evaluate_scenario_trial(
                contract,
                scenario_id="scenario_a",
                observation=observation,
            )
            for observation in observations
        ]
    }

    report = aggregate_five_trial_campaign(contract, trial_results)

    assert report["budget_violation_count"] == 2
    assert report["blocking_budget_violation_count"] == 2
    assert report["budget_measurement_failure_count"] == 1
    assert report["blocking_budget_measurement_failure_count"] == 1
    assert report["budget_threshold_exceedance_count"] == 1
    assert report["blocking_budget_threshold_exceedance_count"] == 1


def test_blocking_minefield_requires_audit_evidence_before_it_can_be_cleared() -> None:
    contract = parse_operational_certification_contract(_suite())
    observation = _observation("scenario_a", 1)
    observation.pop("forbidden_writes")

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=observation,
    )

    assert result["passed"] is False
    assert "minefield_evidence_missing" in {item["code"] for item in result["blockers"]}


def test_nonblocking_minefield_is_visible_without_overriding_evaluator_success() -> (
    None
):
    suite = _suite(scenarios=[_scenario(minefield_blocking=False)])
    contract = parse_operational_certification_contract(suite)

    result = evaluate_scenario_trial(
        contract,
        scenario_id="scenario_a",
        observation=_observation(
            "scenario_a",
            1,
            forbidden_writes=[{"kind": "diagnostic_marker"}],
        ),
    )

    assert result["passed"] is True
    assert result["blockers"][0]["code"] == "minefield_triggered"
    assert result["blockers"][0]["blocking"] is False


def test_five_trial_aggregation_computes_pass_windows_by_scenario_and_family() -> None:
    suite = _suite(
        scenarios=[
            _scenario("scenario_a", family_id="family_one"),
            _scenario("scenario_b", family_id="family_two"),
        ],
        minimum_pass_three_rate=0.5,
    )
    contract, results = _trial_results(
        suite,
        {
            "scenario_a": [True, True, True, False, False],
            "scenario_b": [True, False, True, True, True],
        },
    )

    report = aggregate_five_trial_campaign(contract, results)

    assert report["scenarios"]["scenario_a"]["pass_windows"] == {
        "pass^1": True,
        "pass^3": True,
        "pass^5": False,
    }
    assert report["scenarios"]["scenario_b"]["pass_windows"] == {
        "pass^1": True,
        "pass^3": False,
        "pass^5": False,
    }
    assert report["pass_rates"] == {
        "pass^1": 1.0,
        "pass^3": 0.5,
        "pass^5": 0.0,
    }
    assert report["families"]["family_one"]["pass_rates"]["pass^3"] == 1.0
    assert report["families"]["family_two"]["pass_rates"]["pass^3"] == 0.0
    assert report["represented_evaluator_coverage_rate"] == 1.0
    assert report["failed_certification_gate_ids"] == []
    assert report["certified"] is True
    assert len(report["report_sha256"]) == 64
    assert json.loads(json.dumps(report)) == report


def test_represented_gate_threshold_changes_campaign_verdict_without_code_change() -> (
    None
):
    scenarios = [
        _scenario("scenario_a", family_id="family_one"),
        _scenario("scenario_b", family_id="family_two"),
    ]
    permissive_suite = _suite(
        scenarios=deepcopy(scenarios),
        minimum_pass_three_rate=0.5,
    )
    strict_suite = _suite(
        scenarios=deepcopy(scenarios),
        minimum_pass_three_rate=0.75,
    )
    outcomes = {
        "scenario_a": [True, True, True, False, False],
        "scenario_b": [True, False, True, True, True],
    }
    permissive_contract, permissive_results = _trial_results(
        permissive_suite,
        outcomes,
    )
    strict_contract, strict_results = _trial_results(strict_suite, outcomes)

    permissive = aggregate_five_trial_campaign(
        permissive_contract,
        permissive_results,
    )
    strict = aggregate_five_trial_campaign(strict_contract, strict_results)

    assert permissive["certified"] is True
    assert strict["certified"] is False
    assert strict["failed_certification_gate_ids"] == ["minimum_pass_three_rate"]


def test_campaign_gates_can_consume_separately_represented_campaign_evidence() -> None:
    suite = _suite()
    suite["rubric"]["operational_certification_policy"]["certification_gates"].append(
        {
            "gate_id": "recovery_rate_floor",
            "budget": {
                "budget_id": "represented_recovery_rate_floor",
                "measurement_path": (
                    "/represented_campaign_evidence/recoverable_fault_recovery_rate"
                ),
                "operator": "gte",
                "limit": 0.8,
                "blocking": True,
            },
        }
    )
    contract, results = _trial_results(
        suite,
        {"scenario_a": [True, True, True, True, True]},
    )

    passed = aggregate_five_trial_campaign(
        contract,
        results,
        represented_campaign_evidence={"recoverable_fault_recovery_rate": 0.8},
    )
    failed = aggregate_five_trial_campaign(
        contract,
        results,
        represented_campaign_evidence={"recoverable_fault_recovery_rate": 0.79},
    )

    assert passed["certified"] is True
    assert failed["certified"] is False
    assert failed["failed_certification_gate_ids"] == ["recovery_rate_floor"]


def test_incomplete_trials_are_a_typed_campaign_blocker() -> None:
    contract, results = _trial_results(
        _suite(),
        {"scenario_a": [True, True, True, True, True]},
    )
    results["scenario_a"].pop()

    report = aggregate_five_trial_campaign(contract, results)

    assert report["certified"] is False
    assert report["scenarios"]["scenario_a"]["complete"] is False
    assert "scenario_trials_incomplete" in {
        blocker["code"] for blocker in report["blockers"]
    }


def test_campaign_rejects_tampered_or_unexpected_trial_evidence() -> None:
    contract, results = _trial_results(
        _suite(),
        {"scenario_a": [True, True, True, True, True]},
    )
    results["scenario_a"][0]["passed"] = False
    results["unexpected_scenario"] = [deepcopy(results["scenario_a"][1])]

    report = aggregate_five_trial_campaign(contract, results)
    codes = {blocker["code"] for blocker in report["blockers"]}

    assert report["certified"] is False
    assert "trial_result_digest_mismatch" in codes
    assert "unexpected_scenario_trial_results" in codes


def test_non_five_trial_policy_is_typed_non_certification() -> None:
    suite = _suite()
    policy = suite["rubric"]["operational_certification_policy"]
    policy["trial_count"] = 4
    policy["pass_windows"] = [1, 3]
    contract = parse_operational_certification_contract(suite)

    report = aggregate_five_trial_campaign(contract, [])

    assert report["certified"] is False
    assert "five_trial_campaign_policy_missing" in {
        blocker["code"] for blocker in report["blockers"]
    }


def test_json_projection_rejects_opaque_and_non_finite_evidence() -> None:
    with pytest.raises(TypeError, match="non-JSON"):
        json_serialisable_projection({"opaque": object()})
    with pytest.raises(TypeError, match="non-finite"):
        json_serialisable_projection({"value": float("nan")})


def test_json_projection_canonicalises_native_datetime_evidence() -> None:
    aware = datetime(
        2026,
        7,
        15,
        1,
        2,
        3,
        456789,
        tzinfo=timezone(timedelta(hours=12)),
    )
    naive = datetime(2026, 7, 15, 1, 2, 3, 456789)

    assert json_serialisable_projection({"aware": aware, "naive": naive}) == {
        "aware": "2026-07-15T01:02:03.456789+12:00",
        "naive": "2026-07-15T01:02:03.456789",
    }
    assert stable_payload_digest({"when": aware}) == stable_payload_digest(
        {"when": "2026-07-15T01:02:03.456789+12:00"}
    )


def test_stable_digest_is_independent_of_mapping_key_order() -> None:
    first = {"b": [2, 1], "a": {"value": True}}
    second = {"a": {"value": True}, "b": [2, 1]}

    assert stable_payload_digest(first) == stable_payload_digest(second)
    assert stable_payload_digest({"value": True}) != stable_payload_digest({"value": 1})


def test_repo_seed_bundle_is_a_valid_generic_contract_fixture() -> None:
    seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))

    contract = parse_operational_certification_contract(
        seed,
        case_set="executable_engineering_seed",
    )

    assert contract.suite_concept_id == "#V#operational_certification_benchmark_suite"
    assert contract.topological_scenario_ids == (
        "authenticated_identity_read_only",
        "durable_concept_profile_resume_and_idempotence",
        "represented_workflow_concept_same_session_followup",
        "typed_missing_entity_recovery",
        "unique_state_marker_create_and_read_back",
    )
    assert contract.policy["trial_count"] == 5
    scenarios = {scenario.scenario_id: scenario for scenario in contract.scenarios}
    multi_turn = scenarios["represented_workflow_concept_same_session_followup"]
    assert multi_turn.execution["adapter_id"] == (
        "#V#authenticated_von_multi_turn_operational_adapter"
    )
    assert len(multi_turn.execution["inputs"]["turns"]) == 3
    assert multi_turn.reset_policy == {"mode": "new_chat_session"}
    assert multi_turn.metadata["pilot_acceptance_eligible"] is False

    durable = scenarios["durable_concept_profile_resume_and_idempotence"]
    assert durable.execution["adapter_id"] == (
        "#V#durable_workflow_execute_operational_adapter"
    )
    assert durable.execution["inputs"]["workflow_id"] == (
        "#V#concept_search_instance_retrieval_workflow"
    )
    assert durable.execution["inputs"]["submission_plan"] == [
        {"await_terminal": False, "timeout_seconds": 0.0},
        {"await_terminal": True, "timeout_seconds": 120.0},
    ]
    assert durable.reset_policy == {"mode": "read_only"}
    assert durable.permitted_effects == ()
    assert durable.metadata["pilot_acceptance_eligible"] is False

    unique_state = scenarios["unique_state_marker_create_and_read_back"]
    absence_probe = unique_state.reset_policy["authoritative_absence_probe"]
    assert absence_probe == {
        "workflow_id": "#V#operational_marker_absence_probe_workflow",
        "inputs": {"isolation_id": "{{isolation_id}}"},
        "required_action_ids": [
            "workflow_mcp.invoke_tool",
            "workflow_control.context_project",
        ],
    }


def test_repo_seed_bundle_declares_the_agreed_trusted_sail_pilot_contract() -> None:
    seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))

    contract = parse_operational_certification_contract(seed)

    assert seed["seed_version"] == 7
    assert seed["known_legacy_authority_payload_sha256_by_seed_version"]["6"] == [
        "4f39d85811751e3b98d6c0624fd06cbffa55e44bd8eedffc9e559b12029525dc"
    ]
    assert seed["known_legacy_authority_payload_sha256_by_seed_version"]["5"] == [
        "0b2c86e5664bda819ea747a6b44c7cac5f2b14d45c719256ce691dff021f95a1"
    ]
    assert contract.case_set == "trusted_sail_pilot_v1"
    assert {scenario.scenario_id for scenario in contract.scenarios} == {
        "pilot_kb_relation_rag_read_only",
        "pilot_arxiv_mcp_read_only",
        "pilot_jira_mcp_read_only",
        "pilot_multi_tool_research_briefing",
        "pilot_same_session_referent_followup",
        "pilot_durable_workflow_resume_and_idempotence",
        "pilot_unique_state_marker_create_and_read_back",
        "pilot_typed_missing_entity_recovery",
        "pilot_represented_degraded_fault_matrix",
        "pilot_durable_checkpoint_interruption_and_resume",
    }
    assert all(not scenario.depends_on for scenario in contract.scenarios)
    assert all(
        scenario.metadata["pilot_acceptance_eligible"] is True
        for scenario in contract.scenarios
    )

    cohort = contract.policy["pilot_cohort"]
    assert cohort == {
        "status": "agreed",
        "profile": "trusted_sail",
        "organisation_concept_id": (
            "#V#university_of_auckland_strong_ai_lab"
        ),
        "actor_concept_ids": [
            "#V#michael_witbrock",
            "#V#zhan_von_witbrock",
        ],
        "aggregation_policy": (
            "all_actors_must_independently_satisfy_all_certification_gates"
        ),
    }
    envelopes = contract.policy["pilot_envelopes"]
    assert envelopes["status"] == "agreed_initial"
    assert envelopes["latency"] == {
        "single_turn_max_ms": 120000,
        "durable_max_ms": 180000,
        "multi_turn_max_ms": 360000,
    }
    assert envelopes["clarification"]["max_count_per_trial"] == 1
    assert envelopes["correction"]["max_count_per_trial"] == 0
    assert envelopes["revision_policy"] == {
        "basis": "measured_campaign_evidence",
        "requires_live_vontology_revision": True,
        "no_silent_relaxation": True,
    }

    scenarios = {scenario.scenario_id: scenario for scenario in contract.scenarios}
    expected_tools = {
        "pilot_kb_relation_rag_read_only": ["search_knowledge_base"],
        "pilot_arxiv_mcp_read_only": ["search_arxiv"],
        "pilot_jira_mcp_read_only": ["jira_get_issue"],
        "pilot_multi_tool_research_briefing": [
            "search_knowledge_base",
            "search_arxiv",
            "jira_get_issue",
        ],
    }
    for scenario_id, required_tools in expected_tools.items():
        scenario = scenarios[scenario_id]
        tool_check = next(
            check
            for check in scenario.checks
            if check["matcher_id"] == "required_observed_tools_present"
        )
        assert tool_check == {
            "matcher_id": "required_observed_tools_present",
            "kind": "subset",
            "path": "/path_analysis/observed_tool_names",
            "expected": required_tools,
        }

    assert next(
        check
        for check in scenarios["pilot_typed_missing_entity_recovery"].checks
        if check["matcher_id"]
        == "at_least_one_tool_observed_for_semantic_evaluation"
    ) == {
        "matcher_id": "at_least_one_tool_observed_for_semantic_evaluation",
        "kind": "count",
        "path": "/path_analysis/observed_tool_names",
        "operator": "gte",
        "expected": 1,
    }

    for scenario in contract.scenarios:
        input_text = json.dumps(scenario.execution.get("inputs") or {})
        assert "Michael" not in input_text
        assert "Zhan" not in input_text
        measurement_paths = {
            budget["measurement_path"] for budget in scenario.budgets
        }
        assert "/operational_metrics/duration_ms" in measurement_paths
        burden_evidence = scenario.metadata["interaction_burden_evidence"]
        if burden_evidence.get("follow_up_request_applicable") is False:
            assert (
                "/operational_metrics/follow_up_request_count"
                not in measurement_paths
            )
            assert burden_evidence["follow_up_request_measurement"] == (
                "not_applicable"
            )
        else:
            assert (
                "/operational_metrics/follow_up_request_count"
                in measurement_paths
            )
        assert "/operational_metrics/correction_count" not in measurement_paths
        assert burden_evidence[
            "correction_measurement_status"
        ] == "pending_represented_correction_event_telemetry"

    marker_probe = scenarios[
        "pilot_unique_state_marker_create_and_read_back"
    ].reset_policy["authoritative_postcondition_probe"]
    assert marker_probe["workflow_id"] == (
        "#V#operational_marker_readback_probe_workflow"
    )
    assert marker_probe["expected_cardinality"] == 1

    degraded = scenarios["pilot_represented_degraded_fault_matrix"]
    assert degraded.execution["inputs"]["workflow_id"] == (
        "#V#operational_degraded_fault_matrix_probe_workflow"
    )
    assert degraded.reset_policy["isolation_binding_paths"] == [
        "/workflow_inputs/isolation_id"
    ]
    assert degraded.permitted_effects[0]["cardinality"] == 1
    degraded_matchers = {check["matcher_id"]: check for check in degraded.checks}
    assert degraded_matchers["authoritative_empty_has_zero_candidates"][
        "expected"
    ] == []
    assert degraded_matchers["invalid_argument_type_exact"]["expected"] == (
        "invalid_arguments"
    )
    assert degraded_matchers["invalid_argument_stage_exact"]["expected"] == (
        "input_schema"
    )
    assert degraded_matchers["invalid_argument_not_retryable"]["expected"] is False
    assert degraded_matchers["input_requirement_outcome_exact"]["expected"] == (
        "input_required"
    )
    assert degraded_matchers["input_requirement_names_concept_id"][
        "expected"
    ] == ["concept_id"]
    assert degraded_matchers[
        "input_requirement_preserves_source_schema_failure"
    ]["expected"] == "schema_validation_failed"
    assert degraded_matchers["read_only_mutation_guard_reason_exact"][
        "expected"
    ] == "insufficient_mutation_authority"
    assert degraded_matchers["wrong_target_contract_error_exact"]["expected"] == (
        "target_contract_symbolic_mismatch"
    )

    interrupted = scenarios["pilot_durable_checkpoint_interruption_and_resume"]
    assert interrupted.execution["inputs"]["workflow_id"] == (
        "#V#operational_checkpoint_interruption_probe_workflow"
    )
    assert [
        step.get("operation", "execute")
        for step in interrupted.execution["inputs"]["submission_plan"]
    ] == ["execute", "await_status", "resume", "execute"]
    interruption_matchers = {
        check["matcher_id"]: check for check in interrupted.checks
    }
    assert interruption_matchers["pause_claim_fence_digest_observed"]["path"] == (
        "/path_analysis/checkpoint_pause_receipts/0/claim_token_sha256"
    )
    assert interruption_matchers[
        "represented_checkpoint_resume_stage_reached"
    ]["expected"] is True
    assert interruption_matchers[
        "represented_same_instance_resume_confirmed"
    ]["expected"] is True
    assert interruption_matchers[
        "represented_pause_checkpoint_state_exact"
    ]["expected"] == "mark_resumed"
    assert interruption_matchers[
        "represented_resume_checkpoint_state_exact"
    ]["expected"] == "mark_resumed"
    assert interruption_matchers["represented_resume_count_exact"]["expected"] == 1


def test_repo_seed_declares_generic_transient_mcp_fault_recovery_case() -> None:
    seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    seed["default_case_set"] = "transient_mcp_fault_recovery_v1"
    contract = parse_operational_certification_contract(seed)

    assert len(contract.scenarios) == 1
    scenario = contract.scenarios[0]
    assert scenario.scenario_id == (
        "transient_mcp_fault_chain_recovers_with_bounded_retries"
    )
    assert scenario.execution["adapter_id"] == (
        "#V#synchronous_represented_workflow_operational_adapter"
    )
    assert scenario.execution["inputs"]["workflow_id"] == (
        "#V#operational_mcp_fault_recovery_probe_workflow"
    )
    fault_contract = scenario.fault_injection
    assert fault_contract["recoverable"] is True
    assert fault_contract["bounded_recovery_budget"] == 3
    assert fault_contract["require_all_fault_rules_consumed"] is True
    plan = fault_contract["agent_test_mcp_fault_plan"]
    assert [rule["fault_class"] for rule in plan["faults"]] == [
        "timeout",
        "rate_limit",
        "temporary_unavailability",
    ]
    assert {rule["tool_name"] for rule in plan["faults"]} == {
        "resolve_concept_by_name"
    }
    assert scenario.execution["inputs"]["timeout_seconds"] == 180.0
    assert "submission_plan" not in scenario.execution["inputs"]
    checks = {check["matcher_id"]: check for check in scenario.checks}
    assert checks["timeout_fault_observed_first"]["expected"] == "timeout"
    assert checks["rate_limit_fault_observed_second"]["expected"] == "rate_limit"
    assert checks["temporary_unavailability_fault_observed_third"]["expected"] == (
        "temporary_unavailability"
    )
    assert checks["represented_recovery_attempt_count_exact"]["expected"] == 3
    assert checks["represented_recovery_final_read_completed"]["expected"] == (
        "not_found"
    )
    assert scenario.permitted_effects == ()
    assert scenario.metadata["pilot_acceptance_eligible"] is False


def test_negative_readback_control_rejects_all_five_contradictory_trials() -> None:
    seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    negative_suite = load_benchmark_suite_case_set(
        suite_concept_id="#V#operational_certification_benchmark_suite",
        case_set="certification_negative_controls_v1",
        fixture_path=_SEED_PATH,
    )
    contract = parse_operational_certification_contract(negative_suite)
    scenario = contract.scenarios[0]
    trusted_degraded = next(
        item
        for item in seed["case_sets"]["trusted_sail_pilot_v1"]
        if item["scenario_id"] == "pilot_represented_degraded_fault_matrix"
    )

    assert scenario.scenario_id == "negative_readback_mismatch_must_not_certify"
    assert scenario.fault_injection == {
        "fault_classes": ["readback_mismatch"],
        "recoverable": False,
        "injection_surface": "represented_scenario_postcondition_expectation",
    }
    assert scenario.metadata["negative_control"] is True
    assert scenario.metadata["expected_campaign_certified"] is False
    assert (
        scenario.reset_policy["authoritative_postcondition_probe"][
            "expected_description"
        ]
        != trusted_degraded["reset_policy"]["authoritative_postcondition_probe"][
            "expected_description"
        ]
    )

    trial_results = []
    for trial_index in range(1, 6):
        observation = {
            "scenario_id": scenario.scenario_id,
            "trial_index": trial_index,
            "path_analysis": {
                "workflow_id": "#V#operational_degraded_fault_matrix_probe_workflow",
                "execution": {
                    "workflow_output": {
                        "represented_operational_degraded_fault_matrix_result": {
                            "committed_effect_count": 1,
                            "readback_concept_id": (
                                f"#V#negative_control_marker_{trial_index}"
                            ),
                        }
                    }
                },
            },
            "authoritative_postcondition_probe": {
                "verified": False,
                "expected_description": (
                    "Deliberately contradictory certification marker "
                    f"negative-control-{trial_index}"
                ),
                "observed_description": (
                    "Trusted SAIL pilot certification marker "
                    f"negative-control-{trial_index}"
                ),
            },
            "final_state_snapshot": {
                "committed_effects": [
                    {"concept_id": f"#V#negative_control_marker_{trial_index}"}
                ]
            },
            "represented_safety_audits": {
                "forbidden_effects": [],
                "namespace_violations": [],
                "false_success_claims": [],
                "fabricated_evidence": [],
            },
            "operational_metrics": {
                "duration_ms": 25,
                "follow_up_request_count": 0,
            },
            "represented_evaluator_results": {
                "#V#operational_state_evidence_evaluator": {
                    "schema_version": (
                        REPRESENTED_OPERATIONAL_EVALUATOR_RESULT_SCHEMA_VERSION
                    ),
                    "evaluator_id": "#V#operational_state_evidence_evaluator",
                    "scenario_id": scenario.scenario_id,
                    "trial_index": trial_index,
                    "verdict": "fail",
                    "terminal_state": "readback_mismatch",
                    "evidence": [
                        {
                            "kind": "authoritative_postcondition_readback",
                            "verified": False,
                        }
                    ],
                }
            },
        }
        result = evaluate_scenario_trial(
            contract,
            scenario_id=scenario.scenario_id,
            observation=observation,
        )
        checks = {item["matcher_id"]: item for item in result["check_results"]}

        assert result["valid_evaluator_result_count"] == 1
        assert result["passing_evaluator_count"] == 0
        assert checks["authoritative_postcondition_probe_verified"]["matched"] is False
        assert result["passed"] is False
        trial_results.append(result)

    report = aggregate_five_trial_campaign(
        contract,
        {scenario.scenario_id: trial_results},
    )

    assert report["represented_evaluator_coverage_rate"] == 1.0
    assert report["blocking_minefield_trigger_count"] == 0
    assert report["blocking_budget_violation_count"] == 0
    assert report["scenarios"][scenario.scenario_id]["pass_windows"] == {
        "pass^1": False,
        "pass^3": False,
        "pass^5": False,
    }
    assert "overall_pass_three_minimum" in report["failed_certification_gate_ids"]
    assert report["certified"] is False


def test_repo_seed_bundle_round_trips_through_existing_benchmark_loader() -> None:
    definition = load_benchmark_suite_definition_from_seed_fixture(_SEED_PATH)
    selected_case_set = load_benchmark_suite_case_set(
        suite_concept_id="#V#operational_certification_benchmark_suite",
        fixture_path=_SEED_PATH,
    )

    definition_contract = parse_operational_certification_contract(definition)
    selected_contract = parse_operational_certification_contract(selected_case_set)

    assert definition_contract.contract_sha256 == selected_contract.contract_sha256
    assert selected_contract.source == "seed_bundle_import_fixture"
    assert definition["seed_version"] == 7
    assert selected_case_set["seed_version"] == 7
