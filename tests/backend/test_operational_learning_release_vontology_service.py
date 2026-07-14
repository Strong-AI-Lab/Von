from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from src.backend.services import (
    operational_learning_release_vontology_service as service,
)
from src.backend.services.operational_learning_release_service import (
    OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION,
    REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION,
    build_failure_evidence_packets,
    operational_learning_release_digest,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
NAMESPACE = "#V#user@org"
USER_ID = "#V#user"
ORG_ID = "#V#org"
ARTIFACT_ID = "#V#generic_policy_artifact"


@pytest.fixture
def vontology_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    concepts: dict[str, dict[str, Any]] = {}
    texts: dict[str, str] = {}
    writes: list[dict[str, Any]] = []
    creates: list[dict[str, Any]] = []

    def get_concept(concept_id: str | None = None, **_kwargs):
        return copy.deepcopy(concepts.get(str(concept_id)))

    def create_concept(**kwargs):
        creates.append(copy.deepcopy(kwargs))
        concept_id = str(kwargs["concept_id"])
        doc = {
            "concept_id": concept_id,
            "attributes": copy.deepcopy(kwargs.get("attributes") or {}),
        }
        concepts[concept_id] = doc
        return copy.deepcopy(doc)

    def get_texts(
        subject_concept_id: str,
        **_kwargs,
    ) -> list[dict[str, Any]]:
        text = texts.get(subject_concept_id)
        return [] if text is None else [{"text": text, "lang": "en-NZ"}]

    def upsert_text(**kwargs):
        writes.append(copy.deepcopy(kwargs))
        texts[str(kwargs["subject_concept_id"])] = str(kwargs["text"])
        return {"success": True, "relation_id": "relation-1"}

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        get_concept,
    )
    monkeypatch.setattr(service.concept_service, "create_concept", create_concept)
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
    monkeypatch.setattr(service, "get_texts_for_concept", get_texts)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", upsert_text)
    monkeypatch.setattr(
        service, "_verify_live_authority_reference", lambda _value, **_kwargs: None
    )
    monkeypatch.setattr(
        service,
        "_verify_live_transition_evidence",
        lambda **_kwargs: None,
    )

    def activate(candidate):
        receipt = {
            "activated": True,
            "affected_artifact": candidate["affected_artifact"],
            "candidate_release_sha256": candidate["release_sha256"],
            "runtime_release_sha256": candidate["release_sha256"],
        }
        receipt["receipt_sha256"] = operational_learning_release_digest(receipt)
        return receipt

    monkeypatch.setattr(service, "_apply_and_readback_release_activation", activate)
    return {
        "concepts": concepts,
        "texts": texts,
        "writes": writes,
        "creates": creates,
    }


def _authority() -> dict[str, Any]:
    return {
        "schema_version": REPRESENTED_AUTHORITY_REFERENCE_SCHEMA_VERSION,
        "authority_concept_id": "#V#represented_learning_workflow",
        "authority_revision_sha256": operational_learning_release_digest(
            {"authority": "#V#represented_learning_workflow", "revision": 1}
        ),
    }


def _packets() -> list[dict[str, Any]]:
    return build_failure_evidence_packets(
        [
            {
                "request_id": "request-1",
                "causal_stage": "verification",
                "cause_code": "represented_evidence_incomplete",
                "affected_artifact": ARTIFACT_ID,
                "outcome": "typed_non_success",
            }
        ],
        created_at=NOW,
    )


def _register(
    current: dict[str, Any],
    *,
    risk_classes: list[str] | None = None,
) -> dict[str, Any]:
    return service.register_operational_learning_release_candidate_in_vontology(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        expected_version=current["version"],
        expected_state_sha256=current["state_sha256"],
        candidate_id="candidate-1",
        release_id="release-1",
        affected_artifact=ARTIFACT_ID,
        release_payload={"represented_revision": "revision-1"},
        failure_evidence_packets=_packets(),
        proposal_authority=_authority(),
        risk_classes=risk_classes or ["quality"],
        expires_at="2027-01-01T00:00:00+00:00",
        retest_after="2026-12-01T00:00:00+00:00",
        retest_requirements={"suite_id": "#V#generic_regression_suite"},
        created_at=NOW,
    )


