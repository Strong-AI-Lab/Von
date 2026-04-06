from __future__ import annotations

from src.backend.services.testing_workflow_contracts import (
    EPHEMERAL_THEORY_TYPE_ID,
    EXPERIMENT_RUN_TYPE_ID,
    EXPERIMENT_SPEC_TYPE_ID,
    MEETING_INVITATION_TESTING_WORKFLOW_ID,
)
from src.backend.services.workflow_materialisation_diagnostics_service import (
    TESTING_TYPE_CONCEPT_IDS,
    build_workflow_concept_parity_audit,
    build_workflow_materialisation_diagnostics,
)


def test_workflow_materialisation_diagnostics_classifies_partial_bootstrap(
    monkeypatch,
) -> None:
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "_get_environment_provenance",
        lambda: {
            "configured_database_name": "von_db",
            "running_under_pytest": False,
            "looks_like_test_database_name": False,
            "looks_like_noncanonical_database": False,
            "concept_collection_available": True,
            "concept_collection_count": 154,
            "concept_collection_empty": False,
            "reason_codes": [],
        },
    )
    monkeypatch.setattr(
        service,
        "_get_runtime_durable_workflow_snapshots",
        lambda: {
            "available": True,
            "startup_status": {
                "state": "ready",
                "ready": True,
                "workflow_bootstrap_summary": {
                    "testing_workflow_bootstrap": {"success": True}
                },
            },
            "workflow_components": {
                "testing_workflow_bootstrap": {"success": True},
                "workflow_authority_bootstrap": {"success": True},
            },
        },
    )
    monkeypatch.setattr(
        service,
        "_get_parity_inventory_snapshot",
        lambda: {
            "available": True,
            "snapshot": {
                "build_state": "ready",
                "diagnostics": {"reason_codes": []},
                "workflow_authority": {
                    "counts": {"missing_concepts": 0, "missing_required_type": 0},
                    "missing_concept_workflow_ids": [],
                    "missing_required_type_by_workflow_id": {},
                },
            },
        },
    )

    def _lookup_exact_concept(concept_id: str) -> dict[str, object]:
        missing = {EPHEMERAL_THEORY_TYPE_ID, EXPERIMENT_SPEC_TYPE_ID}
        if concept_id in missing:
            return {
                "concept_id": concept_id,
                "exists": False,
                "lookup_mode": "exact_concept_id",
                "instance_of": [],
                "reason_codes": ["concept_missing"],
            }
        return {
            "concept_id": concept_id,
            "exists": True,
            "lookup_mode": "exact_concept_id",
            "instance_of": (
                ["#V#workflow"]
                if concept_id == MEETING_INVITATION_TESTING_WORKFLOW_ID
                else []
            ),
            "reason_codes": [],
        }

    monkeypatch.setattr(service, "_lookup_exact_concept", _lookup_exact_concept)

    payload = build_workflow_materialisation_diagnostics(
        required_concept_ids=[
            EPHEMERAL_THEORY_TYPE_ID,
            EXPERIMENT_SPEC_TYPE_ID,
            EXPERIMENT_RUN_TYPE_ID,
            MEETING_INVITATION_TESTING_WORKFLOW_ID,
        ]
    )

    assert payload["success"] is True
    assert payload["classification"]["state"] == "partial_bootstrap"
    assert (
        "testing_substrate_partial_bootstrap"
        in payload["classification"]["reason_codes"]
    )
    testing_type_parity = payload["testing_type_parity"]
    assert EPHEMERAL_THEORY_TYPE_ID in testing_type_parity["missing_concept_ids"]
    assert EXPERIMENT_SPEC_TYPE_ID in testing_type_parity["missing_concept_ids"]
    required = {item["concept_id"]: item for item in payload["required_concepts"]}
    assert (
        required[EPHEMERAL_THEORY_TYPE_ID]["diagnostic_state"]
        == "absent_partial_bootstrap"
    )
    assert required[MEETING_INVITATION_TESTING_WORKFLOW_ID]["exists"] is True


