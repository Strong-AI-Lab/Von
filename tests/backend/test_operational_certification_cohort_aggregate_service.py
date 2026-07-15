from __future__ import annotations

import copy
from typing import Any

import pytest

from src.backend.services.operational_certification_attestation_service import (
    build_operational_certification_runner_attestation,
)
from src.backend.services.operational_certification_cohort_aggregate_service import (
    OperationalCertificationCohortAggregateError,
    build_operational_certification_actor_campaign_dossier,
    build_operational_certification_cohort_aggregate,
)
from src.backend.services.operational_certification_cohort_vontology_service import (
    OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID,
    persist_operational_certification_cohort_aggregate,
)
from src.backend.services.operational_certification_contract_service import (
    CertificationScenarioContract,
    OperationalCertificationContract,
    stable_payload_digest,
)
from src.backend.services.operational_certification_runner_service import (
    build_campaign_experiment_observation,
)


ACTOR_ALPHA = "#V#pilot_alpha"
ACTOR_BETA = "#V#pilot_beta"
ORG_ID = "#V#represented_pilot_org"
TEST_SIGNING_KEY = "unit-test-certification-key-" + "a" * 64
TEST_SIGNING_KEY_ID = "unit-test-cohort-key"


@pytest.fixture(autouse=True)
def _trusted_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY",
        TEST_SIGNING_KEY,
    )
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID",
        TEST_SIGNING_KEY_ID,
    )


def _contract() -> OperationalCertificationContract:
    scenario = CertificationScenarioContract(
        scenario_id="represented_case",
        family_id="represented_family",
        depends_on=(),
        execution={},
        evaluator_specs=(),
        checks=(),
        minefields=(),
        budgets=(),
        initial_state={},
        reset_policy={},
        acceptable_goal_states=(),
        milestone_dag=(),
        permitted_effects=(),
        target_scope={},
        security_scope={},
        fault_injection={},
        metadata={},
        contract_sha256=stable_payload_digest({"scenario": "represented_case"}),
    )
    policy = {
        "schema_version": "operational_certification_policy.v1",
        "trial_count": 5,
        "pass_windows": [1, 3, 5],
        "pilot_cohort": {
            "status": "agreed",
            "profile": "represented_test_profile",
            "organisation_concept_id": ORG_ID,
            "actor_concept_ids": [ACTOR_ALPHA, ACTOR_BETA],
            "aggregation_policy": (
                "all_actors_must_independently_satisfy_all_certification_gates"
            ),
        },
        "certification_gates": [
            {
                "gate_id": "represented_policy_gate",
                "matcher": {
                    "matcher_id": "represented_policy_gate_matcher",
                    "kind": "exact",
                    "path": "/represented_policy_gate",
                    "expected": True,
                },
            }
        ],
    }
    contract_basis = {
        "suite_id": "represented_pilot_suite",
        "suite_concept_id": "#V#represented_pilot_suite",
        "case_set": "represented_pilot_case_set",
        "policy": policy,
        "scenario_contract_sha256s": {scenario.scenario_id: scenario.contract_sha256},
        "topological_scenario_ids": [scenario.scenario_id],
    }
    return OperationalCertificationContract(
        suite_id="represented_pilot_suite",
        suite_concept_id="#V#represented_pilot_suite",
        case_set="represented_pilot_case_set",
        policy=policy,
        scenarios=(scenario,),
        topological_scenario_ids=(scenario.scenario_id,),
        source="vontology",
        source_definition_sha256=stable_payload_digest(
            {"represented_suite_revision": 1}
        ),
        contract_sha256=stable_payload_digest(contract_basis),
    )


def _gate(gate_id: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "gate_id": gate_id,
        "kind": "hard_interface",
        "passed": True,
        "check": {"verified": True},
    }
    row["result_sha256"] = stable_payload_digest(row)
    return row