def test_absent_state_projects_virtual_version_zero_without_writing(
    vontology_store: dict[str, Any],
) -> None:
    record = service.load_operational_learning_release_state(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
    )

    assert record["version"] == 0
    assert record["persisted"] is False
    assert record["state_sha256"] == operational_learning_release_digest(
        record["state"]
    )
    assert record["campaign_evidence_projection"]["completed_learning_loop_count"] == 0
    assert "pilot_corpus_agreed" not in record["campaign_evidence_projection"]
    assert "safe_operating_envelope" not in record["campaign_evidence_projection"]
    assert vontology_store["writes"] == []
    assert vontology_store["creates"] == []


def test_candidate_resolver_projects_exact_immutable_context_without_writing(
    vontology_store: dict[str, Any],
) -> None:
    registered = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    candidate = registered["state"]["candidate_snapshots"]["candidate-1"]
    write_count = len(vontology_store["writes"])

    result = service.resolve_operational_learning_release_candidate_in_vontology(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        candidate_id="candidate-1",
        release_sha256=candidate["release_sha256"],
        affected_artifact=ARTIFACT_ID,
    )

    context = result["result"]["candidate_context"]
    assert context["status"] == "resolved"
    assert context["candidate_snapshot"] == candidate
    assert context["candidate_snapshot_sha256"] == candidate["candidate_sha256"]
    assert context["failure_evidence_material_availability"] == {
        "schema_version": ("represented_failure_evidence_material_availability.v1"),
        "packet_count": 1,
        "member_count": 1,
        "member_digest_locator_count": 1,
        "member_inline_source_material_count": 0,
        "member_source_material_reference_count": 0,
        "member_source_material_count": 0,
        "all_members_have_source_material": False,
        "coverage": "digest_locators_only",
    }
    assert context["release_payload"] == candidate["release_payload"]
    assert context["release_payload_sha256"] == operational_learning_release_digest(
        candidate["release_payload"]
    )
    assert "parent_release_sha256" in context
    assert context["parent_release_sha256"] is None
    assert context["binding"]["candidate_id"] == "candidate-1"
    assert context["authority"]["record_sha256"] == registered["record_sha256"]
    digest_basis = copy.deepcopy(context)
    observed_digest = digest_basis.pop("context_sha256")
    assert observed_digest == operational_learning_release_digest(digest_basis)
    assert len(vontology_store["writes"]) == write_count


def test_failure_evidence_material_projection_reports_partial_source_material() -> None:
    projection = service._project_failure_evidence_material_availability(
        {
            "failure_evidence_packets": [
                {
                    "members": [
                        {
                            "source_evidence_sha256": "a" * 64,
                            "source_evidence": {"causal_stage": "verification"},
                        },
                        {
                            "source_evidence_sha256": "b" * 64,
                        },
                    ]
                }
            ]
        }
    )

    assert projection["coverage"] == "partial_source_material"
    assert projection["member_count"] == 2
    assert projection["member_digest_locator_count"] == 2
    assert projection["member_inline_source_material_count"] == 1
    assert projection["member_source_material_count"] == 1
    assert projection["all_members_have_source_material"] is False


