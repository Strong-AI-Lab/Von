from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from typing import Any

import pytest

from src.backend.services.operational_learning_release_service import (
    AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION,
    HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION,
    LEARNING_RELEASE_EVALUATOR_DIGEST_ONLY_REFERENCE_SCHEMA_VERSION,
    LEARNING_RELEASE_EVALUATOR_EVIDENCE_PROJECTION_SCHEMA_VERSION,
    OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION,
    REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION,
    REPRESENTED_LEARNING_RELEASE_DECISION_SCHEMA_VERSION,
    LearningReleaseValidationError,
    build_failure_evidence_packets,
    build_learning_release_certification_evidence,
    build_learning_release_evaluator_evidence_projection,
    build_learning_release_experiment_evidence,
    operational_learning_release_digest,
    promote_operational_learning_release_candidate,
    register_operational_learning_release_candidate,
    reject_operational_learning_release_candidate,
    rollback_operational_learning_release,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
DECISION_TIME = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
ARTIFACT_ID = "#V#generic_policy_artifact"
NAMESPACE = "#V#user@org"
USER_ID = "#V#user"
ORG_ID = "#V#org"


def _authority(concept_id: str = "#V#represented_learning_workflow") -> dict[str, Any]:
    return {
        "schema_version": REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION,
        "authority_concept_id": concept_id,
        "authority_revision_sha256": operational_learning_release_digest(
            {"concept_id": concept_id, "revision": 7}
        ),
    }


def _failure_packets(
    *,
    artifact_id: str = ARTIFACT_ID,
    request_ids: tuple[str, ...] = ("request-1", "request-2"),
) -> list[dict[str, Any]]:
    return build_failure_evidence_packets(
        [
            {
                "schema_version": "terminal_outcome_receipt_projection.v1",
                "request_id": request_id,
                "causal_stage": "verification",
                "cause_code": "represented_evidence_incomplete",
                "affected_artifact": artifact_id,
                "outcome": "typed_non_success",
                "diagnostic_text": f"diagnostic unique to {request_id}",
            }
            for request_id in request_ids
        ],
        created_at=NOW,
    )


def _register_candidate(
    state: dict[str, Any],
    *,
    suffix: str,
    artifact_id: str = ARTIFACT_ID,
    risk_classes: tuple[str, ...] = ("quality",),
) -> dict[str, Any]:
    return register_operational_learning_release_candidate(
        state,
        candidate_id=f"candidate-{suffix}",
        release_id=f"release-{suffix}",
        affected_artifact=artifact_id,
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        release_payload={
            "represented_revision": suffix,
            "parameters": {"mode": "represented"},
        },
        failure_evidence_packets=_failure_packets(
            artifact_id=artifact_id,
            request_ids=(f"request-{suffix}-1", f"request-{suffix}-2"),
        ),
        proposal_authority=_authority("#V#episode_self_improvement_proposal_workflow"),
        risk_classes=risk_classes,
        expires_at="2027-01-01T00:00:00+00:00",
        retest_after="2026-12-01T00:00:00+00:00",
        retest_requirements={
            "experiment_suite_id": "#V#generic_regression_suite",
            "reason": "represented certification expiry",
        },
        created_at=NOW,
    )


def _experiment_run(
    candidate: dict[str, Any],
    *,
    verdict: str = "pass",
) -> dict[str, Any]:
    return {
        "schema_version": "experiment_run.v1",
        "run_id": f"#V#experiment_run_{verdict}",
        "experiment_spec_id": "#V#generic_learning_release_experiment_spec",
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "metadata": {
            "learning_release_candidate_id": candidate["candidate_id"],
            "learning_release_candidate_release_sha256": candidate["release_sha256"],
        },
        "status": "completed" if verdict != "fail" else "failed",
        "verdict": verdict,
        "observations": [
            {
                "schema_version": "strict_certification_experiment_observation.v1",
                "verdict": verdict,
                "evidence": {"request_id": "request-evidence-1"},
            }
        ],
        "evidence": {
            "turn_execution_request_ids": ["request-evidence-1"],
        },
    }


def _campaign_result(
    candidate: dict[str, Any],
    *,
    certified: bool = True,
) -> dict[str, Any]:
    represented_gate = {
        "gate_id": "represented_release_gate",
        "passed": certified,
    }
    represented_gate["result_sha256"] = operational_learning_release_digest(
        represented_gate
    )
    live_gate = {
        "gate_id": "live_release_evidence_eligible",
        "passed": True,
    }
    live_gate["result_sha256"] = operational_learning_release_digest(live_gate)
    persistence_gate = {
        "gate_id": "experiment_persistence_complete",
        "passed": True,
    }
    persistence_gate["result_sha256"] = operational_learning_release_digest(
        persistence_gate
    )
    report: dict[str, Any] = {
        "schema_version": "operational_certification_campaign_result.v1",
        "suite_id": "generic_operational_suite",
        "suite_concept_id": "#V#generic_operational_suite",
        "suite_source": "vontology",
        "contract_sha256": operational_learning_release_digest(
            {"suite": "generic_operational_suite", "revision": 3}
        ),
        "trial_count": 5,
        "certification_gate_results": [
            represented_gate,
            live_gate,
            persistence_gate,
        ],
        "failed_certification_gate_ids": (
            [] if certified else ["represented_release_gate"]
        ),
        "blockers": [],
        "experiment_persistence_required": True,
        "experiment_persistence_complete": True,
        "represented_campaign_evidence": {
            "evaluated_learning_release_candidate_bindings": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_release_sha256": candidate["release_sha256"],
                }
            ]
        },
        "certified": certified,
    }
    report["report_sha256"] = operational_learning_release_digest(report)
    return report