def _campaign(contract: OperationalCertificationContract) -> dict[str, Any]:
    gate_ids = [
        "represented_policy_gate",
        "live_release_evidence_eligible",
        "runtime_authority_alignment_verified",
        "pilot_actor_cohort_membership_verified",
        "experiment_persistence_complete",
        "trusted_runner_attestation_available",
    ]
    report: dict[str, Any] = {
        "schema_version": "operational_certification_campaign_result.v1",
        "suite_id": contract.suite_id,
        "suite_concept_id": contract.suite_concept_id,
        "case_set": contract.case_set,
        "suite_source": contract.source,
        "contract_sha256": contract.contract_sha256,
        "source_definition_sha256": contract.source_definition_sha256,
        "certification_gate_results": [_gate(gate_id) for gate_id in gate_ids],
        "failed_certification_gate_ids": [],
        "blockers": [],
        "experiment_persistence_complete": True,
        "certified": True,
    }
    report["report_sha256"] = stable_payload_digest(report)
    return report


def _execution(
    contract: OperationalCertificationContract,
    actor_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    namespace = f"{actor_id}@represented_pilot_org"
    campaign_execution_id = f"campaign-{actor_id.removeprefix('#V#')}"
    experiment_run_id = f"#V#experiment-{actor_id.removeprefix('#V#')}"
    campaign = _campaign(contract)
    cohort = contract.policy["pilot_cohort"]
    provenance: dict[str, Any] = {
        "schema_version": "operational_certification_execution_provenance.v1",
        "campaign_execution_id": campaign_execution_id,
        "experiment_run_id": experiment_run_id,
        "effective_user_id": actor_id,
        "effective_org_id": ORG_ID,
        "effective_namespace": namespace,
        "authenticated": True,
        "suite_concept_id": contract.suite_concept_id,
        "suite_source": contract.source,
        "source_definition_sha256": contract.source_definition_sha256,
        "contract_sha256": contract.contract_sha256,
        "policy_sha256": stable_payload_digest(contract.policy),
        "pilot_cohort_membership": {
            "applicable": True,
            "verified": True,
            "profile": cohort["profile"],
            "effective_user_id": actor_id,
            "effective_org_id": ORG_ID,
            "expected_organisation_concept_id": ORG_ID,
            "actor_concept_ids": cohort["actor_concept_ids"],
            "actor_count": 2,
            "aggregation_policy": cohort["aggregation_policy"],
            "aggregation_required": True,
        },
    }
    provenance["trusted_runner_attestation"] = (
        build_operational_certification_runner_attestation(
            experiment_run_id=experiment_run_id,
            campaign_execution_id=campaign_execution_id,
            namespace=namespace,
            user_id=actor_id,
            org_id=ORG_ID,
            report_sha256=campaign["report_sha256"],
            contract_sha256=contract.contract_sha256,
        )
    )
    provenance["trusted_runner_attestation_status"] = "signed"
    campaign_observation = build_campaign_experiment_observation(
        campaign,
        execution_provenance=provenance,
    )
    canonical_run: dict[str, Any] = {
        "run_id": experiment_run_id,
        "experiment_spec_id": f"#V#spec-{actor_id.removeprefix('#V#')}",
        "namespace": namespace,
        "user_id": actor_id,
        "org_id": ORG_ID,
        "status": "completed",
        "verdict": "pass",
        "metadata": {
            "suite_concept_id": contract.suite_concept_id,
            "contract_sha256": contract.contract_sha256,
            "source_definition_sha256": contract.source_definition_sha256,
        },
        "observations": [
            *(
                {
                    "observation_type": "operational_certification_trial",
                    "observation_id": f"trial-{index}",
                }
                for index in range(1, 6)
            ),
            campaign_observation,
        ],
    }
    finalisation_checks = {
        "verdict_receipt_success": True,
        "canonical_readback_available": True,
        "run_id_exact": True,
        "experiment_spec_id_exact": True,
        "namespace_exact": True,
        "user_id_exact": True,
        "org_id_exact": True,
        "terminal_status": True,
        "verdict_exact": True,
        "observation_count_exact": True,
        "run_metadata_contract_exact": True,
        "canonical_observations_exact": True,
    }
    execution: dict[str, Any] = {
        "schema_version": "operational_certification_execution.v1",
        "contract": contract.to_projection(),
        "trial_observations": [],
        "trial_results": [],
        "campaign_result": campaign,
        "experiment_persistence": [
            *(
                {
                    "scenario_id": "represented_case",
                    "trial_index": index,
                    "success": True,
                }
                for index in range(1, 6)
            ),
            {"scope": "campaign", "success": True},
        ],
        "execution_provenance": provenance,
        "campaign_execution_id": campaign_execution_id,
        "experiment_run_id": experiment_run_id,
        "experiment_finalisation": {
            "schema_version": ("operational_certification_experiment_finalisation.v1"),
            "success": True,
            "checks": finalisation_checks,
            "expected_observation_count": 6,
            "observed_observation_count": 6,
            "status": "completed",
            "verdict": "pass",
            "canonical_observation_validation": {"success": True},
            "canonical_run_state_sha256": stable_payload_digest(canonical_run),
        },
        "release_eligibility": {
            "scope": "actor_campaign",
            "eligible": True,
            "suite_source": contract.source,
            "authenticated": True,
            "effective_user_id": actor_id,
            "effective_org_id": ORG_ID,
            "effective_namespace": namespace,
            "pilot_cohort_membership_verified": True,
            "cohort_aggregate_required_for_v1": True,
            "cohort_aggregate_verified": False,
            "experiment_finalisation_success": True,
            "experiment_status": "completed",
            "experiment_verdict": "pass",
            "runtime_authority_alignment_verified": True,
        },
    }
    execution["execution_sha256"] = stable_payload_digest(execution)
    return execution, canonical_run


def _fixture_campaigns() -> tuple[
    OperationalCertificationContract,
    dict[str, Any],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    contract = _contract()
    alpha, alpha_run = _execution(contract, ACTOR_ALPHA)
    beta, beta_run = _execution(contract, ACTOR_BETA)
    return (
        contract,
        alpha,
        beta,
        {
            alpha_run["run_id"]: alpha_run,
            beta_run["run_id"]: beta_run,
        },
    )


def test_cohort_aggregate_is_policy_driven_complete_and_order_independent() -> None:
    contract, alpha, beta, runs = _fixture_campaigns()
    loader = runs.get

    forward = build_operational_certification_cohort_aggregate(
        contract=contract,
        actor_campaign_executions=[alpha, beta],
        experiment_run_loader=loader,
    )
    reverse = build_operational_certification_cohort_aggregate(
        contract=contract,
        actor_campaign_executions=[beta, alpha],
        experiment_run_loader=loader,
    )

    assert forward == reverse
    assert forward["certified"] is True
    assert forward["all_actors_certified"] is True
    assert forward["cohort"]["actor_concept_ids"] == [ACTOR_ALPHA, ACTOR_BETA]
    assert set(forward["campaign_report_sha256_by_actor"]) == {
        ACTOR_ALPHA,
        ACTOR_BETA,
    }
    assert len(set(forward["campaign_report_sha256_by_actor"].values())) == 1
    assert [
        row["actor_concept_id"] for row in forward["actor_campaign_dossiers"]
    ] == sorted([ACTOR_ALPHA, ACTOR_BETA])


def test_actor_dossier_binds_canonical_experiment_readback() -> None:
    contract, alpha, _beta, runs = _fixture_campaigns()

    dossier = build_operational_certification_actor_campaign_dossier(
        contract=contract,
        execution=alpha,
        experiment_run_loader=runs.get,
    )

    assert dossier["actor_concept_id"] == ACTOR_ALPHA
    assert dossier["certified"] is True
    assert dossier["experiment_persistence_complete"] is True
    assert dossier["experiment_finalisation_complete"] is True
    assert dossier["canonical_experiment_run_sha256"] == stable_payload_digest(
        runs[alpha["experiment_run_id"]]
    )


@pytest.mark.parametrize(
    ("selector", "expected_details"),
    [
        (
            lambda alpha, beta: [alpha],
            {"missing_actor_concept_ids": [ACTOR_BETA]},
        ),
        (
            lambda alpha, beta: [alpha, alpha],
            {
                "duplicate_actor_concept_ids": [ACTOR_ALPHA],
                "missing_actor_concept_ids": [ACTOR_BETA],
            },
        ),
    ],
)
def test_cohort_aggregate_rejects_missing_or_duplicate_actor_campaigns(
    selector: Any,
    expected_details: dict[str, Any],
) -> None:
    contract, alpha, beta, runs = _fixture_campaigns()

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_campaign_set_mismatch",
    ) as raised:
        build_operational_certification_cohort_aggregate(
            contract=contract,
            actor_campaign_executions=selector(alpha, beta),
            experiment_run_loader=runs.get,
        )

    for key, value in expected_details.items():
        assert raised.value.details[key] == value
    assert raised.value.recovery_affordances == [
        {
            "action_type": "supply_exact_declared_actor_campaign_set",
            "declared_actor_concept_ids": [ACTOR_ALPHA, ACTOR_BETA],
        }
    ]


def test_cohort_aggregate_rejects_an_extra_undeclared_actor() -> None:
    contract, alpha, beta, runs = _fixture_campaigns()
    extra, extra_run = _execution(contract, "#V#undeclared_actor")
    runs[extra_run["run_id"]] = extra_run

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_not_declared",
    ):
        build_operational_certification_cohort_aggregate(
            contract=contract,
            actor_campaign_executions=[alpha, beta, extra],
            experiment_run_loader=runs.get,
        )


