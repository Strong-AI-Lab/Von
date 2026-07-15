"""Immutable cohort aggregation for actor-scoped certification campaigns.

The represented certification suite owns cohort membership and aggregation
policy.  This module supplies only a hard evidence boundary: it validates one
trusted, persisted campaign execution per declared actor and constructs a
deterministic all-actors aggregate.  Canonical persistence and read-back live
in ``operational_certification_cohort_vontology_service``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
from typing import Any

from .namespace_service import (
    derive_actor_context_from_namespace,
    derive_namespace_for_actor,
    resolve_canonical_namespace,
)
from .operational_certification_contract_service import (
    OPERATIONAL_CERTIFICATION_CONTRACT_PROJECTION_SCHEMA_VERSION,
    OperationalCertificationContract,
    json_serialisable_projection,
    stable_payload_digest,
    validate_operational_certification_campaign_result_integrity,
)
from .operational_certification_runner_service import (
    OPERATIONAL_CERTIFICATION_EXECUTION_SCHEMA_VERSION,
    validate_campaign_experiment_observation,
)


OPERATIONAL_CERTIFICATION_ACTOR_CAMPAIGN_DOSSIER_SCHEMA_VERSION = (
    "operational_certification_actor_campaign_dossier.v1"
)
OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_SCHEMA_VERSION = (
    "operational_certification_cohort_aggregate.v1"
)
_SUPPORTED_AGGREGATION_POLICY = (
    "all_actors_must_independently_satisfy_all_certification_gates"
)
_REQUIRED_HARD_GATE_IDS = frozenset(
    {
        "live_release_evidence_eligible",
        "runtime_authority_alignment_verified",
        "pilot_actor_cohort_membership_verified",
        "experiment_persistence_complete",
        "trusted_runner_attestation_available",
    }
)
_REQUIRED_FINALISATION_CHECK_IDS = frozenset(
    {
        "verdict_receipt_success",
        "canonical_readback_available",
        "run_id_exact",
        "experiment_spec_id_exact",
        "namespace_exact",
        "user_id_exact",
        "org_id_exact",
        "terminal_status",
        "verdict_exact",
        "observation_count_exact",
        "run_metadata_contract_exact",
        "canonical_observations_exact",
    }
)
_ACTOR_DOSSIER_FIELDS = frozenset(
    {
        "schema_version",
        "actor_concept_id",
        "organisation_concept_id",
        "effective_namespace",
        "suite_id",
        "suite_concept_id",
        "case_set",
        "suite_source",
        "source_definition_sha256",
        "contract_sha256",
        "policy_sha256",
        "cohort_policy_sha256",
        "campaign_execution_id",
        "experiment_run_id",
        "experiment_spec_id",
        "campaign_report_sha256",
        "execution_sha256",
        "canonical_experiment_run_sha256",
        "experiment_observation_count",
        "trusted_runner_attestation",
        "trusted_runner_attestation_sha256",
        "certified",
        "experiment_persistence_complete",
        "experiment_finalisation_complete",
        "dossier_sha256",
    }
)
_COHORT_AGGREGATE_FIELDS = frozenset(
    {
        "schema_version",
        "authority",
        "cohort",
        "actor_count",
        "actor_campaign_dossiers",
        "campaign_report_sha256_by_actor",
        "actor_dossier_sha256_by_actor",
        "all_actors_certified",
        "certified",
        "aggregate_identity_sha256",
        "aggregate_sha256",
    }
)
ExperimentRunLoader = Callable[[str], Mapping[str, Any] | None]


class OperationalCertificationCohortAggregateError(ValueError):
    """Typed failure at the immutable cohort-evidence boundary."""

    def __init__(
        self,
        code: str,
        *,
        details: Mapping[str, Any] | None = None,
        recovery_affordances: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.code = code
        self.details = copy.deepcopy(dict(details or {}))
        self.recovery_affordances = [
            copy.deepcopy(dict(item)) for item in recovery_affordances
        ]
        super().__init__(code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ("operational_certification_cohort_aggregate_error.v1"),
            "error_code": self.code,
            "details": copy.deepcopy(self.details),
            "recovery_affordances": copy.deepcopy(self.recovery_affordances),
        }


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _require_sha256(value: Any, *, code: str) -> str:
    if not _is_sha256(value):
        raise OperationalCertificationCohortAggregateError(code)
    return str(value).lower()


def _mapping(value: Any, *, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationalCertificationCohortAggregateError(code)
    try:
        return json_serialisable_projection(value)
    except TypeError as exc:
        raise OperationalCertificationCohortAggregateError(code) from exc


def _mapping_sequence(value: Any, *, code: str) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise OperationalCertificationCohortAggregateError(code)
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise OperationalCertificationCohortAggregateError(code)
        rows.append(_mapping(item, code=code))
    return rows


def _contract_projection(
    contract: OperationalCertificationContract,
) -> dict[str, Any]:
    projection = _mapping(
        contract.to_projection(),
        code="cohort_contract_projection_invalid",
    )
    if projection.get("schema_version") != (
        OPERATIONAL_CERTIFICATION_CONTRACT_PROJECTION_SCHEMA_VERSION
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_contract_projection_invalid"
        )
    scenario_rows = _mapping_sequence(
        projection.get("scenarios"),
        code="cohort_contract_scenarios_invalid",
    )
    scenario_hashes: dict[str, str] = {}
    for scenario in scenario_rows:
        scenario_id = _clean(scenario.get("scenario_id"))
        scenario_sha256 = _require_sha256(
            scenario.get("contract_sha256"),
            code="cohort_scenario_contract_digest_invalid",
        )
        if not scenario_id or scenario_id in scenario_hashes:
            raise OperationalCertificationCohortAggregateError(
                "cohort_contract_scenarios_invalid"
            )
        scenario_hashes[scenario_id] = scenario_sha256
    contract_basis = {
        "suite_id": projection.get("suite_id"),
        "suite_concept_id": projection.get("suite_concept_id"),
        "case_set": projection.get("case_set"),
        "policy": projection.get("policy"),
        "scenario_contract_sha256s": scenario_hashes,
        "topological_scenario_ids": projection.get("topological_scenario_ids"),
    }
    expected_contract_sha256 = stable_payload_digest(contract_basis)
    if projection.get("contract_sha256") != expected_contract_sha256:
        raise OperationalCertificationCohortAggregateError(
            "cohort_contract_digest_mismatch"
        )
    for field_name in ("suite_id", "suite_concept_id", "case_set", "source"):
        if not _clean(projection.get(field_name)):
            raise OperationalCertificationCohortAggregateError(
                "cohort_contract_identity_incomplete",
                details={"field": field_name},
            )
    _require_sha256(
        projection.get("source_definition_sha256"),
        code="cohort_source_definition_digest_invalid",
    )
    return projection


def _represented_cohort(
    contract_projection: Mapping[str, Any],
) -> dict[str, Any]:
    policy = _mapping(
        contract_projection.get("policy"),
        code="represented_pilot_cohort_policy_missing",
    )
    raw_cohort = _mapping(
        policy.get("pilot_cohort"),
        code="represented_pilot_cohort_policy_missing",
    )
    raw_actor_ids = raw_cohort.get("actor_concept_ids")
    if not isinstance(raw_actor_ids, Sequence) or isinstance(
        raw_actor_ids,
        (str, bytes, bytearray),
    ):
        raise OperationalCertificationCohortAggregateError(
            "represented_pilot_cohort_actor_ids_invalid"
        )
    actor_ids = [_clean(item) for item in raw_actor_ids]
    org_id = _clean(raw_cohort.get("organisation_concept_id"))
    aggregation_policy = _clean(raw_cohort.get("aggregation_policy"))
    if (
        raw_cohort.get("status") != "agreed"
        or not actor_ids
        or any(not actor_id.startswith("#V#") for actor_id in actor_ids)
        or len(set(actor_ids)) != len(actor_ids)
        or not org_id.startswith("#V#")
        or aggregation_policy != _SUPPORTED_AGGREGATION_POLICY
    ):
        raise OperationalCertificationCohortAggregateError(
            "represented_pilot_cohort_policy_invalid"
        )
    cohort = {
        "status": "agreed",
        "profile": _clean(raw_cohort.get("profile")) or None,
        "organisation_concept_id": org_id,
        "actor_concept_ids": actor_ids,
        "aggregation_policy": aggregation_policy,
    }
    cohort["cohort_policy_sha256"] = stable_payload_digest(raw_cohort)
    return cohort


def _expected_scope(*, actor_id: str, org_id: str) -> dict[str, str]:
    namespace = derive_namespace_for_actor(actor_id, org_id)
    canonical = resolve_canonical_namespace(namespace, actor_id, org_id)
    namespace_actor, namespace_org = derive_actor_context_from_namespace(namespace)
    if (
        not namespace
        or canonical != namespace
        or namespace_actor != actor_id
        or namespace_org != org_id
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_scope_canonicalisation_failed",
            details={"actor_concept_id": actor_id, "organisation_concept_id": org_id},
        )
    return {
        "effective_namespace": namespace,
        "effective_user_id": actor_id,
        "effective_org_id": org_id,
    }


def _require_exact_fields(
    observed: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    code: str,
) -> None:
    mismatches = [
        field_name
        for field_name, expected_value in expected.items()
        if observed.get(field_name) != expected_value
    ]
    if mismatches:
        raise OperationalCertificationCohortAggregateError(
            code,
            details={"mismatched_fields": sorted(mismatches)},
        )


def _required_policy_gate_ids(contract_projection: Mapping[str, Any]) -> set[str]:
    policy = _mapping(
        contract_projection.get("policy"),
        code="cohort_contract_policy_invalid",
    )
    raw_gates = policy.get("certification_gates")
    gates = _mapping_sequence(
        raw_gates,
        code="cohort_contract_certification_gates_invalid",
    )
    gate_ids: set[str] = set()
    for gate in gates:
        gate_id = _clean(gate.get("gate_id"))
        if not gate_id or gate_id in gate_ids:
            raise OperationalCertificationCohortAggregateError(
                "cohort_contract_certification_gates_invalid"
            )
        gate_ids.add(gate_id)
    return gate_ids | set(_REQUIRED_HARD_GATE_IDS)


def _validate_campaign(
    campaign: Mapping[str, Any],
    *,
    contract_projection: Mapping[str, Any],
) -> None:
    integrity_errors = validate_operational_certification_campaign_result_integrity(
        campaign
    )
    if integrity_errors:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_campaign_integrity_invalid",
            details={"integrity_errors": integrity_errors},
        )
    _require_exact_fields(
        campaign,
        {
            "suite_id": contract_projection.get("suite_id"),
            "suite_concept_id": contract_projection.get("suite_concept_id"),
            "case_set": contract_projection.get("case_set"),
            "suite_source": contract_projection.get("source"),
            "contract_sha256": contract_projection.get("contract_sha256"),
            "source_definition_sha256": contract_projection.get(
                "source_definition_sha256"
            ),
        },
        code="cohort_actor_campaign_contract_mismatch",
    )
    if (
        campaign.get("certified") is not True
        or campaign.get("experiment_persistence_complete") is not True
        or campaign.get("failed_certification_gate_ids") not in ([], ())
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_campaign_not_certified"
        )
    gate_rows = _mapping_sequence(
        campaign.get("certification_gate_results"),
        code="cohort_actor_campaign_gate_results_invalid",
    )
    gate_results = {_clean(row.get("gate_id")): row.get("passed") for row in gate_rows}
    required_gate_ids = _required_policy_gate_ids(contract_projection)
    missing_or_failed = sorted(
        gate_id
        for gate_id in required_gate_ids
        if gate_results.get(gate_id) is not True
    )
    if missing_or_failed:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_campaign_required_gate_missing_or_failed",
            details={"gate_ids": missing_or_failed},
        )


def _load_experiment_run(
    experiment_run_id: str,
    loader: ExperimentRunLoader | None,
) -> Mapping[str, Any] | None:
    if loader is not None:
        return loader(experiment_run_id)
    from .experiment_run_service import get_experiment_run_state

    return get_experiment_run_state(experiment_run_id)


def _validate_canonical_experiment_run(
    *,
    experiment_run_id: str,
    execution_provenance: Mapping[str, Any],
    campaign: Mapping[str, Any],
    finalisation: Mapping[str, Any],
    expected_scope: Mapping[str, str],
    contract_projection: Mapping[str, Any],
    loader: ExperimentRunLoader | None,
) -> dict[str, Any]:
    canonical = _load_experiment_run(experiment_run_id, loader)
    if not isinstance(canonical, Mapping):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_canonical_experiment_run_not_found",
            details={"experiment_run_id": experiment_run_id},
            recovery_affordances=(
                {
                    "action_type": "read_or_rerun_actor_certification_experiment",
                    "experiment_run_id": experiment_run_id,
                },
            ),
        )
    canonical_projection = _mapping(
        canonical,
        code="cohort_actor_canonical_experiment_run_invalid",
    )
    experiment_spec_id = _clean(canonical_projection.get("experiment_spec_id"))
    if not experiment_spec_id:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_experiment_spec_id_missing"
        )
    _require_exact_fields(
        canonical_projection,
        {
            "run_id": experiment_run_id,
            "namespace": expected_scope["effective_namespace"],
            "user_id": expected_scope["effective_user_id"],
            "org_id": expected_scope["effective_org_id"],
            "status": "completed",
            "verdict": "pass",
        },
        code="cohort_actor_canonical_experiment_run_mismatch",
    )
    metadata = _mapping(
        canonical_projection.get("metadata"),
        code="cohort_actor_experiment_metadata_invalid",
    )
    _require_exact_fields(
        metadata,
        {
            "suite_concept_id": contract_projection.get("suite_concept_id"),
            "contract_sha256": contract_projection.get("contract_sha256"),
            "source_definition_sha256": contract_projection.get(
                "source_definition_sha256"
            ),
        },
        code="cohort_actor_experiment_metadata_mismatch",
    )
    observations = _mapping_sequence(
        canonical_projection.get("observations"),
        code="cohort_actor_experiment_observations_invalid",
    )
    matching_campaign_observations: list[dict[str, Any]] = []
    for observation in observations:
        if observation.get("observation_type") != "operational_certification_campaign":
            continue
        evidence = observation.get("evidence")
        persisted_campaign = (
            evidence.get("operational_certification_campaign_result")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(persisted_campaign, Mapping):
            continue
        if persisted_campaign.get("report_sha256") != campaign.get("report_sha256"):
            continue
        if validate_campaign_experiment_observation(observation):
            raise OperationalCertificationCohortAggregateError(
                "cohort_actor_persisted_campaign_observation_invalid"
            )
        if stable_payload_digest(persisted_campaign) != stable_payload_digest(campaign):
            raise OperationalCertificationCohortAggregateError(
                "cohort_actor_persisted_campaign_mismatch"
            )
        if observation.get("execution_provenance") != execution_provenance:
            raise OperationalCertificationCohortAggregateError(
                "cohort_actor_persisted_campaign_provenance_mismatch"
            )
        matching_campaign_observations.append(observation)
    if len(matching_campaign_observations) != 1:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_persisted_campaign_observation_not_unique",
            details={"matching_observation_count": len(matching_campaign_observations)},
        )
    expected_observation_count = len(contract_projection.get("scenarios") or []) * 5 + 1
    if (
        len(observations) != expected_observation_count
        or finalisation.get("expected_observation_count") != expected_observation_count
        or finalisation.get("observed_observation_count") != expected_observation_count
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_experiment_observation_count_mismatch"
        )
    canonical_sha256 = stable_payload_digest(canonical_projection)
    if finalisation.get("canonical_run_state_sha256") != canonical_sha256:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_canonical_experiment_digest_mismatch"
        )
    return {
        "canonical_run_state_sha256": canonical_sha256,
        "observation_count": expected_observation_count,
        "experiment_spec_id": experiment_spec_id,
    }


def build_operational_certification_actor_campaign_dossier(
    *,
    contract: OperationalCertificationContract,
    execution: Mapping[str, Any],
    experiment_run_loader: ExperimentRunLoader | None = None,
) -> dict[str, Any]:
    """Validate and normalise one immutable actor-level campaign dossier."""

    contract_projection = _contract_projection(contract)
    cohort = _represented_cohort(contract_projection)
    execution_projection = _mapping(
        execution,
        code="cohort_actor_execution_invalid",
    )
    if execution_projection.get("schema_version") != (
        OPERATIONAL_CERTIFICATION_EXECUTION_SCHEMA_VERSION
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_execution_schema_invalid"
        )
    observed_execution_sha256 = _require_sha256(
        execution_projection.get("execution_sha256"),
        code="cohort_actor_execution_digest_invalid",
    )
    execution_without_digest = {
        key: value
        for key, value in execution_projection.items()
        if key != "execution_sha256"
    }
    if observed_execution_sha256 != stable_payload_digest(execution_without_digest):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_execution_digest_mismatch"
        )
    if execution_projection.get("contract") != contract_projection:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_execution_contract_mismatch"
        )

    campaign = _mapping(
        execution_projection.get("campaign_result"),
        code="cohort_actor_campaign_missing",
    )
    _validate_campaign(campaign, contract_projection=contract_projection)
    provenance = _mapping(
        execution_projection.get("execution_provenance"),
        code="cohort_actor_execution_provenance_missing",
    )
    actor_id = _clean(provenance.get("effective_user_id"))
    org_id = cohort["organisation_concept_id"]
    if actor_id not in cohort["actor_concept_ids"]:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_not_declared",
            details={"actor_concept_id": actor_id or None},
            recovery_affordances=(
                {
                    "action_type": "supply_declared_actor_campaign",
                    "declared_actor_concept_ids": cohort["actor_concept_ids"],
                },
            ),
        )
    expected_scope = _expected_scope(actor_id=actor_id, org_id=org_id)
    _require_exact_fields(
        provenance,
        {
            **expected_scope,
            "authenticated": True,
            "suite_concept_id": contract_projection.get("suite_concept_id"),
            "suite_source": contract_projection.get("source"),
            "source_definition_sha256": contract_projection.get(
                "source_definition_sha256"
            ),
            "contract_sha256": contract_projection.get("contract_sha256"),
            "policy_sha256": stable_payload_digest(contract_projection["policy"]),
        },
        code="cohort_actor_execution_provenance_mismatch",
    )
    membership = _mapping(
        provenance.get("pilot_cohort_membership"),
        code="cohort_actor_membership_evidence_missing",
    )
    _require_exact_fields(
        membership,
        {
            "applicable": True,
            "verified": True,
            "effective_user_id": actor_id,
            "effective_org_id": org_id,
            "expected_organisation_concept_id": org_id,
            "actor_concept_ids": cohort["actor_concept_ids"],
            "actor_count": len(cohort["actor_concept_ids"]),
            "aggregation_policy": cohort["aggregation_policy"],
            "aggregation_required": True,
        },
        code="cohort_actor_membership_evidence_mismatch",
    )
    campaign_execution_id = _clean(provenance.get("campaign_execution_id"))
    experiment_run_id = _clean(provenance.get("experiment_run_id"))
    if (
        not campaign_execution_id
        or execution_projection.get("campaign_execution_id") != campaign_execution_id
        or not experiment_run_id
        or execution_projection.get("experiment_run_id") != experiment_run_id
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_execution_identity_mismatch"
        )

    persistence_rows = _mapping_sequence(
        execution_projection.get("experiment_persistence"),
        code="cohort_actor_experiment_persistence_invalid",
    )
    campaign_persistence_rows = [
        row for row in persistence_rows if row.get("scope") == "campaign"
    ]
    expected_trial_persistence_count = len(contract_projection["scenarios"]) * 5
    trial_persistence_rows = [
        row for row in persistence_rows if row.get("scope") != "campaign"
    ]
    if (
        len(campaign_persistence_rows) != 1
        or campaign_persistence_rows[0].get("success") is not True
        or len(trial_persistence_rows) != expected_trial_persistence_count
        or any(row.get("success") is not True for row in persistence_rows)
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_experiment_persistence_incomplete"
        )

    finalisation = _mapping(
        execution_projection.get("experiment_finalisation"),
        code="cohort_actor_experiment_finalisation_missing",
    )
    checks = _mapping(
        finalisation.get("checks"),
        code="cohort_actor_experiment_finalisation_checks_missing",
    )
    missing_or_failed_checks = sorted(
        check_id
        for check_id in _REQUIRED_FINALISATION_CHECK_IDS
        if checks.get(check_id) is not True
    )
    if (
        finalisation.get("schema_version")
        != "operational_certification_experiment_finalisation.v1"
        or finalisation.get("success") is not True
        or finalisation.get("status") != "completed"
        or finalisation.get("verdict") != "pass"
        or missing_or_failed_checks
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_experiment_finalisation_incomplete",
            details={"missing_or_failed_checks": missing_or_failed_checks},
        )
    canonical_run = _validate_canonical_experiment_run(
        experiment_run_id=experiment_run_id,
        execution_provenance=provenance,
        campaign=campaign,
        finalisation=finalisation,
        expected_scope=expected_scope,
        contract_projection=contract_projection,
        loader=experiment_run_loader,
    )

    release_eligibility = _mapping(
        execution_projection.get("release_eligibility"),
        code="cohort_actor_release_eligibility_missing",
    )
    _require_exact_fields(
        release_eligibility,
        {
            "scope": "actor_campaign",
            "eligible": True,
            "suite_source": contract_projection.get("source"),
            "authenticated": True,
            **expected_scope,
            "pilot_cohort_membership_verified": True,
            "cohort_aggregate_required_for_v1": True,
            "cohort_aggregate_verified": False,
            "experiment_finalisation_success": True,
            "experiment_status": "completed",
            "experiment_verdict": "pass",
            "runtime_authority_alignment_verified": True,
        },
        code="cohort_actor_release_eligibility_mismatch",
    )

    from .operational_certification_attestation_service import (
        verify_operational_certification_runner_attestation,
    )

    raw_attestation = provenance.get("trusted_runner_attestation")
    try:
        verified_attestation = verify_operational_certification_runner_attestation(
            raw_attestation,
            expected={
                "experiment_run_id": experiment_run_id,
                "campaign_execution_id": campaign_execution_id,
                "namespace": expected_scope["effective_namespace"],
                "user_id": actor_id,
                "org_id": org_id,
                "report_sha256": campaign.get("report_sha256"),
                "contract_sha256": contract_projection.get("contract_sha256"),
            },
        )
    except (RuntimeError, ValueError) as exc:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_trusted_attestation_invalid",
            details={"reason": str(exc)},
            recovery_affordances=(
                {"action_type": "rerun_trusted_actor_certification_campaign"},
            ),
        ) from exc
    attestation = _mapping(
        raw_attestation,
        code="cohort_actor_trusted_attestation_invalid",
    )
    if verified_attestation.get("signature_verified") is not True:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_trusted_attestation_invalid"
        )

    dossier: dict[str, Any] = {
        "schema_version": (
            OPERATIONAL_CERTIFICATION_ACTOR_CAMPAIGN_DOSSIER_SCHEMA_VERSION
        ),
        "actor_concept_id": actor_id,
        "organisation_concept_id": org_id,
        "effective_namespace": expected_scope["effective_namespace"],
        "suite_id": contract_projection.get("suite_id"),
        "suite_concept_id": contract_projection.get("suite_concept_id"),
        "case_set": contract_projection.get("case_set"),
        "suite_source": contract_projection.get("source"),
        "source_definition_sha256": contract_projection.get("source_definition_sha256"),
        "contract_sha256": contract_projection.get("contract_sha256"),
        "policy_sha256": stable_payload_digest(contract_projection["policy"]),
        "cohort_policy_sha256": cohort["cohort_policy_sha256"],
        "campaign_execution_id": campaign_execution_id,
        "experiment_run_id": experiment_run_id,
        "experiment_spec_id": canonical_run["experiment_spec_id"],
        "campaign_report_sha256": campaign.get("report_sha256"),
        "execution_sha256": observed_execution_sha256,
        "canonical_experiment_run_sha256": canonical_run["canonical_run_state_sha256"],
        "experiment_observation_count": canonical_run["observation_count"],
        "trusted_runner_attestation": attestation,
        "trusted_runner_attestation_sha256": stable_payload_digest(attestation),
        "certified": True,
        "experiment_persistence_complete": True,
        "experiment_finalisation_complete": True,
    }
    dossier["dossier_sha256"] = stable_payload_digest(dossier)
    return dossier


def _validate_actor_dossier(
    dossier: Mapping[str, Any],
    *,
    contract_projection: Mapping[str, Any],
    cohort: Mapping[str, Any],
) -> dict[str, Any]:
    projection = _mapping(dossier, code="cohort_actor_dossier_invalid")
    if set(projection) != _ACTOR_DOSSIER_FIELDS:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_dossier_fields_invalid",
            details={
                "missing_fields": sorted(_ACTOR_DOSSIER_FIELDS - set(projection)),
                "extra_fields": sorted(set(projection) - _ACTOR_DOSSIER_FIELDS),
            },
        )
    observed_digest = _require_sha256(
        projection.get("dossier_sha256"),
        code="cohort_actor_dossier_digest_invalid",
    )
    without_digest = {
        key: value for key, value in projection.items() if key != "dossier_sha256"
    }
    if observed_digest != stable_payload_digest(without_digest):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_dossier_digest_mismatch"
        )
    actor_id = _clean(projection.get("actor_concept_id"))
    org_id = _clean(projection.get("organisation_concept_id"))
    if (
        actor_id not in cohort["actor_concept_ids"]
        or org_id != cohort["organisation_concept_id"]
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_dossier_scope_mismatch"
        )
    scope = _expected_scope(actor_id=actor_id, org_id=org_id)
    _require_exact_fields(
        projection,
        {
            "schema_version": (
                OPERATIONAL_CERTIFICATION_ACTOR_CAMPAIGN_DOSSIER_SCHEMA_VERSION
            ),
            "effective_namespace": scope["effective_namespace"],
            "suite_id": contract_projection.get("suite_id"),
            "suite_concept_id": contract_projection.get("suite_concept_id"),
            "case_set": contract_projection.get("case_set"),
            "suite_source": contract_projection.get("source"),
            "source_definition_sha256": contract_projection.get(
                "source_definition_sha256"
            ),
            "contract_sha256": contract_projection.get("contract_sha256"),
            "policy_sha256": stable_payload_digest(contract_projection["policy"]),
            "cohort_policy_sha256": cohort["cohort_policy_sha256"],
            "certified": True,
            "experiment_persistence_complete": True,
            "experiment_finalisation_complete": True,
        },
        code="cohort_actor_dossier_authority_mismatch",
    )
    for field_name in (
        "source_definition_sha256",
        "contract_sha256",
        "policy_sha256",
        "cohort_policy_sha256",
        "campaign_report_sha256",
        "execution_sha256",
        "canonical_experiment_run_sha256",
        "trusted_runner_attestation_sha256",
    ):
        _require_sha256(
            projection.get(field_name),
            code="cohort_actor_dossier_digest_invalid",
        )
    for field_name in (
        "campaign_execution_id",
        "experiment_run_id",
        "experiment_spec_id",
    ):
        if not _clean(projection.get(field_name)):
            raise OperationalCertificationCohortAggregateError(
                "cohort_actor_dossier_execution_identity_invalid",
                details={"field": field_name},
            )
    attestation = _mapping(
        projection.get("trusted_runner_attestation"),
        code="cohort_actor_trusted_attestation_invalid",
    )
    if stable_payload_digest(attestation) != projection.get(
        "trusted_runner_attestation_sha256"
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_trusted_attestation_digest_mismatch"
        )
    from .operational_certification_attestation_service import (
        verify_operational_certification_runner_attestation,
    )

    try:
        verify_operational_certification_runner_attestation(
            attestation,
            expected={
                "experiment_run_id": projection.get("experiment_run_id"),
                "campaign_execution_id": projection.get("campaign_execution_id"),
                "namespace": scope["effective_namespace"],
                "user_id": actor_id,
                "org_id": org_id,
                "report_sha256": projection.get("campaign_report_sha256"),
                "contract_sha256": projection.get("contract_sha256"),
            },
        )
    except (RuntimeError, ValueError) as exc:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_trusted_attestation_invalid",
            details={"reason": str(exc)},
            recovery_affordances=(
                {"action_type": "rerun_trusted_actor_certification_campaign"},
            ),
        ) from exc
    return projection


def build_operational_certification_cohort_aggregate(
    *,
    contract: OperationalCertificationContract,
    actor_campaign_executions: Sequence[Mapping[str, Any]],
    experiment_run_loader: ExperimentRunLoader | None = None,
) -> dict[str, Any]:
    """Build a deterministic aggregate only after every declared actor passes."""

    contract_projection = _contract_projection(contract)
    cohort = _represented_cohort(contract_projection)
    if not isinstance(actor_campaign_executions, Sequence) or isinstance(
        actor_campaign_executions,
        (str, bytes, bytearray),
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_campaign_sequence_invalid"
        )
    dossiers: list[dict[str, Any]] = []
    for execution in actor_campaign_executions:
        if not isinstance(execution, Mapping):
            raise OperationalCertificationCohortAggregateError(
                "cohort_actor_campaign_sequence_invalid"
            )
        dossiers.append(
            build_operational_certification_actor_campaign_dossier(
                contract=contract,
                execution=execution,
                experiment_run_loader=experiment_run_loader,
            )
        )
    observed_actor_ids = [dossier["actor_concept_id"] for dossier in dossiers]
    duplicate_actor_ids = sorted(
        {
            actor_id
            for actor_id in observed_actor_ids
            if observed_actor_ids.count(actor_id) > 1
        }
    )
    expected_actor_ids = list(cohort["actor_concept_ids"])
    missing_actor_ids = sorted(set(expected_actor_ids) - set(observed_actor_ids))
    extra_actor_ids = sorted(set(observed_actor_ids) - set(expected_actor_ids))
    if duplicate_actor_ids or missing_actor_ids or extra_actor_ids:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_campaign_set_mismatch",
            details={
                "duplicate_actor_concept_ids": duplicate_actor_ids,
                "missing_actor_concept_ids": missing_actor_ids,
                "extra_actor_concept_ids": extra_actor_ids,
            },
            recovery_affordances=(
                {
                    "action_type": "supply_exact_declared_actor_campaign_set",
                    "declared_actor_concept_ids": expected_actor_ids,
                },
            ),
        )
    reused_execution_id_fields: dict[str, list[str]] = {}
    for field_name in (
        "campaign_execution_id",
        "experiment_run_id",
        "experiment_spec_id",
    ):
        values = [str(dossier.get(field_name) or "") for dossier in dossiers]
        reused = sorted(
            {value for value in values if value and values.count(value) > 1}
        )
        if reused:
            reused_execution_id_fields[field_name] = reused
    if reused_execution_id_fields:
        raise OperationalCertificationCohortAggregateError(
            "cohort_actor_campaign_execution_identity_reused",
            details={"reused_identity_values": reused_execution_id_fields},
            recovery_affordances=(
                {"action_type": "rerun_actor_campaigns_with_distinct_experiments"},
            ),
        )
    dossiers.sort(key=lambda item: item["actor_concept_id"])
    campaign_digests = {
        dossier["actor_concept_id"]: dossier["campaign_report_sha256"]
        for dossier in dossiers
    }
    dossier_digests = {
        dossier["actor_concept_id"]: dossier["dossier_sha256"] for dossier in dossiers
    }
    authority = {
        "suite_id": contract_projection.get("suite_id"),
        "suite_concept_id": contract_projection.get("suite_concept_id"),
        "case_set": contract_projection.get("case_set"),
        "suite_source": contract_projection.get("source"),
        "source_definition_sha256": contract_projection.get("source_definition_sha256"),
        "contract_sha256": contract_projection.get("contract_sha256"),
        "policy_sha256": stable_payload_digest(contract_projection["policy"]),
    }
    identity_basis = {
        "authority": authority,
        "cohort_policy_sha256": cohort["cohort_policy_sha256"],
        "actor_dossier_sha256_by_actor": dossier_digests,
    }
    aggregate: dict[str, Any] = {
        "schema_version": OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_SCHEMA_VERSION,
        "authority": authority,
        "cohort": cohort,
        "actor_count": len(expected_actor_ids),
        "actor_campaign_dossiers": dossiers,
        "campaign_report_sha256_by_actor": campaign_digests,
        "actor_dossier_sha256_by_actor": dossier_digests,
        "all_actors_certified": True,
        "certified": True,
        "aggregate_identity_sha256": stable_payload_digest(identity_basis),
    }
    aggregate["aggregate_sha256"] = stable_payload_digest(aggregate)
    return aggregate


def operational_certification_cohort_aggregate_concept_id(
    aggregate: Mapping[str, Any],
) -> str:
    identity_sha256 = _require_sha256(
        aggregate.get("aggregate_identity_sha256"),
        code="cohort_aggregate_identity_digest_invalid",
    )
    return f"#V#operational_certification_cohort_aggregate_{identity_sha256[:32]}"


def validate_operational_certification_cohort_aggregate(
    aggregate: Mapping[str, Any],
    *,
    contract: OperationalCertificationContract,
) -> dict[str, Any]:
    projection = _mapping(aggregate, code="cohort_aggregate_invalid")
    if set(projection) != _COHORT_AGGREGATE_FIELDS:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_fields_invalid",
            details={
                "missing_fields": sorted(_COHORT_AGGREGATE_FIELDS - set(projection)),
                "extra_fields": sorted(set(projection) - _COHORT_AGGREGATE_FIELDS),
            },
        )
    if projection.get("schema_version") != (
        OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_SCHEMA_VERSION
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_schema_invalid"
        )
    observed_aggregate_sha256 = _require_sha256(
        projection.get("aggregate_sha256"),
        code="cohort_aggregate_digest_invalid",
    )
    aggregate_without_digest = {
        key: value for key, value in projection.items() if key != "aggregate_sha256"
    }
    if observed_aggregate_sha256 != stable_payload_digest(aggregate_without_digest):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_digest_mismatch"
        )
    authority = _mapping(
        projection.get("authority"),
        code="cohort_aggregate_authority_invalid",
    )
    cohort = _mapping(
        projection.get("cohort"),
        code="cohort_aggregate_policy_invalid",
    )
    raw_actor_ids = cohort.get("actor_concept_ids")
    if not isinstance(raw_actor_ids, list):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_actor_ids_invalid"
        )
    actor_ids = [_clean(item) for item in raw_actor_ids]
    if (
        not actor_ids
        or any(not actor_id for actor_id in actor_ids)
        or len(set(actor_ids)) != len(actor_ids)
        or cohort.get("aggregation_policy") != _SUPPORTED_AGGREGATION_POLICY
        or projection.get("actor_count") != len(actor_ids)
        or projection.get("all_actors_certified") is not True
        or projection.get("certified") is not True
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_policy_invalid"
        )
    contract_projection = _contract_projection(contract)
    expected_cohort = _represented_cohort(contract_projection)
    expected_authority = {
        "suite_id": contract_projection.get("suite_id"),
        "suite_concept_id": contract_projection.get("suite_concept_id"),
        "case_set": contract_projection.get("case_set"),
        "suite_source": contract_projection.get("source"),
        "source_definition_sha256": contract_projection.get("source_definition_sha256"),
        "contract_sha256": contract_projection.get("contract_sha256"),
        "policy_sha256": stable_payload_digest(contract_projection["policy"]),
    }
    if authority != expected_authority or cohort != expected_cohort:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_authority_mismatch"
        )
    dossiers = _mapping_sequence(
        projection.get("actor_campaign_dossiers"),
        code="cohort_aggregate_actor_dossiers_invalid",
    )
    observed_actor_ids = [
        _clean(dossier.get("actor_concept_id")) for dossier in dossiers
    ]
    if sorted(observed_actor_ids) != sorted(actor_ids):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_actor_dossier_set_mismatch"
        )
    validated_dossiers: list[dict[str, Any]] = []
    for dossier in dossiers:
        validated_dossiers.append(
            _validate_actor_dossier(
                dossier,
                contract_projection=contract_projection,
                cohort=cohort,
            )
        )
    campaign_digests = {
        dossier["actor_concept_id"]: dossier["campaign_report_sha256"]
        for dossier in validated_dossiers
    }
    dossier_digests = {
        dossier["actor_concept_id"]: dossier["dossier_sha256"]
        for dossier in validated_dossiers
    }
    if projection.get("campaign_report_sha256_by_actor") != campaign_digests:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_campaign_digest_projection_mismatch"
        )
    if projection.get("actor_dossier_sha256_by_actor") != dossier_digests:
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_dossier_digest_projection_mismatch"
        )
    identity_basis = {
        "authority": authority,
        "cohort_policy_sha256": cohort.get("cohort_policy_sha256"),
        "actor_dossier_sha256_by_actor": dossier_digests,
    }
    if projection.get("aggregate_identity_sha256") != stable_payload_digest(
        identity_basis
    ):
        raise OperationalCertificationCohortAggregateError(
            "cohort_aggregate_identity_digest_mismatch"
        )
    return projection


__all__ = [
    "OPERATIONAL_CERTIFICATION_ACTOR_CAMPAIGN_DOSSIER_SCHEMA_VERSION",
    "OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_SCHEMA_VERSION",
    "OperationalCertificationCohortAggregateError",
    "build_operational_certification_actor_campaign_dossier",
    "build_operational_certification_cohort_aggregate",
    "operational_certification_cohort_aggregate_concept_id",
    "validate_operational_certification_cohort_aggregate",
]