def test_candidate_context_workflow_projects_nullable_parent_release_fact() -> None:
    bundle_path = (
        Path(service.__file__).resolve().parents[1]
        / "workflows"
        / "repo_seed_bundles"
        / "operational_learning_release_authority_workflow_seed_bundle.json"
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    workflow = next(
        item
        for item in bundle["workflows"]
        if item["workflow_id"]
        == "#V#operational_learning_candidate_context_resolution_workflow"
    )
    steps = {item["state_id"]: item for item in workflow["publication_spec"]["steps"]}
    resolve_step = steps["resolve_candidate"]
    parent_mapping = next(
        item
        for item in resolve_step["tool_output_mapping_specs"]
        if item["context_key"] == "parent_release_sha256"
    )
    assert parent_mapping["tool_output_field"] == (
        "result.candidate_context.parent_release_sha256"
    )
    assert "parent_release_sha256" in resolve_step["writes_context_keys"]

    project_bindings = dict(steps["project_candidate_context"]["static_input_bindings"])
    assert project_bindings["field_sources"]["parent_release_sha256"] == {
        "$context_key": "parent_release_sha256"
    }
    assert "parent_release_sha256" in project_bindings["required_fields"]
    assert project_bindings["include_null_fields"] is True


def test_candidate_resolver_fails_closed_on_release_binding_mismatch(
    vontology_store: dict[str, Any],
) -> None:
    registered = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    candidate = registered["state"]["candidate_snapshots"]["candidate-1"]

    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_candidate_binding_mismatch",
    ):
        service.resolve_operational_learning_release_candidate_in_vontology(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            candidate_id="candidate-1",
            release_sha256="f" * 64,
            affected_artifact=ARTIFACT_ID,
        )

    assert candidate["release_sha256"] != "f" * 64


def test_active_release_resolver_returns_typed_absence_and_checks_expectation(
    vontology_store: dict[str, Any],
) -> None:
    _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )

    result = service.resolve_operational_learning_active_release_in_vontology(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        affected_artifact=ARTIFACT_ID,
    )

    active_release = result["result"]["active_release"]
    assert active_release["status"] == "no_active_release"
    assert active_release["affected_artifact"] == ARTIFACT_ID
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_expected_active_not_found",
    ):
        service.resolve_operational_learning_active_release_in_vontology(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            affected_artifact=ARTIFACT_ID,
            expected_release_sha256="a" * 64,
        )


def test_active_release_resolver_projects_exact_active_payload(
    monkeypatch: pytest.MonkeyPatch,
    vontology_store: dict[str, Any],
) -> None:
    registered = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    candidate = registered["state"]["candidate_snapshots"]["candidate-1"]
    active_record = copy.deepcopy(registered)
    active_record["state"]["candidate_states"]["candidate-1"]["state"] = "active"
    active_record["state"]["release_states"][candidate["release_sha256"]][
        "state"
    ] = "active"
    active_record["state"]["release_pointers"][ARTIFACT_ID] = {
        "active": {
            "schema_version": "operational_learning_release_pointer.v1",
            "affected_artifact": ARTIFACT_ID,
            "namespace": NAMESPACE,
            "user_id": USER_ID,
            "org_id": ORG_ID,
            "candidate_id": "candidate-1",
            "release_id": "release-1",
            "release_sha256": candidate["release_sha256"],
            "risk_classes": candidate["risk_classes"],
            "expires_at": candidate["expires_at"],
            "retest_after": candidate["retest_after"],
            "retest_requirements": candidate["retest_requirements"],
        },
        "previous": None,
    }
    monkeypatch.setattr(
        service,
        "load_operational_learning_release_state",
        lambda **_kwargs: copy.deepcopy(active_record),
    )

    result = service.resolve_operational_learning_active_release_in_vontology(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        affected_artifact=ARTIFACT_ID,
        expected_release_sha256=candidate["release_sha256"],
    )

    active_release = result["result"]["active_release"]
    assert active_release["status"] == "active_release_resolved"
    assert active_release["candidate_id"] == "candidate-1"
    assert active_release["release_payload"] == candidate["release_payload"]
    assert active_release["candidate_lifecycle_state"] == "active"


def test_scope_mismatch_fails_before_vontology_access(
    vontology_store: dict[str, Any],
) -> None:
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_scope_mismatch",
    ):
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id="#V#other",
            org_id=ORG_ID,
        )

    assert vontology_store["writes"] == []