def test_actor_dossier_rejects_execution_digest_tampering() -> None:
    contract, alpha, _beta, runs = _fixture_campaigns()
    alpha["release_eligibility"]["eligible"] = False

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_execution_digest_mismatch",
    ):
        build_operational_certification_actor_campaign_dossier(
            contract=contract,
            execution=alpha,
            experiment_run_loader=runs.get,
        )


def test_actor_dossier_rejects_forged_runner_attestation() -> None:
    contract, alpha, _beta, runs = _fixture_campaigns()
    alpha["execution_provenance"]["trusted_runner_attestation"]["signature"] = "0" * 64
    canonical = runs[alpha["experiment_run_id"]]
    campaign_observation = canonical["observations"][-1]
    campaign_observation["execution_provenance"] = copy.deepcopy(
        alpha["execution_provenance"]
    )
    alpha["experiment_finalisation"]["canonical_run_state_sha256"] = (
        stable_payload_digest(canonical)
    )
    alpha["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in alpha.items() if key != "execution_sha256"}
    )

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_trusted_attestation_invalid",
    ):
        build_operational_certification_actor_campaign_dossier(
            contract=contract,
            execution=alpha,
            experiment_run_loader=runs.get,
        )


def test_cohort_aggregate_rejects_reused_experiment_spec_across_actors() -> None:
    contract, alpha, beta, runs = _fixture_campaigns()
    alpha_spec_id = runs[alpha["experiment_run_id"]]["experiment_spec_id"]
    beta_run = runs[beta["experiment_run_id"]]
    beta_run["experiment_spec_id"] = alpha_spec_id
    beta["experiment_finalisation"]["canonical_run_state_sha256"] = (
        stable_payload_digest(beta_run)
    )
    beta["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in beta.items() if key != "execution_sha256"}
    )

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_campaign_execution_identity_reused",
    ) as raised:
        build_operational_certification_cohort_aggregate(
            contract=contract,
            actor_campaign_executions=[alpha, beta],
            experiment_run_loader=runs.get,
        )

    assert raised.value.details["reused_identity_values"] == {
        "experiment_spec_id": [alpha_spec_id]
    }