def _evidence(
    candidate: dict[str, Any],
    *,
    experiment_verdict: str = "pass",
    certified: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        build_learning_release_experiment_evidence(
            candidate=candidate,
            experiment_run=_experiment_run(
                candidate,
                verdict=experiment_verdict,
            ),
        ),
        build_learning_release_certification_evidence(
            candidate=candidate,
            campaign_result=_campaign_result(candidate, certified=certified),
            execution_provenance={
                "effective_namespace": NAMESPACE,
                "effective_user_id": USER_ID,
                "effective_org_id": ORG_ID,
            },
        ),
    )


def _decision(
    candidate: dict[str, Any],
    experiment: dict[str, Any],
    certification: dict[str, Any],
    *,
    action: str,
    suffix: str,
) -> dict[str, Any]:
    return {
        "schema_version": REPRESENTED_LEARNING_RELEASE_DECISION_SCHEMA_VERSION,
        "decision_id": f"decision-{action}-{suffix}",
        "decision": action,
        "candidate_id": candidate["candidate_id"],
        "candidate_release_sha256": candidate["release_sha256"],
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "authority": _authority("#V#episode_self_improvement_promotion_workflow"),
        "experiment_evidence_sha256": experiment["evidence_sha256"],
        "certification_evidence_sha256": certification["evidence_sha256"],
        "rationale": "Represented evaluator decision grounded in linked evidence.",
        "decided_at": DECISION_TIME.isoformat(),
    }


def _approval(
    candidate: dict[str, Any],
    *,
    action: str,
) -> dict[str, Any]:
    approval_id = f"human-approval-{action}-{candidate['candidate_id']}"
    authenticated_receipt = {
        "schema_version": AUTHENTICATED_HUMAN_APPROVAL_RECEIPT_SCHEMA_VERSION,
        "approval_id": approval_id,
        "approver_id": "#V#authorised_human_reviewer",
        "action": action,
        "candidate_id": candidate["candidate_id"],
        "candidate_release_sha256": candidate["release_sha256"],
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "approved_at": DECISION_TIME.isoformat(),
        "authenticated_at": DECISION_TIME.isoformat(),
    }
    authenticated_receipt["receipt_sha256"] = operational_learning_release_digest(
        authenticated_receipt
    )
    return {
        "schema_version": HUMAN_LEARNING_RELEASE_APPROVAL_SCHEMA_VERSION,
        "approval_id": approval_id,
        "approver_id": "#V#authorised_human_reviewer",
        "action": action,
        "candidate_id": candidate["candidate_id"],
        "candidate_release_sha256": candidate["release_sha256"],
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "approved": True,
        "approved_at": DECISION_TIME.isoformat(),
        "authenticated_approval_receipt": authenticated_receipt,
    }


