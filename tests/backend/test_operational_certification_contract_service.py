from __future__ import annotations

from copy import deepcopy
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
                "evidence": evidence
                if evidence is not None
                else [{"kind": "state_readback", "sha256": f"evidence-{trial_index}"}],
            }
        },
    }


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

    assert within["within_budget"] is True
    assert exceeded["within_budget"] is False
    assert missing["reason_code"] == "budget_measurement_missing"


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


def test_stable_digest_is_independent_of_mapping_key_order() -> None:
    first = {"b": [2, 1], "a": {"value": True}}
    second = {"a": {"value": True}, "b": [2, 1]}

    assert stable_payload_digest(first) == stable_payload_digest(second)
    assert stable_payload_digest({"value": True}) != stable_payload_digest({"value": 1})


def test_repo_seed_bundle_is_a_valid_generic_contract_fixture() -> None:
    seed = json.loads(_SEED_PATH.read_text(encoding="utf-8"))

    contract = parse_operational_certification_contract(seed)

    assert contract.suite_concept_id == "#V#operational_certification_benchmark_suite"
    assert contract.topological_scenario_ids == (
        "authenticated_identity_read_only",
        "typed_missing_entity_recovery",
        "unique_state_message_create_and_read_back",
    )
    assert contract.policy["trial_count"] == 5
    assert all(
        scenario.execution["adapter_id"]
        == "#V#authenticated_von_generate_operational_adapter"
        for scenario in contract.scenarios
    )


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