def test_register_persists_exact_scope_version_digest_and_readback(
    vontology_store: dict[str, Any],
) -> None:
    empty = service.load_operational_learning_release_state(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
    )
    result = _register(empty)
    record = result["state_record"]

    assert record["persisted"] is True
    assert record["version"] == 1
    assert record["previous_state_sha256"] == empty["state_sha256"]
    assert record["namespace"] == NAMESPACE
    assert record["user_id"] == USER_ID
    assert record["org_id"] == ORG_ID
    candidate = result["result"]
    assert record["state"]["candidate_snapshots"]["candidate-1"] == candidate
    assert record["state_sha256"] == operational_learning_release_digest(
        record["state"]
    )
    assert len(vontology_store["writes"]) == 1
    state_create = next(
        item
        for item in vontology_store["creates"]
        if item.get("create_as_instance") is True
    )
    assert state_create["created_by_concept_id"] == USER_ID
    assert state_create["organisation_concept_id"] == ORG_ID
    assert state_create["event_namespace"] == NAMESPACE
    assert state_create["attributes"]["operational_learning_release_scope"] == {
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }

    loaded = service.load_operational_learning_release_state(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
    )
    assert loaded == record


@pytest.mark.parametrize(
    ("expected_version_delta", "digest"),
    [(-1, None), (0, "f" * 64)],
)
def test_stale_version_or_digest_preserves_state_and_exposes_recovery(
    vontology_store: dict[str, Any],
    expected_version_delta: int,
    digest: str | None,
) -> None:
    current = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    writes_before = len(vontology_store["writes"])

    with pytest.raises(service.LearningReleaseStateConflictError) as exc_info:
        service.register_operational_learning_release_candidate_in_vontology(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            expected_version=current["version"] + expected_version_delta,
            expected_state_sha256=digest or current["state_sha256"],
            candidate_id="candidate-2",
            release_id="release-2",
            affected_artifact=ARTIFACT_ID,
            release_payload={"represented_revision": "revision-2"},
            failure_evidence_packets=_packets(),
            proposal_authority=_authority(),
            risk_classes=["quality"],
            expires_at="2027-01-01T00:00:00+00:00",
            retest_after="2026-12-01T00:00:00+00:00",
            retest_requirements={"suite_id": "#V#generic_regression_suite"},
            created_at=NOW,
        )

    projection = exc_info.value.to_dict()
    assert projection["details"]["current_version"] == current["version"]
    assert projection["details"]["current_state_sha256"] == current["state_sha256"]
    assert {item["action_type"] for item in projection["recovery_affordances"]} == {
        "read_latest_state",
        "retry_with_latest_version",
    }
    assert len(vontology_store["writes"]) == writes_before


def test_tampered_state_or_record_digest_fails_closed(
    vontology_store: dict[str, Any],
) -> None:
    record = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    concept_id = record["state_concept_id"]
    stored = json.loads(vontology_store["texts"][concept_id])
    stored["state"]["candidate_states"]["candidate-1"][
        "updated_at"
    ] = "2026-07-12T13:00:00+00:00"
    vontology_store["texts"][concept_id] = json.dumps(stored)

    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_state_digest_mismatch",
    ):
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )

    stored["state_sha256"] = operational_learning_release_digest(stored["state"])
    stored["updated_at"] = "2026-07-12T13:00:00+00:00"
    vontology_store["texts"][concept_id] = json.dumps(stored)
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_record_digest_mismatch",
    ):
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )


def test_write_without_exact_readback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    vontology_store: dict[str, Any],
) -> None:
    empty = service.load_operational_learning_release_state(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **_kwargs: {"success": True},
    )

    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_state_readback_mismatch",
    ):
        _register(empty)


def test_second_process_cannot_mutate_without_storage_writer_lease(
    monkeypatch: pytest.MonkeyPatch,
    vontology_store: dict[str, Any],
) -> None:
    empty = service.load_operational_learning_release_state(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
    )
    monkeypatch.setattr(
        service.concept_service,
        "acquire_concept_mutation_lease",
        lambda **_kwargs: {"success": False},
    )

    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="operational_learning_release_single_writer_unavailable",
    ) as exc_info:
        _register(empty)

    assert vontology_store["writes"] == []
    assert {
        item["action_type"] for item in exc_info.value.to_dict()["recovery_affordances"]
    } == {"read_latest_state", "retry_after_writer_lease"}