def _promote(
    state: dict[str, Any],
    candidate: dict[str, Any],
    *,
    suffix: str,
    human_approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    experiment, certification = _evidence(candidate)
    return promote_operational_learning_release_candidate(
        state,
        candidate_id=candidate["candidate_id"],
        represented_evaluator_decision=_decision(
            candidate,
            experiment,
            certification,
            action="promote",
            suffix=suffix,
        ),
        experiment_evidence=experiment,
        certification_evidence=certification,
        human_approval=human_approval,
        recorded_at=DECISION_TIME,
    )


def test_failure_packets_group_only_on_represented_causal_dimensions() -> None:
    packets = build_failure_evidence_packets(
        [
            {
                "request_id": "request-a",
                "causal_stage": "selection",
                "cause_code": "represented_choice_unsupported",
                "affected_artifact": "#V#generic_artifact",
                "workflow_name": "first unrelated surface",
                "proposed_remedy": "first unrelated remedy",
            },
            {
                "request_id": "request-b",
                "causal_stage": "selection",
                "cause_code": "represented_choice_unsupported",
                "affected_artifact": "#V#generic_artifact",
                "workflow_name": "second unrelated surface",
                "proposed_remedy": "second unrelated remedy",
            },
            {
                "request_id": "request-c",
                "causal_stage": "verification",
                "cause_code": "represented_choice_unsupported",
                "affected_artifact": "#V#generic_artifact",
            },
        ],
        created_at=NOW,
    )

    assert len(packets) == 2
    selection_packet = next(
        packet for packet in packets if packet["causal_stage"] == "selection"
    )
    assert selection_packet["grouping_dimensions"] == [
        "causal_stage",
        "cause_code",
        "affected_artifact",
    ]
    assert selection_packet["member_request_ids"] == ["request-a", "request-b"]
    assert {member["request_id"] for member in selection_packet["members"]} == {
        "request-a",
        "request-b",
    }
    assert selection_packet["packet_sha256"] == operational_learning_release_digest(
        {
            key: value
            for key, value in selection_packet.items()
            if key != "packet_sha256"
        }
    )


def test_failure_packet_rejects_one_request_with_conflicting_causal_dimensions() -> (
    None
):
    with pytest.raises(
        LearningReleaseValidationError,
        match="failure_request_has_conflicting_causal_dimensions",
    ):
        build_failure_evidence_packets(
            [
                {
                    "request_id": "request-same",
                    "causal_stage": "selection",
                    "cause_code": "failure_a",
                    "affected_artifact": ARTIFACT_ID,
                },
                {
                    "request_id": "request-same",
                    "causal_stage": "verification",
                    "cause_code": "failure_b",
                    "affected_artifact": ARTIFACT_ID,
                },
            ],
            created_at=NOW,
        )


def test_candidate_snapshot_and_release_provenance_are_immutable() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="immutable")
    stored_before = copy.deepcopy(
        state["candidate_snapshots"][candidate["candidate_id"]]
    )

    candidate["release_payload"]["parameters"]["mode"] = "tampered-return-value"

    assert state["candidate_snapshots"]["candidate-immutable"] == stored_before
    assert stored_before["release_sha256"] == operational_learning_release_digest(
        {
            key: stored_before[key]
            for key in (
                "candidate_id",
                "release_id",
                "affected_artifact",
                "namespace",
                "user_id",
                "org_id",
                "release_payload",
                "parent_release",
                "failure_evidence_packets",
                "proposal_authority",
                "risk_classes",
                "expires_at",
                "retest_after",
                "retest_requirements",
                "created_at",
            )
        }
    )
    assert stored_before["member_request_ids"] == [
        "request-immutable-1",
        "request-immutable-2",
    ]


def test_register_is_idempotent_but_rejects_candidate_id_reuse() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="same")

    same = _register_candidate(state, suffix="same")
    assert same["candidate_sha256"] == candidate["candidate_sha256"]

    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_candidate_id_conflict",
    ):
        register_operational_learning_release_candidate(
            state,
            candidate_id="candidate-same",
            release_id="release-same",
            affected_artifact=ARTIFACT_ID,
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            release_payload={"different": True},
            failure_evidence_packets=_failure_packets(
                request_ids=("request-same-1", "request-same-2")
            ),
            proposal_authority=_authority(
                "#V#episode_self_improvement_proposal_workflow"
            ),
            risk_classes=("quality",),
            expires_at="2027-01-01T00:00:00+00:00",
            retest_after="2026-12-01T00:00:00+00:00",
            retest_requirements={"suite": "#V#generic_regression_suite"},
            created_at=NOW,
        )


def test_missing_proposal_authority_fails_closed_without_initialising_state() -> None:
    state: dict[str, Any] = {}

    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_proposal_authority_required",
    ):
        register_operational_learning_release_candidate(
            state,
            candidate_id="candidate-no-authority",
            release_id="release-no-authority",
            affected_artifact=ARTIFACT_ID,
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            release_payload={"revision": "candidate"},
            failure_evidence_packets=_failure_packets(),
            proposal_authority={},
            expires_at="2027-01-01T00:00:00+00:00",
            retest_after="2026-12-01T00:00:00+00:00",
            retest_requirements={"suite": "#V#generic_regression_suite"},
            created_at=NOW,
        )

    assert state == {}