def test_actor_dossier_rejects_cross_actor_experiment_run_reuse() -> None:
    contract, alpha, beta, runs = _fixture_campaigns()
    reused_run_id = alpha["experiment_run_id"]
    beta["experiment_run_id"] = reused_run_id
    beta["execution_provenance"]["experiment_run_id"] = reused_run_id
    beta["execution_provenance"]["trusted_runner_attestation"] = (
        build_operational_certification_runner_attestation(
            experiment_run_id=reused_run_id,
            campaign_execution_id=beta["campaign_execution_id"],
            namespace=beta["execution_provenance"]["effective_namespace"],
            user_id=ACTOR_BETA,
            org_id=ORG_ID,
            report_sha256=beta["campaign_result"]["report_sha256"],
            contract_sha256=contract.contract_sha256,
        )
    )
    beta["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in beta.items() if key != "execution_sha256"}
    )

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_canonical_experiment_run_mismatch",
    ):
        build_operational_certification_actor_campaign_dossier(
            contract=contract,
            execution=beta,
            experiment_run_loader=runs.get,
        )


def test_actor_dossier_rejects_noncanonical_experiment_state() -> None:
    contract, alpha, _beta, runs = _fixture_campaigns()
    runs[alpha["experiment_run_id"]]["status"] = "running"

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_canonical_experiment_run_mismatch",
    ):
        build_operational_certification_actor_campaign_dossier(
            contract=contract,
            execution=alpha,
            experiment_run_loader=runs.get,
        )