def test_authenticated_approval_is_derived_persisted_and_required_for_live_path(
    monkeypatch: pytest.MonkeyPatch,
    vontology_store: dict[str, Any],
) -> None:
    registered = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        ),
        risk_classes=["security"],
    )["state_record"]
    approval_result = service.record_authenticated_human_learning_release_approval(
        authenticated_namespace=NAMESPACE,
        authenticated_user_id=USER_ID,
        authenticated_org_id=ORG_ID,
        expected_version=registered["version"],
        expected_state_sha256=registered["state_sha256"],
        approval_id="approval-1",
        action="promote",
        candidate_id="candidate-1",
        authenticated_at=NOW,
    )
    approved_record = approval_result["state_record"]
    approval = approval_result["result"]

    assert approved_record["version"] == registered["version"] + 1
    assert approved_record["state_sha256"] == registered["state_sha256"]
    assert approval["approver_id"] == USER_ID
    assert approval["namespace"] == NAMESPACE
    assert approval["authenticated_approval_receipt"]["action"] == "promote"
    assert approval["authenticated_approval_receipt"]["candidate_id"] == ("candidate-1")

    forged = copy.deepcopy(approval)
    forged["approval_id"] = "approval-forged"
    forged_receipt = forged["authenticated_approval_receipt"]
    forged_receipt["approval_id"] = "approval-forged"
    forged_receipt.pop("receipt_sha256")
    forged_receipt["receipt_sha256"] = operational_learning_release_digest(
        forged_receipt
    )
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="authoritative_human_approval_not_found",
    ):
        service.promote_operational_learning_release_candidate_in_vontology(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            expected_version=approved_record["version"],
            expected_state_sha256=approved_record["state_sha256"],
            candidate_id="candidate-1",
            represented_evaluator_decision={},
            experiment_evidence={},
            certification_evidence={},
            human_approval=forged,
            recorded_at=NOW,
        )

    captured: dict[str, Any] = {}

    def fake_promote(state, **kwargs):
        captured.update(kwargs)
        return {"receipt_id": "synthetic-support-test"}

    monkeypatch.setattr(
        service,
        "promote_operational_learning_release_candidate",
        fake_promote,
    )
    promoted = service.promote_operational_learning_release_candidate_in_vontology(
        namespace=NAMESPACE,
        user_id=USER_ID,
        org_id=ORG_ID,
        expected_version=approved_record["version"],
        expected_state_sha256=approved_record["state_sha256"],
        candidate_id="candidate-1",
        represented_evaluator_decision={},
        experiment_evidence={},
        certification_evidence={},
        human_approval=approval,
        recorded_at=NOW,
    )

    assert promoted["success"] is True
    assert captured["human_approval"] == approval


def test_live_promotion_requires_authoritative_approval_even_for_declared_low_risk(
    vontology_store: dict[str, Any],
) -> None:
    registered = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        ),
        risk_classes=["quality"],
    )["state_record"]

    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="authoritative_human_approval_required_for_live_release",
    ):
        service.promote_operational_learning_release_candidate_in_vontology(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            expected_version=registered["version"],
            expected_state_sha256=registered["state_sha256"],
            candidate_id="candidate-1",
            represented_evaluator_decision={},
            experiment_evidence={},
            certification_evidence={},
            recorded_at=NOW,
        )


def test_missing_runtime_activation_adapter_is_a_typed_non_promotion() -> None:
    candidate = {
        "candidate_id": "candidate-1",
        "affected_artifact": ARTIFACT_ID,
        "release_sha256": "a" * 64,
    }

    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="canonical_learning_release_activation_adapter_unavailable",
    ) as exc_info:
        service._apply_and_readback_release_activation(candidate)

    assert exc_info.value.details["decision_state"] == (
        "promotion_approved_not_activated"
    )


def test_candidate_evaluation_binding_precedes_decision_and_feeds_campaign(
    vontology_store: dict[str, Any],
) -> None:
    registered = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    result = (
        service.register_operational_learning_release_candidate_evaluation_in_vontology(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
            expected_version=registered["version"],
            expected_state_sha256=registered["state_sha256"],
            evaluation_id="evaluation-1",
            candidate_id="candidate-1",
            registered_at=NOW,
        )
    )
    record = result["state_record"]
    candidate = record["state"]["candidate_snapshots"]["candidate-1"]
    campaign = record["campaign_evidence_projection"]

    assert record["state_sha256"] == registered["state_sha256"]
    assert campaign["completed_learning_loop_count"] == 0
    assert campaign["learning_release_receipt_ids"] == []
    assert campaign["evaluated_learning_release_candidate_bindings"] == [
        {
            "candidate_id": "candidate-1",
            "candidate_release_sha256": candidate["release_sha256"],
        }
    ]