def test_certification_candidate_id_without_exact_release_hash_fails_closed() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="exact-certification-binding")
    campaign = _campaign_result(candidate)
    campaign["represented_campaign_evidence"][
        "evaluated_learning_release_candidate_bindings"
    ][0]["candidate_release_sha256"] = ("f" * 64)
    campaign.pop("report_sha256")
    campaign["report_sha256"] = operational_learning_release_digest(campaign)

    with pytest.raises(
        LearningReleaseValidationError,
        match="certification_campaign_candidate_binding_missing",
    ):
        build_learning_release_certification_evidence(
            candidate=candidate,
            campaign_result=campaign,
            execution_provenance={
                "effective_namespace": NAMESPACE,
                "effective_user_id": USER_ID,
                "effective_org_id": ORG_ID,
            },
        )


def test_promote_updates_active_previous_and_retest_pointers_with_receipt() -> None:
    state: dict[str, Any] = {}
    first = _register_candidate(state, suffix="first")
    first_receipt = _promote(state, first, suffix="first")
    second = _register_candidate(state, suffix="second")
    second_receipt = _promote(state, second, suffix="second")

    pointers = state["release_pointers"][ARTIFACT_ID]
    assert pointers["active"]["release_sha256"] == second["release_sha256"]
    assert pointers["previous"]["release_sha256"] == first["release_sha256"]
    assert pointers["active"]["expires_at"] == second["expires_at"]
    assert pointers["active"]["retest_after"] == second["retest_after"]
    assert pointers["active"]["retest_requirements"] == second["retest_requirements"]
    assert first_receipt["schema_version"] == (
        OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION
    )
    assert second_receipt["outcome"] == "promoted"
    assert second_receipt["active_pointer_changed"] is True
    assert state["candidate_states"]["candidate-first"]["state"] == "superseded"
    assert state["candidate_states"]["candidate-second"]["state"] == "active"


def test_promotion_requires_positive_experiment_and_certification_evidence() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="gated")

    failing_experiment, passing_certification = _evidence(
        candidate,
        experiment_verdict="fail",
    )
    with pytest.raises(
        LearningReleaseValidationError,
        match="promotion_requires_passing_experiment_evidence",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=_decision(
                candidate,
                failing_experiment,
                passing_certification,
                action="promote",
                suffix="failed-experiment",
            ),
            experiment_evidence=failing_experiment,
            certification_evidence=passing_certification,
            recorded_at=DECISION_TIME,
        )
    assert ARTIFACT_ID not in state["release_pointers"]

    passing_experiment, failing_certification = _evidence(
        candidate,
        certified=False,
    )
    with pytest.raises(
        LearningReleaseValidationError,
        match="promotion_requires_certified_campaign_evidence",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=_decision(
                candidate,
                passing_experiment,
                failing_certification,
                action="promote",
                suffix="failed-certification",
            ),
            experiment_evidence=passing_experiment,
            certification_evidence=failing_certification,
            recorded_at=DECISION_TIME,
        )
    assert state["candidate_states"][candidate["candidate_id"]]["state"] == ("proposed")


@pytest.mark.parametrize("risk_class", ["security", "destructive", "privilege"])
def test_high_risk_promotion_requires_bound_human_approval(risk_class: str) -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(
        state,
        suffix=risk_class,
        risk_classes=(risk_class,),
    )
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix=risk_class,
    )
    state_before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="human_approval_required_for_high_risk_release",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )
    assert state == state_before

    receipt = promote_operational_learning_release_candidate(
        state,
        candidate_id=candidate["candidate_id"],
        represented_evaluator_decision=decision,
        experiment_evidence=experiment,
        certification_evidence=certification,
        human_approval=_approval(candidate, action="promote"),
        recorded_at=DECISION_TIME,
    )
    assert receipt["human_approval_id"]


def test_missing_or_substituted_evaluator_authority_fails_closed() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="authority")
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix="authority",
    )
    decision.pop("authority")
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="represented_evaluator_authority_required",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )

    assert state == before


def test_tampered_candidate_or_evidence_fails_closed() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="tamper")
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix="tamper",
    )
    certification["campaign_result"]["trial_count"] = 1
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="certification_report_hash_mismatch",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )
    assert state == before

    state["candidate_snapshots"][candidate["candidate_id"]]["release_payload"] = {
        "tampered": True
    }
    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_hash_mismatch",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=_evidence(candidate)[1],
            recorded_at=DECISION_TIME,
        )


