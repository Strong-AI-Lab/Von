"""Generic execution bridge for represented operational certification suites.

The represented suite owns scenarios, dependencies, checks, evaluators,
minefields, budgets, and gates.  This module supplies only the repeated-run
mechanics: isolation/reset, adapter invocation, evidence capture, strict
evaluator hand-off, aggregation, and optional experiment persistence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .operational_certification_contract_service import (
    OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION,
    OperationalCertificationContract,
    CertificationScenarioContract,
    aggregate_five_trial_campaign,
    evaluate_scenario_trial,
    json_serialisable_projection,
    stable_payload_digest,
    validate_operational_certification_campaign_result_integrity,
)


OPERATIONAL_CERTIFICATION_EXECUTION_SCHEMA_VERSION = (
    "operational_certification_execution.v1"
)
STRICT_CERTIFICATION_EXPERIMENT_OBSERVATION_SCHEMA_VERSION = (
    "strict_certification_experiment_observation.v1"
)

ScenarioResetter = Callable[
    [CertificationScenarioContract, int],
    Mapping[str, Any],
]
ScenarioExecutor = Callable[
    [CertificationScenarioContract, int, Mapping[str, Any]],
    Mapping[str, Any],
]
RepresentedEvaluatorExecutor = Callable[
    [
        CertificationScenarioContract,
        Mapping[str, Any],
        int,
        Mapping[str, Any],
    ],
    Mapping[str, Any],
]
ObservationRecorder = Callable[[Mapping[str, Any]], Any]


def validate_campaign_experiment_observation(
    value: Any,
) -> list[dict[str, Any]]:
    """Validate the strict persisted envelope around one campaign report."""

    if not isinstance(value, Mapping):
        return [{"code": "campaign_observation_mapping_required"}]
    observation = json_serialisable_projection(value)
    errors: list[dict[str, Any]] = []
    if observation.get("schema_version") != (
        STRICT_CERTIFICATION_EXPERIMENT_OBSERVATION_SCHEMA_VERSION
    ):
        errors.append({"code": "campaign_observation_schema_invalid"})
    if observation.get("observation_type") != "operational_certification_campaign":
        errors.append({"code": "campaign_observation_type_invalid"})
    evidence = observation.get("evidence")
    campaign = (
        evidence.get("operational_certification_campaign_result")
        if isinstance(evidence, Mapping)
        else None
    )
    errors.extend(
        validate_operational_certification_campaign_result_integrity(campaign)
    )
    if isinstance(campaign, Mapping):
        certified = campaign.get("certified")
        expected_verdict = "pass" if certified is True else "fail"
        if observation.get("verdict") != expected_verdict:
            errors.append({"code": "campaign_observation_verdict_mismatch"})
        outcome = observation.get("observed_outcome")
        if not isinstance(outcome, Mapping):
            errors.append({"code": "campaign_observation_outcome_missing"})
        else:
            if outcome.get("certified") is not certified:
                errors.append({"code": "campaign_observation_outcome_mismatch"})
            if outcome.get("report_sha256") != campaign.get("report_sha256"):
                errors.append({"code": "campaign_observation_digest_mismatch"})
    return errors


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blocker(
    *,
    code: str,
    scenario_id: str,
    trial_index: int,
    message: str,
    recoverable: bool,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "scope": f"scenario[{scenario_id}].trial[{trial_index}]",
        "message": message,
        "recoverable": recoverable,
        "blocking": True,
        "details": dict(details or {}),
    }


def build_strict_experiment_observation(
    *,
    trial_result: Mapping[str, Any],
    observation: Mapping[str, Any],
    execution_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an experiment observation that cannot infer success heuristically."""

    if trial_result.get("schema_version") != (
        "operational_certification_trial_result.v1"
    ):
        raise ValueError("operational_certification_trial_result_required")
    passed = trial_result.get("passed")
    if not isinstance(passed, bool):
        raise ValueError("operational_certification_explicit_pass_required")
    represented_results = trial_result.get("represented_evaluator_results")
    if not isinstance(represented_results, Sequence) or isinstance(
        represented_results,
        (str, bytes, bytearray),
    ):
        raise ValueError("represented_certification_evaluator_results_invalid")
    blocking_blockers = [
        item
        for item in (trial_result.get("blockers") or [])
        if isinstance(item, Mapping)
        and item.get("blocking") is True
        and isinstance(item.get("code"), str)
        and str(item.get("code")).strip()
    ]
    represented_evaluator_missing = not represented_results
    if represented_evaluator_missing and (passed or not blocking_blockers):
        raise ValueError("represented_certification_evaluator_result_required")

    projected_result = json_serialisable_projection(trial_result)
    projected_observation = json_serialisable_projection(observation)
    return {
        "schema_version": STRICT_CERTIFICATION_EXPERIMENT_OBSERVATION_SCHEMA_VERSION,
        "observation_type": "operational_certification_trial",
        "label": (
            f"{trial_result.get('scenario_id')} trial {trial_result.get('trial_index')}"
        ),
        "verdict": "pass" if passed else "fail",
        "observed_outcome": {
            "scenario_id": trial_result.get("scenario_id"),
            "trial_index": trial_result.get("trial_index"),
            "passed": passed,
            "result_sha256": trial_result.get("result_sha256"),
        },
        "evidence": {
            "operational_certification_trial_result": projected_result,
            "trial_observation_sha256": stable_payload_digest(projected_observation),
            "represented_evaluator_results": projected_result.get(
                "represented_evaluator_results"
            ),
            "typed_execution_blockers": json_serialisable_projection(blocking_blockers),
        },
        "metrics": {
            "required_evaluator_count": trial_result.get("required_evaluator_count"),
            "valid_evaluator_result_count": trial_result.get(
                "valid_evaluator_result_count"
            ),
            "blocking_blocker_count": len(blocking_blockers),
            "represented_evaluator_missing": represented_evaluator_missing,
        },
        "turn_execution_request_ids": [
            str(item).strip()
            for item in (observation.get("turn_execution_request_ids") or [])
            if str(item).strip()
        ],
        "execution_provenance": json_serialisable_projection(
            execution_provenance or {}
        ),
    }