def test_campaign_projection_uses_unique_receipts_and_exact_release_bindings(
    vontology_store: dict[str, Any],
) -> None:
    record = _register(
        service.load_operational_learning_release_state(
            namespace=NAMESPACE,
            user_id=USER_ID,
            org_id=ORG_ID,
        )
    )["state_record"]
    candidate = record["state"]["candidate_snapshots"]["candidate-1"]
    receipt = {
        "schema_version": OPERATIONAL_LEARNING_RELEASE_RECEIPT_SCHEMA_VERSION,
        "receipt_id": "learning-release-receipt-1",
        "action": "reject",
        "candidate_id": candidate["candidate_id"],
        "release_sha256": candidate["release_sha256"],
    }
    receipt["receipt_sha256"] = operational_learning_release_digest(receipt)
    record["state"]["decision_receipts"] = [receipt, copy.deepcopy(receipt)]
    record["state_sha256"] = operational_learning_release_digest(record["state"])
    storage_record = {
        key: value
        for key, value in record.items()
        if key not in {"record_sha256", "persisted", "campaign_evidence_projection"}
    }
    record["record_sha256"] = operational_learning_release_digest(storage_record)

    projection = service.project_operational_learning_release_campaign_evidence(record)

    assert projection["completed_learning_loop_count"] == 1
    assert projection["learning_release_receipt_ids"] == ["learning-release-receipt-1"]
    assert projection["evaluated_learning_release_candidate_bindings"] == [
        {
            "candidate_id": candidate["candidate_id"],
            "candidate_release_sha256": candidate["release_sha256"],
        }
    ]
    assert "pilot_corpus_agreed" not in projection
    assert "safe_operating_envelope" not in projection


def test_live_authority_revision_must_match_current_vontology_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_scope: dict[str, str | None] = {}

    def _load_identity(_workflow_id: str) -> dict[str, str]:
        from src.backend.security.access_control import (
            get_effective_organisation_concept_id,
            get_effective_user_concept_id,
        )

        observed_scope["user_id"] = get_effective_user_concept_id()
        observed_scope["org_id"] = get_effective_organisation_concept_id()
        return {"authoritative_definition_hash": "a" * 64}

    monkeypatch.setattr(
        service,
        "_load_authoritative_workflow_identity",
        _load_identity,
    )

    service._verify_live_authority_reference(
        {
            "authority_concept_id": "#V#represented_learning_workflow",
            "authority_revision_sha256": "a" * 64,
        },
        user_id=USER_ID,
        org_id=ORG_ID,
    )
    assert observed_scope == {"user_id": USER_ID, "org_id": ORG_ID}
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="live_represented_authority_revision_mismatch",
    ):
        service._verify_live_authority_reference(
            {
                "authority_concept_id": "#V#represented_learning_workflow",
                "authority_revision_sha256": "b" * 64,
            },
            user_id=USER_ID,
            org_id=ORG_ID,
        )


def test_self_consistent_inline_experiment_cannot_replace_canonical_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_run = {
        "schema_version": "experiment_run.v1",
        "run_id": "run-1",
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "metadata": {
            "learning_release_candidate_id": "candidate-1",
            "learning_release_candidate_release_sha256": "c" * 64,
        },
    }
    candidate = {
        "candidate_id": "candidate-1",
        "release_sha256": "c" * 64,
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }
    monkeypatch.setattr(
        service,
        "_load_canonical_experiment_run",
        lambda _run_id: copy.deepcopy(canonical_run),
    )
    canonical_digest = operational_learning_release_digest(canonical_run)
    service._verify_canonical_experiment_evidence(
        candidate=candidate,
        experiment_evidence={
            "experiment_run": copy.deepcopy(canonical_run),
            "experiment_run_sha256": canonical_digest,
        },
    )

    forged_run = copy.deepcopy(canonical_run)
    forged_run["verdict"] = "pass"
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="canonical_learning_release_experiment_run_mismatch",
    ):
        service._verify_canonical_experiment_evidence(
            candidate=candidate,
            experiment_evidence={
                "experiment_run": forged_run,
                "experiment_run_sha256": operational_learning_release_digest(
                    forged_run
                ),
            },
        )


