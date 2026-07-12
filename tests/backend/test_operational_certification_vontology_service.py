from __future__ import annotations

import pytest

from src.backend.services import operational_certification_vontology_service as service


def test_campaign_evidence_loader_returns_none_when_authority_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: None,
    )

    assert service.load_represented_operational_campaign_evidence() is None


def test_campaign_evidence_loader_returns_none_for_canonical_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_concept_id: str):
        raise service.ConceptNotFoundError("missing")

    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        _missing,
    )

    assert service.load_represented_operational_campaign_evidence() is None


def test_campaign_evidence_loader_binds_scope_and_learning_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_load_learning_release_campaign_projection",
        lambda **_kwargs: {
            "authority": {"state_concept_id": "#V#release_state"},
            "completed_learning_loop_count": 2,
            "learning_release_receipt_ids": ["receipt-2", "receipt-1"],
            "evaluated_learning_release_candidate_bindings": [
                {
                    "candidate_id": "candidate-2",
                    "candidate_release_sha256": "b" * 64,
                },
                {
                    "candidate_id": "candidate-1",
                    "candidate_release_sha256": "a" * 64,
                },
            ],
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: {
            "concept_id": service.OPERATIONAL_CERTIFICATION_CAMPAIGN_EVIDENCE_CONCEPT_ID,
            "attributes": {
                "operational_certification_campaign_evidence": {
                    "effective_namespace": "#V#user@org",
                    "effective_user_id": "#V#user",
                    "effective_org_id": "#V#org",
                    "pilot_corpus_agreed": True,
                    "pilot_envelopes_agreed": True,
                    "safe_operating_envelope": {"profile": "trusted_sail"},
                }
            },
        },
    )

    evidence = service.load_represented_operational_campaign_evidence(
        expected_namespace="#V#user@org",
        expected_user_id="#V#user",
        expected_org_id="#V#org",
    )

    assert evidence is not None
    assert evidence["source"] == "vontology"
    assert evidence["pilot_corpus_agreed"] is True
    assert evidence["pilot_envelopes_agreed"] is True
    assert evidence["completed_learning_loop_count"] == 2
    assert evidence["learning_release_receipt_ids"] == ["receipt-1", "receipt-2"]
    assert evidence["learning_release_authority"] == {
        "state_concept_id": "#V#release_state"
    }
    assert evidence["evaluated_learning_release_candidate_ids"] == [
        "candidate-1",
        "candidate-2",
    ]
    assert evidence["evaluated_learning_release_candidate_bindings"] == [
        {
            "candidate_id": "candidate-1",
            "candidate_release_sha256": "a" * 64,
        },
        {
            "candidate_id": "candidate-2",
            "candidate_release_sha256": "b" * 64,
        },
    ]
    assert evidence["safe_operating_envelope"] == {"profile": "trusted_sail"}
    assert len(evidence["authority"]["revision_sha256"]) == 64
    assert len(evidence["evidence_sha256"]) == 64


def test_campaign_evidence_loader_rejects_cross_scope_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: {
            "attributes": {
                "operational_certification_campaign_evidence": {
                    "effective_namespace": "#V#other@org",
                    "effective_user_id": "#V#other",
                    "effective_org_id": "#V#org",
                }
            }
        },
    )

    with pytest.raises(ValueError, match="campaign_evidence_scope_mismatch"):
        service.load_represented_operational_campaign_evidence(
            expected_namespace="#V#user@org",
            expected_user_id="#V#user",
            expected_org_id="#V#org",
        )


def test_legacy_candidate_id_only_campaign_evidence_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: {
            "attributes": {
                "operational_certification_campaign_evidence": {
                    "effective_namespace": "#V#user@org",
                    "effective_user_id": "#V#user",
                    "effective_org_id": "#V#org",
                    "evaluated_learning_release_candidate_ids": ["candidate-1"],
                }
            }
        },
    )

    with pytest.raises(ValueError, match="must_be_state_derived"):
        service.load_represented_operational_campaign_evidence(
            expected_namespace="#V#user@org",
            expected_user_id="#V#user",
            expected_org_id="#V#org",
        )


def test_missing_safe_envelope_remains_absent_for_exists_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_load_learning_release_campaign_projection",
        lambda **_kwargs: {
            "authority": {"state_concept_id": "#V#release_state"},
            "completed_learning_loop_count": 0,
            "learning_release_receipt_ids": [],
            "evaluated_learning_release_candidate_bindings": [],
        },
    )
    monkeypatch.setattr(
        service.concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: {
            "attributes": {
                "operational_certification_campaign_evidence": {
                    "effective_namespace": "#V#user@org",
                    "effective_user_id": "#V#user",
                    "effective_org_id": "#V#org",
                    "safe_operating_envelope": None,
                }
            }
        },
    )

    evidence = service.load_represented_operational_campaign_evidence(
        expected_namespace="#V#user@org",
        expected_user_id="#V#user",
        expected_org_id="#V#org",
    )

    assert evidence is not None
    assert "safe_operating_envelope" not in evidence