def test_workflow_materialisation_diagnostics_classifies_fresh_test_db(
    monkeypatch,
) -> None:
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "_get_environment_provenance",
        lambda: {
            "configured_database_name": "test_von_db",
            "running_under_pytest": True,
            "looks_like_test_database_name": True,
            "looks_like_noncanonical_database": True,
            "concept_collection_available": True,
            "concept_collection_count": 0,
            "concept_collection_empty": True,
            "reason_codes": [
                "running_under_pytest",
                "noncanonical_database_name",
                "test_database_name",
                "concept_collection_empty",
            ],
        },
    )
    monkeypatch.setattr(
        service,
        "_get_runtime_durable_workflow_snapshots",
        lambda: {
            "available": True,
            "startup_status": {"state": "skipped_pytest", "ready": False},
            "workflow_components": {},
        },
    )
    monkeypatch.setattr(
        service,
        "_get_parity_inventory_snapshot",
        lambda: {
            "available": True,
            "snapshot": {
                "build_state": "pending_background_build",
                "diagnostics": {"reason_codes": ["inventory_pending_background_build"]},
                "workflow_authority": {
                    "counts": {"missing_concepts": 0, "missing_required_type": 0},
                    "missing_concept_workflow_ids": [],
                    "missing_required_type_by_workflow_id": {},
                },
            },
        },
    )
    monkeypatch.setattr(
        service,
        "_lookup_exact_concept",
        lambda concept_id: {
            "concept_id": concept_id,
            "exists": False,
            "lookup_mode": "exact_concept_id",
            "instance_of": [],
            "reason_codes": ["concept_missing"],
        },
    )

    payload = build_workflow_materialisation_diagnostics(
        required_concept_ids=[EPHEMERAL_THEORY_TYPE_ID],
        include_present_concepts=False,
    )

    assert payload["success"] is True
    assert payload["classification"]["state"] == "fresh_or_test_db"
    assert set(payload["classification"]["reason_codes"]) >= {
        "running_under_pytest",
        "noncanonical_database_name",
        "concept_collection_empty",
    }
    assert payload["environment"]["configured_database_name"] == "test_von_db"
    assert payload["required_concepts"][0]["diagnostic_state"] == (
        "absent_environment_state"
    )
    assert payload["testing_type_parity"]["required_concept_ids"] == list(
        TESTING_TYPE_CONCEPT_IDS
    )


def test_workflow_concept_parity_audit_summarises_state_counts(monkeypatch) -> None:
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "build_workflow_materialisation_diagnostics",
        lambda **_kwargs: {
            "success": True,
            "generated_at_utc": "2026-04-07T00:00:00+00:00",
            "required_concept_ids": [
                "#V#alpha_workflow",
                "#V#beta_workflow",
                "#V#gamma_workflow",
            ],
            "classification": {
                "state": "missing_authority",
                "ready_for_authoritative_checks": True,
            },
            "environment": {"configured_database_name": "von_db"},
            "durable_workflow_startup": {"state": "ready"},
            "workflow_bootstrap": {"summary": {"testing_workflow_bootstrap": {"success": True}}},
            "parity_inventory": {"build_state": "ready"},
            "testing_type_parity": {"missing_concept_ids": []},
            "required_concepts": [
                {
                    "concept_id": "#V#alpha_workflow",
                    "exists": True,
                    "diagnostic_state": "present",
                },
                {
                    "concept_id": "#V#beta_workflow",
                    "exists": False,
                    "diagnostic_state": "absent_missing_authority",
                },
                {
                    "concept_id": "#V#gamma_workflow",
                    "exists": True,
                    "diagnostic_state": "authority_drift",
                },
            ],
            "errors": [],
        },
    )

    payload = build_workflow_concept_parity_audit(
        concept_ids=["#V#alpha_workflow", "#V#beta_workflow", "#V#gamma_workflow"]
    )

    assert payload["success"] is True
    assert payload["schema_version"] == "workflow_concept_parity_audit.v1"
    assert payload["summary"] == {
        "audited_count": 3,
        "present_count": 2,
        "missing_count": 1,
        "authority_drift_count": 1,
        "diagnostic_state_counts": {
            "present": 1,
            "absent_missing_authority": 1,
            "authority_drift": 1,
        },
        "classification_state": "missing_authority",
        "ready_for_authoritative_checks": True,
    }
    assert payload["concepts"][1]["concept_id"] == "#V#beta_workflow"