def test_rejection_retains_active_and_previous_pointers_exactly() -> None:
    state: dict[str, Any] = {}
    active = _register_candidate(state, suffix="active")
    _promote(state, active, suffix="active")
    rejected = _register_candidate(
        state,
        suffix="rejected",
        risk_classes=("security",),
    )
    pointer_before = copy.deepcopy(state["release_pointers"][ARTIFACT_ID])
    experiment, certification = _evidence(
        rejected,
        experiment_verdict="fail",
        certified=False,
    )

    receipt = reject_operational_learning_release_candidate(
        state,
        candidate_id=rejected["candidate_id"],
        represented_evaluator_decision=_decision(
            rejected,
            experiment,
            certification,
            action="reject",
            suffix="rejected",
        ),
        experiment_evidence=experiment,
        certification_evidence=certification,
        recorded_at=DECISION_TIME,
    )

    assert receipt["outcome"] == "rejected"
    assert receipt["active_pointer_changed"] is False
    assert state["release_pointers"][ARTIFACT_ID] == pointer_before
    assert state["candidate_states"][rejected["candidate_id"]]["state"] == ("rejected")


def test_rollback_restores_exact_previous_hash_and_marks_release_rolled_back() -> None:
    state: dict[str, Any] = {}
    first = _register_candidate(state, suffix="rollback-first")
    _promote(state, first, suffix="rollback-first")
    second = _register_candidate(state, suffix="rollback-second")
    _promote(state, second, suffix="rollback-second")
    before = copy.deepcopy(state["release_pointers"][ARTIFACT_ID])
    experiment, certification = _evidence(
        second,
        experiment_verdict="fail",
        certified=False,
    )

    receipt = rollback_operational_learning_release(
        state,
        affected_artifact=ARTIFACT_ID,
        represented_evaluator_decision=_decision(
            second,
            experiment,
            certification,
            action="rollback",
            suffix="rollback-second",
        ),
        experiment_evidence=experiment,
        certification_evidence=certification,
        recorded_at=DECISION_TIME,
    )

    pointers = state["release_pointers"][ARTIFACT_ID]
    assert pointers["active"] == before["previous"]
    assert pointers["active"]["release_sha256"] == first["release_sha256"]
    assert pointers["previous"] == before["active"]
    assert receipt["state"] == "rolled_back"
    assert receipt["resulting_active"]["release_sha256"] == (first["release_sha256"])
    assert state["candidate_states"][second["candidate_id"]]["state"] == ("rolled_back")
    assert state["release_states"][second["release_sha256"]]["state"] == ("rolled_back")
    assert state["candidate_states"][first["candidate_id"]]["state"] == "active"


def test_high_risk_rollback_requires_human_approval() -> None:
    state: dict[str, Any] = {}
    first = _register_candidate(state, suffix="safe-base")
    _promote(state, first, suffix="safe-base")
    risky = _register_candidate(
        state,
        suffix="risky-active",
        risk_classes=("privilege",),
    )
    _promote(
        state,
        risky,
        suffix="risky-active",
        human_approval=_approval(risky, action="promote"),
    )
    experiment, certification = _evidence(
        risky,
        experiment_verdict="fail",
        certified=False,
    )
    decision = _decision(
        risky,
        experiment,
        certification,
        action="rollback",
        suffix="risky-active",
    )
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="human_approval_required_for_high_risk_release",
    ):
        rollback_operational_learning_release(
            state,
            affected_artifact=ARTIFACT_ID,
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )
    assert state == before

    receipt = rollback_operational_learning_release(
        state,
        affected_artifact=ARTIFACT_ID,
        represented_evaluator_decision=decision,
        experiment_evidence=experiment,
        certification_evidence=certification,
        human_approval=_approval(risky, action="rollback"),
        recorded_at=DECISION_TIME,
    )
    assert receipt["outcome"] == "rolled_back"


def test_decision_receipts_are_idempotent() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="idempotent")
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix="idempotent",
    )
    first = promote_operational_learning_release_candidate(
        state,
        candidate_id=candidate["candidate_id"],
        represented_evaluator_decision=decision,
        experiment_evidence=experiment,
        certification_evidence=certification,
        recorded_at=DECISION_TIME,
    )
    second = promote_operational_learning_release_candidate(
        state,
        candidate_id=candidate["candidate_id"],
        represented_evaluator_decision=decision,
        experiment_evidence=experiment,
        certification_evidence=certification,
        recorded_at=DECISION_TIME,
    )

    assert second == first
    assert len(state["decision_receipts"]) == 1


def test_candidate_due_for_retest_cannot_be_promoted() -> None:
    state: dict[str, Any] = {}
    candidate = register_operational_learning_release_candidate(
        state,
        candidate_id="candidate-stale",
        release_id="release-stale",
        affected_artifact=ARTIFACT_ID,
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        release_payload={"revision": "stale"},
        failure_evidence_packets=_failure_packets(),
        proposal_authority=_authority(),
        risk_classes=("quality",),
        expires_at="2026-08-01T00:00:00+00:00",
        retest_after="2026-07-13T00:00:00+00:00",
        retest_requirements={"suite": "#V#generic_regression_suite"},
        created_at=NOW,
    )
    experiment, certification = _evidence(candidate)

    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_candidate_retest_due",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=_decision(
                candidate,
                experiment,
                certification,
                action="promote",
                suffix="stale",
            ),
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )


def test_failure_packet_rejects_duplicate_request_with_different_source_evidence() -> (
    None
):
    with pytest.raises(
        LearningReleaseValidationError,
        match="failure_request_has_conflicting_source_evidence",
    ):
        build_failure_evidence_packets(
            [
                {
                    "request_id": "request-duplicate",
                    "causal_stage": "verification",
                    "cause_code": "represented_evidence_incomplete",
                    "affected_artifact": ARTIFACT_ID,
                    "diagnostic_text": "first represented source",
                },
                {
                    "request_id": "request-duplicate",
                    "causal_stage": "verification",
                    "cause_code": "represented_evidence_incomplete",
                    "affected_artifact": ARTIFACT_ID,
                    "diagnostic_text": "conflicting represented source",
                },
            ],
            created_at=NOW,
        )


@pytest.mark.parametrize(
    ("field_name", "foreign_value"),
    [
        ("namespace", "#V#other_user@other_org"),
        ("user_id", "#V#other_user"),
        ("org_id", "#V#other_org"),
    ],
)
def test_experiment_evidence_rejects_cross_scope_run(
    field_name: str,
    foreign_value: str,
) -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix=f"experiment-{field_name}")
    run = _experiment_run(candidate)
    run[field_name] = foreign_value

    with pytest.raises(
        LearningReleaseValidationError,
        match="experiment_run_scope_mismatch",
    ):
        build_learning_release_experiment_evidence(
            candidate=candidate,
            experiment_run=run,
        )


def test_evaluator_evidence_projection_is_bounded_transparent_and_exactly_bound() -> (
    None
):
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="evaluator-projection")
    run = _experiment_run(candidate)
    repeated_evidence = "candidate execution evidence " * 4_000
    repeated_provenance = "repeated execution provenance " * 800
    run["observations"][0]["evidence"] = {
        "raw_execution_material": repeated_evidence,
        "typed_blockers": ["represented_evidence_incomplete"],
    }
    run["observations"][0]["execution_provenance"] = {
        "raw_trace_material": repeated_provenance,
        "authority_execution_request_id": "request-evidence-1",
    }
    experiment = build_learning_release_experiment_evidence(
        candidate=candidate,
        experiment_run=run,
    )
    certification = build_learning_release_certification_evidence(
        candidate=candidate,
        campaign_result=_campaign_result(candidate, certified=False),
        execution_provenance={
            "effective_namespace": NAMESPACE,
            "effective_user_id": USER_ID,
            "effective_org_id": ORG_ID,
        },
    )

    projection = build_learning_release_evaluator_evidence_projection(
        candidate=candidate,
        experiment_evidence=experiment,
        certification_evidence=certification,
    )

    assert projection["schema_version"] == (
        LEARNING_RELEASE_EVALUATOR_EVIDENCE_PROJECTION_SCHEMA_VERSION
    )
    assert projection["candidate_binding"] == {
        "candidate_id": candidate["candidate_id"],
        "candidate_release_sha256": candidate["release_sha256"],
    }
    projected_experiment = projection["experiment_evidence"]
    assert projected_experiment["evidence_sha256"] == experiment["evidence_sha256"]
    assert projected_experiment["experiment_run_sha256"] == (
        experiment["experiment_run_sha256"]
    )
    assert projected_experiment["experiment_run_fields"]["verdict"] == "pass"
    assert projection["certification_evidence"] == certification
    assert projection["certification_evidence"]["campaign_result"]["certified"] is (
        False
    )

    observation = projected_experiment["observation_projections"][0]
    assert observation["source_observation_sha256"] == (
        operational_learning_release_digest(run["observations"][0])
    )
    assert observation["projected_observation"]["verdict"] == "pass"
    evidence_reference = observation["projected_observation"]["evidence"]
    provenance_reference = observation["projected_observation"]["execution_provenance"]
    assert evidence_reference["schema_version"] == (
        LEARNING_RELEASE_EVALUATOR_DIGEST_ONLY_REFERENCE_SCHEMA_VERSION
    )
    assert evidence_reference["source_sha256"] == operational_learning_release_digest(
        run["observations"][0]["evidence"]
    )
    assert evidence_reference["source_mapping_keys"] == [
        "raw_execution_material",
        "typed_blockers",
    ]
    assert provenance_reference["source_sha256"] == (
        operational_learning_release_digest(
            run["observations"][0]["execution_provenance"]
        )
    )
    assert len(projection["digest_only_material"]) == 2
    encoded_projection = json.dumps(projection, sort_keys=True)
    encoded_experiment = json.dumps(experiment, sort_keys=True)
    assert repeated_evidence not in encoded_projection
    assert repeated_provenance not in encoded_projection
    assert len(encoded_projection) < len(encoded_experiment) // 4
    digest_basis = copy.deepcopy(projection)
    projection_sha256 = digest_basis.pop("projection_sha256")
    assert projection_sha256 == operational_learning_release_digest(digest_basis)