def test_actor_dossier_rejects_incomplete_campaign_persistence() -> None:
    contract, alpha, _beta, runs = _fixture_campaigns()
    alpha["experiment_persistence"][-1]["success"] = False
    alpha["execution_sha256"] = stable_payload_digest(
        {key: value for key, value in alpha.items() if key != "execution_sha256"}
    )

    with pytest.raises(
        OperationalCertificationCohortAggregateError,
        match="cohort_actor_experiment_persistence_incomplete",
    ):
        build_operational_certification_actor_campaign_dossier(
            contract=contract,
            execution=alpha,
            experiment_run_loader=runs.get,
        )


def test_persist_cohort_aggregate_creates_org_scoped_immutable_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import (
        operational_certification_cohort_vontology_service as service,
    )

    contract, alpha, beta, runs = _fixture_campaigns()
    concepts: dict[str, dict[str, Any]] = {}
    texts: dict[str, str] = {}
    writes: list[dict[str, Any]] = []

    def fake_get_concept(concept_id: str) -> dict[str, Any] | None:
        return copy.deepcopy(concepts.get(concept_id))

    def fake_create_concept(**kwargs: Any) -> dict[str, Any]:
        concept_id = kwargs["concept_id"]
        concept = {
            "concept_id": concept_id,
            "attributes": copy.deepcopy(kwargs.get("attributes") or {}),
        }
        concepts[concept_id] = concept
        return copy.deepcopy(concept)

    def fake_get_texts(concept_id: str, **_kwargs: Any) -> list[dict[str, str]]:
        return [{"text": texts[concept_id]}] if concept_id in texts else []

    def fake_upsert(**kwargs: Any) -> dict[str, Any]:
        texts[kwargs["subject_concept_id"]] = kwargs["text"]
        writes.append(copy.deepcopy(kwargs))
        return {"success": True}

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        fake_get_concept,
    )
    monkeypatch.setattr(service.concept_service, "create_concept", fake_create_concept)
    monkeypatch.setattr(
        service.concept_service,
        "acquire_concept_mutation_lease",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(
        service.concept_service,
        "release_concept_mutation_lease",
        lambda **_kwargs: {"success": True},
    )
    monkeypatch.setattr(service, "get_texts_for_concept", fake_get_texts)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", fake_upsert)

    first = persist_operational_certification_cohort_aggregate(
        contract=contract,
        actor_campaign_executions=[alpha, beta],
        experiment_run_loader=runs.get,
    )

    # Model process shutdown followed by injection from the same durable host
    # credential item. The idempotent path must cryptographically verify the
    # persisted actor attestations rather than trusting aggregate digests alone.
    monkeypatch.delenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY")
    monkeypatch.delenv("VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID")
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY",
        TEST_SIGNING_KEY,
    )
    monkeypatch.setenv(
        "VON_OPERATIONAL_CERTIFICATION_SIGNING_KEY_ID",
        TEST_SIGNING_KEY_ID,
    )
    second = persist_operational_certification_cohort_aggregate(
        contract=contract,
        actor_campaign_executions=[beta, alpha],
        experiment_run_loader=runs.get,
    )

    assert first["success"] is True
    assert first["idempotent"] is False
    assert second["success"] is True
    assert second["idempotent"] is True
    assert first["aggregate"] == second["aggregate"]
    assert len(writes) == 1
    assert OPERATIONAL_CERTIFICATION_COHORT_AGGREGATE_TYPE_ID in concepts
    aggregate_concept = concepts[first["aggregate_concept_id"]]
    identity = aggregate_concept["attributes"][
        "operational_certification_cohort_aggregate_identity"
    ]
    assert identity["organisation_concept_id"] == ORG_ID
    assert identity["actor_concept_ids"] == [ACTOR_ALPHA, ACTOR_BETA]