def test_represented_decision_must_be_exact_terminal_trace_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = {
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }
    decision = {
        "schema_version": "represented_learning_release_decision.v1",
        "decision_id": "decision-1",
        "decision": "promote",
        "authority": {
            "authority_concept_id": "#V#represented_promotion_workflow",
            "authority_revision_sha256": "a" * 64,
            "authority_prompt_revision_sha256": "b" * 64,
            "authority_execution_request_id": "request-decision-1",
        },
    }
    trace = {
        "request_id": "request-decision-1",
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "workflow_execution": {
            "workflow_id": "#V#represented_promotion_workflow",
            "workflow_definition_identity": {
                "authoritative_definition_hash": "a" * 64,
            },
            "prompt_context": {"prompt_revision_sha256": "b" * 64},
            "outputs": {"represented_decision": decision},
        },
    }
    monkeypatch.setattr(
        service,
        "_load_canonical_decision_trace",
        lambda _request_id, _namespace: copy.deepcopy(trace),
    )

    service._verify_persisted_represented_decision_trace(
        candidate=candidate,
        decision=decision,
    )
    forged = copy.deepcopy(decision)
    forged["decision"] = "reject"
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="represented_decision_not_found_in_execution_trace",
    ):
        service._verify_persisted_represented_decision_trace(
            candidate=candidate,
            decision=forged,
        )

    wrong_authority_trace = copy.deepcopy(trace)
    wrong_authority_trace["workflow_execution"]["workflow_id"] = "#V#unrelated_workflow"
    monkeypatch.setattr(
        service,
        "_load_canonical_decision_trace",
        lambda _request_id, _namespace: copy.deepcopy(wrong_authority_trace),
    )
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="represented_decision_execution_authority_mismatch",
    ):
        service._verify_persisted_represented_decision_trace(
            candidate=candidate,
            decision=decision,
        )


def test_certification_report_must_match_persisted_campaign_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.backend.services import operational_certification_runner_service
    from src.backend.services import operational_certification_attestation_service

    candidate = {
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }
    campaign = {"report_sha256": "a" * 64, "certified": True}
    provenance = {"experiment_run_id": "certification-run-1", "source": "live"}
    canonical_run = {
        "run_id": "certification-run-1",
        "namespace": NAMESPACE,
        "user_id": USER_ID,
        "org_id": ORG_ID,
        "observations": [
            {
                "evidence": {
                    "operational_certification_campaign_result": copy.deepcopy(campaign)
                },
                "execution_provenance": copy.deepcopy(provenance),
            }
        ],
    }
    monkeypatch.setattr(
        service,
        "_load_canonical_experiment_run",
        lambda _run_id: copy.deepcopy(canonical_run),
    )
    monkeypatch.setattr(
        operational_certification_runner_service,
        "validate_campaign_experiment_observation",
        lambda _observation: [],
    )
    monkeypatch.setattr(
        operational_certification_attestation_service,
        "verify_operational_certification_runner_attestation",
        lambda _value, *, expected: {**expected, "signature_verified": True},
    )
    service._verify_canonical_certification_evidence(
        candidate=candidate,
        certification_evidence={
            "campaign_result": campaign,
            "execution_provenance": provenance,
        },
    )

    forged_campaign = {**campaign, "forged": True}
    with pytest.raises(
        service.LearningReleasePersistenceError,
        match="canonical_operational_certification_report_mismatch",
    ):
        service._verify_canonical_certification_evidence(
            candidate=candidate,
            certification_evidence={
                "campaign_result": forged_campaign,
                "execution_provenance": provenance,
            },
        )
