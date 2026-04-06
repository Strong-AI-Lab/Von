from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.experiment_run_service import (
    create_experiment_spec,
    start_experiment_run,
)
from src.backend.services.testing_theory_service import (
    assert_testing_theory_local_claims,
    create_testing_theory_slice,
    garbage_collect_expired_testing_theories,
    get_testing_theory_state,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass
    yield


def _seed_minimal_base_types() -> None:
    concept_service.create_concept(
        name="Thing",
        concept_id="#V#thing",
        description="Minimal root type for testing bootstrap.",
        parent_concept_ids=[],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )
    concept_service.create_concept(
        name="Information",
        concept_id="#V#information",
        description="Minimal information type for testing bootstrap.",
        parent_concept_ids=["#V#thing"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )
    concept_service.create_concept(
        name="Event",
        concept_id="#V#event",
        description="Minimal event type for testing bootstrap.",
        parent_concept_ids=["#V#thing"],
        create_as_instance=False,
        visibility_scope_mode="global_general",
    )


def test_create_testing_theory_slice_bootstraps_testing_types(_reset_mock_db: Any) -> None:
    _seed_minimal_base_types()

    result = create_testing_theory_slice(
        name="Capability sandbox slice",
        theory_id="#V#ephemeral_theory_capability_sandbox",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
    )

    assert result["success"] is True
    assert concept_service.get_concept_by_concept_id("#V#ephemeral_theory") is not None
    assert (
        concept_service.get_concept_by_concept_id(
            "#V#ephemeral_theory_capability_sandbox"
        )
        is not None
    )


def test_experiment_run_services_bootstrap_testing_types(_reset_mock_db: Any) -> None:
    _seed_minimal_base_types()

    created = create_experiment_spec(
        name="Capability sandbox spec",
        experiment_spec_id="#V#experiment_spec_capability_sandbox",
        namespace="#V#user@org",
        target_workflow_ids=["#V#candidate_workflow"],
    )
    started = start_experiment_run(
        experiment_spec_id="#V#experiment_spec_capability_sandbox",
        run_id="#V#experiment_run_capability_sandbox",
    )

    assert created["success"] is True
    assert started["success"] is True
    assert concept_service.get_concept_by_concept_id("#V#experiment_spec") is not None
    assert concept_service.get_concept_by_concept_id("#V#experiment_run") is not None


def test_testing_theory_gc_expires_only_elapsed_theories(_reset_mock_db: Any) -> None:
    _seed_minimal_base_types()

    expired = create_testing_theory_slice(
        name="Expired capability slice",
        theory_id="#V#expired_capability_slice",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        ttl_seconds=1,
    )
    retained = create_testing_theory_slice(
        name="Retained capability slice",
        theory_id="#V#retained_capability_slice",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        ttl_seconds=7200,
    )
    assert expired["success"] is True
    assert retained["success"] is True

    assert_testing_theory_local_claims(
        theory_id="#V#expired_capability_slice",
        claims=[
            {
                "source_id": "#V#expired_capability_slice",
                "predicate": "#V#has_hypothesis",
                "target": "expired claim",
                "target_kind": "text",
            }
        ],
    )
    assert_testing_theory_local_claims(
        theory_id="#V#retained_capability_slice",
        claims=[
            {
                "source_id": "#V#retained_capability_slice",
                "predicate": "#V#has_hypothesis",
                "target": "retained claim",
                "target_kind": "text",
            }
        ],
    )

    gc_result = garbage_collect_expired_testing_theories(
        now_utc=(datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
        limit=10,
    )

    assert gc_result["success"] is True
    assert gc_result["expired_theory_ids"] == ["#V#expired_capability_slice"]
    assert gc_result["retained_theory_ids"] == ["#V#retained_capability_slice"]

    expired_state = get_testing_theory_state("#V#expired_capability_slice")
    retained_state = get_testing_theory_state("#V#retained_capability_slice")
    assert expired_state is not None
    assert retained_state is not None
    assert expired_state["lifecycle_state"] == "expired"
    assert expired_state["local_assertions"][0]["status"] == "expired"
    assert retained_state["lifecycle_state"] == "active"
    assert retained_state["local_assertions"][0]["status"] == "proposed"