def test_evaluator_evidence_projection_rejects_tampered_wrapper() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="tampered-evaluator-projection")
    experiment, certification = _evidence(candidate)
    experiment["experiment_run"]["observations"][0]["evidence"] = {
        "request_id": "tampered-request"
    }

    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_experiment_evidence_hash_mismatch",
    ):
        build_learning_release_evaluator_evidence_projection(
            candidate=candidate,
            experiment_evidence=experiment,
            certification_evidence=certification,
        )


@pytest.mark.parametrize(
    ("provenance_field", "foreign_value"),
    [
        ("effective_namespace", "#V#other_user@other_org"),
        ("effective_user_id", "#V#other_user"),
        ("effective_org_id", "#V#other_org"),
    ],
)
def test_certification_evidence_rejects_cross_scope_campaign_provenance(
    provenance_field: str,
    foreign_value: str,
) -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix=f"campaign-{provenance_field}")
    provenance = {
        "effective_namespace": NAMESPACE,
        "effective_user_id": USER_ID,
        "effective_org_id": ORG_ID,
    }
    provenance[provenance_field] = foreign_value

    with pytest.raises(
        LearningReleaseValidationError,
        match="certification_evidence_scope_mismatch",
    ):
        build_learning_release_certification_evidence(
            candidate=candidate,
            campaign_result=_campaign_result(candidate),
            execution_provenance=provenance,
        )


@pytest.mark.parametrize(
    ("field_name", "foreign_value"),
    [
        ("namespace", "#V#other_user@other_org"),
        ("user_id", "#V#other_user"),
        ("org_id", "#V#other_org"),
    ],
)
def test_represented_decision_rejects_cross_scope_actor_context(
    field_name: str,
    foreign_value: str,
) -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix=f"decision-{field_name}")
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix=field_name,
    )
    decision[field_name] = foreign_value
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="represented_decision_scope_mismatch",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )

    assert state == before


@pytest.mark.parametrize(
    ("field_name", "foreign_value"),
    [
        ("namespace", "#V#other_user@other_org"),
        ("user_id", "#V#other_user"),
        ("org_id", "#V#other_org"),
    ],
)
def test_human_approval_rejects_cross_scope_actor_context(
    field_name: str,
    foreign_value: str,
) -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(
        state,
        suffix=f"approval-{field_name}",
        risk_classes=("security",),
    )
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix=field_name,
    )
    approval = _approval(candidate, action="promote")
    approval[field_name] = foreign_value
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="human_approval_scope_mismatch",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            human_approval=approval,
            recorded_at=DECISION_TIME,
        )

    assert state == before


def test_high_risk_approval_without_authenticated_receipt_is_rejected() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(
        state,
        suffix="forged-approval",
        risk_classes=("privilege",),
    )
    experiment, certification = _evidence(candidate)
    decision = _decision(
        candidate,
        experiment,
        certification,
        action="promote",
        suffix="forged-approval",
    )
    forged_approval = _approval(candidate, action="promote")
    forged_approval.pop("authenticated_approval_receipt")
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="authenticated_human_approval_receipt_required",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            human_approval=forged_approval,
            recorded_at=DECISION_TIME,
        )

    assert state == before


def test_tampered_authenticated_approval_receipt_is_rejected() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(
        state,
        suffix="tampered-approval-receipt",
        risk_classes=("destructive",),
    )
    experiment, certification = _evidence(candidate)
    approval = _approval(candidate, action="promote")
    approval["authenticated_approval_receipt"]["approver_id"] = "#V#forged_reviewer"
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="authenticated_human_approval_receipt_hash_mismatch",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=_decision(
                candidate,
                experiment,
                certification,
                action="promote",
                suffix="tampered-approval-receipt",
            ),
            experiment_evidence=experiment,
            certification_evidence=certification,
            human_approval=approval,
            recorded_at=DECISION_TIME,
        )

    assert state == before