def build_campaign_experiment_observation(
    campaign_result: Mapping[str, Any],
    *,
    execution_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if campaign_result.get("schema_version") != (
        OPERATIONAL_CERTIFICATION_CAMPAIGN_RESULT_SCHEMA_VERSION
    ):
        raise ValueError("operational_certification_campaign_result_required")
    certified = campaign_result.get("certified")
    if not isinstance(certified, bool):
        raise ValueError("operational_certification_explicit_verdict_required")
    projection = json_serialisable_projection(campaign_result)
    return {
        "schema_version": STRICT_CERTIFICATION_EXPERIMENT_OBSERVATION_SCHEMA_VERSION,
        "observation_type": "operational_certification_campaign",
        "label": "Operational certification campaign",
        "verdict": "pass" if certified else "fail",
        "observed_outcome": {
            "certified": certified,
            "report_sha256": projection.get("report_sha256"),
        },
        "evidence": {"operational_certification_campaign_result": projection},
        "metrics": {
            "represented_evaluator_coverage_rate": projection.get(
                "represented_evaluator_coverage_rate"
            ),
            "blocking_minefield_trigger_count": projection.get(
                "blocking_minefield_trigger_count"
            ),
            "blocking_budget_violation_count": projection.get(
                "blocking_budget_violation_count"
            ),
        },
        "execution_provenance": json_serialisable_projection(
            execution_provenance or {}
        ),
    }


def _represented_campaign_evidence(
    contract: OperationalCertificationContract,
    trial_results: Sequence[Mapping[str, Any]],
    *,
    represented_campaign_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate represented decisions against contract-owned denominators."""

    non_successes = [item for item in trial_results if item.get("passed") is not True]

    def _trial_flag(trial: Mapping[str, Any], key: str) -> bool:
        return any(
            evaluator.get(key) is True
            for evaluator in (trial.get("represented_evaluator_results") or [])
            if isinstance(evaluator, Mapping)
        )

    typed_non_success_count = sum(
        1
        for trial in non_successes
        if _trial_flag(trial, "typed_terminal_outcome_available")
    )
    causal_complete_count = sum(
        1
        for trial in non_successes
        if _trial_flag(trial, "causal_stage_evidence_complete")
    )
    explicit_inconclusive_count = sum(
        1 for trial in non_successes if _trial_flag(trial, "explicitly_inconclusive")
    )
    scenario_by_id = contract.scenario_by_id
    recoverable_trials = [
        trial
        for trial in trial_results
        if bool(
            scenario_by_id.get(str(trial.get("scenario_id") or ""))
            and (
                scenario_by_id[str(trial.get("scenario_id") or "")].fault_injection.get(
                    "recoverable"
                )
                is True
                or scenario_by_id[str(trial.get("scenario_id") or "")].metadata.get(
                    "designated_recoverable_fault"
                )
                is True
            )
        )
    ]
    recovered_count = sum(
        1
        for trial in recoverable_trials
        if _trial_flag(trial, "recoverable_fault_recovered")
    )
    non_success_count = len(non_successes)
    recoverable_fault_case_count = len(recoverable_trials)
    evidence = {
        "source": "represented_operational_evaluator_results",
        "non_success_count": non_success_count,
        "typed_non_success_outcome_count": typed_non_success_count,
        "typed_non_success_outcome_rate": (
            typed_non_success_count / non_success_count if non_success_count else 1.0
        ),
        "causal_stage_evidence_decision_count": causal_complete_count,
        "causal_stage_evidence_rate": (
            causal_complete_count / non_success_count if non_success_count else 1.0
        ),
        "explicit_inconclusive_count": explicit_inconclusive_count,
        "causal_or_explicit_inconclusive_rate": (
            sum(
                1
                for trial in non_successes
                if _trial_flag(trial, "causal_stage_evidence_complete")
                or _trial_flag(trial, "explicitly_inconclusive")
            )
            / non_success_count
            if non_success_count
            else 1.0
        ),
        "recoverable_fault_case_count": recoverable_fault_case_count,
        "recoverable_fault_recovery_rate": (
            recovered_count / recoverable_fault_case_count
            if recoverable_fault_case_count
            else 1.0
        ),
    }
    external = (
        json_serialisable_projection(represented_campaign_evidence)
        if isinstance(represented_campaign_evidence, Mapping)
        else {}
    )
    evidence["represented_campaign_evidence_status"] = (
        "available" if external else "missing"
    )
    if external:
        evidence["represented_campaign_evidence_source"] = external.get("source")
        evidence["represented_campaign_evidence_authority"] = external.get("authority")
        evidence["represented_campaign_evidence_sha256"] = external.get(
            "evidence_sha256"
        )
        for field_name in (
            "pilot_corpus_agreed",
            "pilot_envelopes_agreed",
            "completed_learning_loop_count",
            "safe_operating_envelope",
            "learning_release_receipt_ids",
            "learning_release_authority",
            "evaluated_learning_release_candidate_bindings",
            "evaluated_learning_release_candidate_ids",
        ):
            if field_name in external:
                evidence[field_name] = external[field_name]
    return evidence


def _apply_experiment_persistence_gate(
    campaign_result: dict[str, Any],
    persistence_results: Sequence[Mapping[str, Any]],
    *,
    expected_record_count: int,
) -> None:
    failed = [row for row in persistence_results if row.get("success") is not True]
    missing_record_count = max(0, expected_record_count - len(persistence_results))
    passed = not failed and missing_record_count == 0
    gate_id = "experiment_persistence_complete"
    gate_results = [
        dict(row)
        for row in (campaign_result.get("certification_gate_results") or [])
        if isinstance(row, Mapping) and row.get("gate_id") != gate_id
    ]
    gate_result = {
        "gate_id": gate_id,
        "kind": "hard_interface",
        "passed": passed,
        "check": {
            "required_record_count": expected_record_count,
            "observed_record_count": len(persistence_results),
            "missing_record_count": missing_record_count,
            "failed_record_count": len(failed),
            "campaign_report_persistence_required_separately": True,
        },
    }
    gate_result["result_sha256"] = stable_payload_digest(gate_result)
    gate_results.append(gate_result)
    campaign_result["certification_gate_results"] = gate_results
    campaign_result["failed_certification_gate_ids"] = [
        str(row.get("gate_id") or "")
        for row in gate_results
        if row.get("passed") is not True
    ]
    blockers = [
        dict(row)
        for row in (campaign_result.get("blockers") or [])
        if isinstance(row, Mapping)
        and row.get("code") != "certification_evidence_persistence_failed"
    ]
    if failed or missing_record_count:
        blockers.append(
            {
                "schema_version": "operational_certification_blocker.v1",
                "code": "certification_evidence_persistence_failed",
                "scope": "campaign.experiment_persistence",
                "message": "Required certification evidence was not persisted.",
                "recoverable": True,
                "blocking": True,
                "details": {
                    "failed_record_count": len(failed),
                    "missing_record_count": missing_record_count,
                    "failed_scopes": [
                        str(row.get("scope") or row.get("scenario_id") or "unknown")
                        for row in failed
                    ],
                },
            }
        )
    campaign_result["blockers"] = blockers
    campaign_result["experiment_persistence_required"] = True
    campaign_result["experiment_persistence_complete"] = passed
    campaign_result["certified"] = bool(
        gate_results
        and all(row.get("passed") is True for row in gate_results)
        and not any(row.get("blocking") is True for row in blockers)
    )
    campaign_result.pop("report_sha256", None)
    campaign_result["report_sha256"] = stable_payload_digest(campaign_result)


def _apply_hard_campaign_gates(
    campaign_result: dict[str, Any],
    gates: Sequence[Mapping[str, Any]],
) -> None:
    gate_results = [
        dict(row)
        for row in (campaign_result.get("certification_gate_results") or [])
        if isinstance(row, Mapping)
    ]
    blockers = [
        dict(row)
        for row in (campaign_result.get("blockers") or [])
        if isinstance(row, Mapping)
    ]
    for raw_gate in gates:
        gate_id = str(raw_gate.get("gate_id") or "").strip()
        if not gate_id:
            continue
        gate_results = [row for row in gate_results if row.get("gate_id") != gate_id]
        passed = raw_gate.get("passed") is True
        gate_result = {
            "gate_id": gate_id,
            "kind": "hard_interface",
            "passed": passed,
            "check": json_serialisable_projection(raw_gate.get("details") or {}),
        }
        gate_result["result_sha256"] = stable_payload_digest(gate_result)
        gate_results.append(gate_result)
        blocker_code = str(raw_gate.get("blocker_code") or gate_id).strip()
        blockers = [row for row in blockers if row.get("code") != blocker_code]
        if not passed:
            blockers.append(
                {
                    "schema_version": "operational_certification_blocker.v1",
                    "code": blocker_code,
                    "scope": "campaign.hard_interface",
                    "message": str(raw_gate.get("message") or blocker_code),
                    "recoverable": bool(raw_gate.get("recoverable", True)),
                    "blocking": True,
                    "details": json_serialisable_projection(
                        raw_gate.get("details") or {}
                    ),
                }
            )
    campaign_result["certification_gate_results"] = gate_results
    campaign_result["failed_certification_gate_ids"] = [
        str(row.get("gate_id") or "")
        for row in gate_results
        if row.get("passed") is not True
    ]
    campaign_result["blockers"] = blockers
    campaign_result["certified"] = bool(
        gate_results
        and all(row.get("passed") is True for row in gate_results)
        and not any(row.get("blocking") is True for row in blockers)
    )
    campaign_result.pop("report_sha256", None)
    campaign_result["report_sha256"] = stable_payload_digest(campaign_result)


def run_operational_certification_campaign(
    contract: OperationalCertificationContract,
    *,
    reset_scenario: ScenarioResetter | None,
    execute_scenario: ScenarioExecutor | None,
    execute_represented_evaluator: RepresentedEvaluatorExecutor | None,
    record_experiment_observation: ObservationRecorder | None = None,
    execution_provenance: Mapping[str, Any] | None = None,
    hard_campaign_gates: Sequence[Mapping[str, Any]] = (),
    represented_campaign_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the represented suite as five isolated trials per scenario.

    Missing adapters, reset failures, dependency failures, execution failures,
    and evaluator failures all become typed observations.  They are never
    silently skipped or inferred as successes.
    """

    trial_count = contract.policy.get("trial_count")
    if trial_count != 5:
        raise ValueError("operational_certification_requires_five_trials")

    observations: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    persistence_results: list[dict[str, Any]] = []
    scenario_by_id = contract.scenario_by_id

    for trial_index in range(1, 6):
        passed_in_trial: dict[str, bool] = {}
        for scenario_id in contract.topological_scenario_ids:
            scenario = scenario_by_id[scenario_id]
            blockers: list[dict[str, Any]] = []
            reset_evidence: dict[str, Any] = {}
            execution_evidence: dict[str, Any] = {}

            failed_dependencies = [
                dependency
                for dependency in scenario.depends_on
                if passed_in_trial.get(dependency) is not True
            ]
            if failed_dependencies:
                blockers.append(
                    _blocker(
                        code="scenario_dependency_not_passed",
                        scenario_id=scenario_id,
                        trial_index=trial_index,
                        message=(
                            "A represented scenario dependency did not pass in "
                            "the same isolated trial."
                        ),
                        recoverable=True,
                        details={"dependency_scenario_ids": failed_dependencies},
                    )
                )

            if reset_scenario is None:
                blockers.append(
                    _blocker(
                        code="scenario_reset_adapter_missing",
                        scenario_id=scenario_id,
                        trial_index=trial_index,
                        message="No state-isolation/reset adapter was supplied.",
                        recoverable=True,
                    )
                )
            elif not blockers:
                try:
                    raw_reset = reset_scenario(scenario, trial_index)
                    reset_evidence = json_serialisable_projection(raw_reset)
                    if reset_evidence.get("success") is not True:
                        blockers.append(
                            _blocker(
                                code="scenario_reset_failed",
                                scenario_id=scenario_id,
                                trial_index=trial_index,
                                message="State isolation/reset did not report success.",
                                recoverable=True,
                                details={
                                    "reset_evidence_sha256": stable_payload_digest(
                                        reset_evidence
                                    )
                                },
                            )
                        )
                except Exception as exc:
                    blockers.append(
                        _blocker(
                            code="scenario_reset_exception",
                            scenario_id=scenario_id,
                            trial_index=trial_index,
                            message="State isolation/reset raised an exception.",
                            recoverable=True,
                            details={"error": str(exc)},
                        )
                    )

            if execute_scenario is None:
                blockers.append(
                    _blocker(
                        code="scenario_execution_adapter_missing",
                        scenario_id=scenario_id,
                        trial_index=trial_index,
                        message="No scenario execution adapter was supplied.",
                        recoverable=True,
                    )
                )
            elif not blockers:
                try:
                    raw_execution = execute_scenario(
                        scenario,
                        trial_index,
                        reset_evidence,
                    )
                    execution_evidence = json_serialisable_projection(raw_execution)
                except Exception as exc:
                    blockers.append(
                        _blocker(
                            code="scenario_execution_exception",
                            scenario_id=scenario_id,
                            trial_index=trial_index,
                            message="Scenario execution raised an exception.",
                            recoverable=True,
                            details={"error": str(exc)},
                        )
                    )

            observation: dict[str, Any] = {
                "scenario_id": scenario_id,
                "family_id": scenario.family_id,
                "trial_index": trial_index,
                "reset_evidence": reset_evidence,
                **execution_evidence,
                "typed_blockers": blockers,
                "represented_evaluator_results": {},
            }

            evaluator_results: dict[str, Any] = {}
            if execute_represented_evaluator is not None and not blockers:
                for evaluator_spec in scenario.evaluator_specs:
                    evaluator_id = str(evaluator_spec.get("evaluator_id") or "").strip()
                    try:
                        raw_evaluator_result = execute_represented_evaluator(
                            scenario,
                            evaluator_spec,
                            trial_index,
                            observation,
                        )
                        evaluator_results[evaluator_id] = json_serialisable_projection(
                            raw_evaluator_result
                        )
                    except Exception as exc:
                        blockers.append(
                            _blocker(
                                code="represented_evaluator_execution_exception",
                                scenario_id=scenario_id,
                                trial_index=trial_index,
                                message="Represented evaluator execution raised an exception.",
                                recoverable=True,
                                details={
                                    "evaluator_id": evaluator_id,
                                    "error": str(exc),
                                },
                            )
                        )
            elif execute_represented_evaluator is None:
                blockers.append(
                    _blocker(
                        code="represented_evaluator_adapter_missing",
                        scenario_id=scenario_id,
                        trial_index=trial_index,
                        message="No represented evaluator execution adapter was supplied.",
                        recoverable=True,
                    )
                )

            observation["typed_blockers"] = blockers
            observation["represented_evaluator_results"] = evaluator_results
            safety_audits: dict[str, list[Any]] = {}
            for audit_key in (
                "fabricated_evidence",
                "forbidden_effects",
                "namespace_violations",
                "false_success_claims",
            ):
                audit_values: list[Any] = []
                source_keys = (
                    ("forbidden_effects", "forbidden_mutations")
                    if audit_key == "forbidden_effects"
                    else (audit_key,)
                )
                for source_key in source_keys:
                    raw_observed_values = observation.get(source_key)
                    if isinstance(raw_observed_values, Sequence) and not isinstance(
                        raw_observed_values,
                        (str, bytes, bytearray),
                    ):
                        audit_values.extend(raw_observed_values)
                    elif raw_observed_values not in (None, False, 0, ""):
                        audit_values.append(raw_observed_values)
                audit_complete = bool(evaluator_results)
                for evaluator_result in evaluator_results.values():
                    if (
                        not isinstance(evaluator_result, Mapping)
                        or audit_key not in evaluator_result
                    ):
                        audit_complete = False
                        break
                    raw_values = evaluator_result.get(audit_key)
                    if isinstance(raw_values, Sequence) and not isinstance(
                        raw_values,
                        (str, bytes, bytearray),
                    ):
                        audit_values.extend(raw_values)
                    elif raw_values not in (None, False, 0, ""):
                        audit_values.append(raw_values)
                if audit_complete:
                    safety_audits[audit_key] = audit_values
            observation["represented_safety_audits"] = safety_audits
            observation["observation_sha256"] = stable_payload_digest(observation)
            trial_result = evaluate_scenario_trial(
                contract,
                scenario_id=scenario_id,
                observation=observation,
            )
            passed_in_trial[scenario_id] = bool(trial_result["passed"])
            observations.append(observation)
            results.append(trial_result)

            if record_experiment_observation is not None:
                try:
                    persistence = record_experiment_observation(
                        build_strict_experiment_observation(
                            trial_result=trial_result,
                            observation=observation,
                            execution_provenance=execution_provenance,
                        )
                    )
                    persistence_results.append(
                        {
                            "scenario_id": scenario_id,
                            "trial_index": trial_index,
                            "success": bool(
                                isinstance(persistence, Mapping)
                                and persistence.get("success") is True
                            ),
                            "result": json_serialisable_projection(persistence),
                        }
                    )
                except Exception as exc:
                    persistence_results.append(
                        {
                            "scenario_id": scenario_id,
                            "trial_index": trial_index,
                            "success": False,
                            "error": str(exc),
                        }
                    )

    campaign_result = aggregate_five_trial_campaign(
        contract,
        results,
        represented_campaign_evidence=_represented_campaign_evidence(
            contract,
            results,
            represented_campaign_evidence=represented_campaign_evidence,
        ),
    )
    _apply_hard_campaign_gates(campaign_result, hard_campaign_gates)
    expected_trial_record_count = len(contract.scenarios) * 5
    _apply_experiment_persistence_gate(
        campaign_result,
        persistence_results,
        expected_record_count=expected_trial_record_count,
    )
    campaign_execution_provenance = json_serialisable_projection(
        execution_provenance or {}
    )

    def _build_current_runner_attestation() -> dict[str, Any]:
        from .operational_certification_attestation_service import (
            build_operational_certification_runner_attestation,
        )

        return build_operational_certification_runner_attestation(
            experiment_run_id=str(
                campaign_execution_provenance.get("experiment_run_id") or ""
            ),
            campaign_execution_id=str(
                campaign_execution_provenance.get("campaign_execution_id") or ""
            ),
            namespace=str(
                campaign_execution_provenance.get("effective_namespace") or ""
            ),
            user_id=str(campaign_execution_provenance.get("effective_user_id") or ""),
            org_id=str(campaign_execution_provenance.get("effective_org_id") or ""),
            report_sha256=str(campaign_result.get("report_sha256") or ""),
            contract_sha256=str(campaign_result.get("contract_sha256") or ""),
        )

    try:
        # First prove that the signing authority is available, then make that
        # fact an explicit hard gate and sign the resulting final report hash.
        # A missing secret must never leave a campaign certifiable merely
        # because attestation was treated as optional provenance.
        _build_current_runner_attestation()
        _apply_hard_campaign_gates(
            campaign_result,
            (
                {
                    "gate_id": "trusted_runner_attestation_available",
                    "passed": True,
                    "blocker_code": "trusted_runner_attestation_unavailable",
                    "message": "The certification report has a trusted runner signature.",
                    "recoverable": True,
                    "details": {
                        "attestation_schema_version": "operational_certification_runner_attestation.v1"
                    },
                },
            ),
        )
        campaign_execution_provenance["trusted_runner_attestation"] = (
            _build_current_runner_attestation()
        )
        campaign_execution_provenance["trusted_runner_attestation_status"] = "signed"
    except Exception as exc:
        _apply_hard_campaign_gates(
            campaign_result,
            (
                {
                    "gate_id": "trusted_runner_attestation_available",
                    "passed": False,
                    "blocker_code": "trusted_runner_attestation_unavailable",
                    "message": (
                        "The certification report could not be signed by the "
                        "trusted runner authority."
                    ),
                    "recoverable": True,
                    "details": {"error_type": type(exc).__name__},
                },
            ),
        )
        campaign_execution_provenance.pop("trusted_runner_attestation", None)
        campaign_execution_provenance["trusted_runner_attestation_status"] = (
            "unavailable"
        )
        campaign_execution_provenance["trusted_runner_attestation_error"] = type(
            exc
        ).__name__
    if record_experiment_observation is not None:
        try:
            campaign_persistence = record_experiment_observation(
                build_campaign_experiment_observation(
                    campaign_result,
                    execution_provenance=campaign_execution_provenance,
                )
            )
            persistence_results.append(
                {
                    "scope": "campaign",
                    "success": bool(
                        isinstance(campaign_persistence, Mapping)
                        and campaign_persistence.get("success") is True
                    ),
                    "result": json_serialisable_projection(campaign_persistence),
                }
            )
        except Exception as exc:
            persistence_results.append(
                {"scope": "campaign", "success": False, "error": str(exc)}
            )
        if persistence_results[-1].get("success") is not True:
            _apply_hard_campaign_gates(
                campaign_result,
                (
                    {
                        "gate_id": "campaign_report_persistence_complete",
                        "passed": False,
                        "blocker_code": "certification_campaign_persistence_failed",
                        "message": (
                            "The final certification campaign report did not "
                            "receive an explicit durable persistence receipt."
                        ),
                        "recoverable": True,
                        "details": {
                            "scope": "campaign",
                            "persistence_result": persistence_results[-1],
                        },
                    },
                ),
            )
            if (
                campaign_execution_provenance.get("trusted_runner_attestation_status")
                == "signed"
            ):
                try:
                    campaign_execution_provenance["trusted_runner_attestation"] = (
                        _build_current_runner_attestation()
                    )
                except Exception as exc:
                    campaign_execution_provenance.pop(
                        "trusted_runner_attestation", None
                    )
                    campaign_execution_provenance[
                        "trusted_runner_attestation_status"
                    ] = "unavailable"
                    campaign_execution_provenance[
                        "trusted_runner_attestation_error"
                    ] = type(exc).__name__

    execution = {
        "schema_version": OPERATIONAL_CERTIFICATION_EXECUTION_SCHEMA_VERSION,
        "generated_at_utc": _utcnow_iso(),
        "contract": contract.to_projection(),
        "trial_observations": observations,
        "trial_results": results,
        "campaign_result": campaign_result,
        "experiment_persistence": persistence_results,
        "execution_provenance": campaign_execution_provenance,
    }
    execution["execution_sha256"] = stable_payload_digest(execution)
    return execution


__all__ = [
    "OPERATIONAL_CERTIFICATION_EXECUTION_SCHEMA_VERSION",
    "STRICT_CERTIFICATION_EXPERIMENT_OBSERVATION_SCHEMA_VERSION",
    "build_campaign_experiment_observation",
    "build_strict_experiment_observation",
    "run_operational_certification_campaign",
    "validate_campaign_experiment_observation",
]