@pytest.mark.parametrize(
    "suite_source",
    ["seed_bundle_import_fixture", "offline_evidence_evaluation", "synthetic"],
)
def test_synthetic_or_offline_campaign_cannot_promote(
    suite_source: str,
) -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix=f"source-{suite_source}")
    experiment = build_learning_release_experiment_evidence(
        candidate=candidate,
        experiment_run=_experiment_run(candidate),
    )
    campaign = _campaign_result(candidate)
    campaign["suite_source"] = suite_source
    campaign.pop("report_sha256")
    campaign["report_sha256"] = operational_learning_release_digest(campaign)
    certification = build_learning_release_certification_evidence(
        candidate=candidate,
        campaign_result=campaign,
        execution_provenance={
            "effective_namespace": NAMESPACE,
            "effective_user_id": USER_ID,
            "effective_org_id": ORG_ID,
        },
    )
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="promotion_requires_live_vontology_certification",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=_decision(
                candidate,
                experiment,
                certification,
                action="promote",
                suffix=f"source-{suite_source}",
            ),
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )

    assert state == before


def test_live_campaign_without_release_eligibility_gate_cannot_promote() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="missing-live-gate")
    experiment = build_learning_release_experiment_evidence(
        candidate=candidate,
        experiment_run=_experiment_run(candidate),
    )
    campaign = _campaign_result(candidate)
    campaign["certification_gate_results"] = [
        gate
        for gate in campaign["certification_gate_results"]
        if gate["gate_id"] != "live_release_evidence_eligible"
    ]
    campaign.pop("report_sha256")
    campaign["report_sha256"] = operational_learning_release_digest(campaign)
    certification = build_learning_release_certification_evidence(
        candidate=candidate,
        campaign_result=campaign,
        execution_provenance={
            "effective_namespace": NAMESPACE,
            "effective_user_id": USER_ID,
            "effective_org_id": ORG_ID,
        },
    )

    with pytest.raises(
        LearningReleaseValidationError,
        match="promotion_requires_live_release_evidence_gate",
    ):
        promote_operational_learning_release_candidate(
            state,
            candidate_id=candidate["candidate_id"],
            represented_evaluator_decision=_decision(
                candidate,
                experiment,
                certification,
                action="promote",
                suffix="missing-live-gate",
            ),
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=DECISION_TIME,
        )


def test_tampered_persisted_receipt_blocks_further_release_transitions() -> None:
    state: dict[str, Any] = {}
    active = _register_candidate(state, suffix="receipt-integrity")
    _promote(state, active, suffix="receipt-integrity")
    state["decision_receipts"][0]["outcome"] = "forged-outcome"
    tampered_state = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_receipt_hash_mismatch",
    ):
        _register_candidate(state, suffix="after-tampered-receipt")

    assert state == tampered_state


def test_tampered_pointer_scope_blocks_further_release_transitions() -> None:
    state: dict[str, Any] = {}
    active = _register_candidate(state, suffix="pointer-integrity")
    _promote(state, active, suffix="pointer-integrity")
    state["release_pointers"][ARTIFACT_ID]["active"][
        "namespace"
    ] = "#V#other_user@other_org"
    tampered_state = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="release_pointer_candidate_integrity_mismatch",
    ):
        _register_candidate(state, suffix="after-tampered-pointer")

    assert state == tampered_state


def test_tampered_lifecycle_state_blocks_further_release_transitions() -> None:
    state: dict[str, Any] = {}
    candidate = _register_candidate(state, suffix="state-integrity")
    state["candidate_states"][candidate["candidate_id"]]["state"] = "active"
    tampered_state = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="learning_release_lifecycle_state_inconsistent",
    ):
        _register_candidate(state, suffix="after-tampered-state")

    assert state == tampered_state


def test_rollback_refuses_to_restore_an_expired_previous_release() -> None:
    state: dict[str, Any] = {}
    previous = _register_candidate(state, suffix="expired-previous")
    _promote(state, previous, suffix="expired-previous")
    active = _register_candidate(state, suffix="active-after-expired")
    _promote(state, active, suffix="active-after-expired")
    experiment, certification = _evidence(
        active,
        experiment_verdict="fail",
        certified=False,
    )
    decision = _decision(
        active,
        experiment,
        certification,
        action="rollback",
        suffix="expired-previous",
    )
    before = copy.deepcopy(state)

    with pytest.raises(
        LearningReleaseValidationError,
        match="rollback_previous_release_expired",
    ):
        rollback_operational_learning_release(
            state,
            affected_artifact=ARTIFACT_ID,
            represented_evaluator_decision=decision,
            experiment_evidence=experiment,
            certification_evidence=certification,
            recorded_at=datetime(2028, 1, 1, tzinfo=timezone.utc),
        )

    assert state == before
