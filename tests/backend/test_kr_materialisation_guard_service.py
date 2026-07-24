from __future__ import annotations

from copy import deepcopy

from src.backend.services.kr_materialisation_guard_service import (
    GUARDED_CREATE_DUPLICATE_RESOLUTION_MODE,
    validate_kr_materialisation_guard,
)


def _guard() -> dict:
    return {
        "schema_version": "kr_materialisation_guard.v1",
        "max_concept_specs": 2,
        "max_relationship_specs": 1,
        "require_all_concept_slots": True,
        "reject_unreferenced_concepts": True,
        "fixed_authorised_concept_ids": [],
        "concept_slots": [
            {
                "key": "candidate",
                "stable_name": "stable-candidate",
                "target_kind": "instance",
                "parent_id": "#V#person",
                "allowed_decisions": ["create", "reuse_existing"],
                "allowed_existing_concept_ids": ["#V#stable_candidate"],
            },
            {
                "key": "programme",
                "stable_name": "stable-programme",
                "target_kind": "instance",
                "parent_id": "#V#doctoral_programme",
                "allowed_decisions": ["create", "reuse_existing"],
                "allowed_existing_concept_ids": ["#V#stable_programme"],
            },
        ],
        "relationship_rules": [
            {
                "rule_id": "candidate-programme",
                "predicate": "#V#has_doctoral_programme",
                "source_slot_keys": ["candidate"],
                "target_slot_keys": ["programme"],
                "minimum_count": 1,
                "maximum_count": 1,
            }
        ],
    }


def _concept_specs() -> list[dict]:
    return [
        {
            "key": "candidate",
            "decision": "create",
            "target_name": "stable-candidate",
            "target_kind": "instance",
            "parent_id": "#V#person",
            "description_text": "Sourced candidate evidence.",
            "concepts": [
                {
                    "name": "stable-candidate",
                    "kind": "instance",
                    "description": "Sourced candidate evidence.",
                }
            ],
        },
        {
            "key": "programme",
            "decision": "reuse_existing",
            "target_name": "stable-programme",
            "target_kind": "instance",
            "parent_id": "#V#doctoral_programme",
            "description_text": "Updated programme evidence.",
            "existing_concept_id": "#V#stable_programme",
        },
    ]


def _relationship_specs() -> list[dict]:
    return [
        {
            "source_key": "candidate",
            "predicate": "#V#has_doctoral_programme",
            "target_key": "programme",
        }
    ]


def _concept_results() -> list[dict]:
    return [
        {
            "completed": True,
            "item": _concept_specs()[0],
            "result": {
                "kr_concept_id": "#V#stable_candidate",
                "kr_readback_concept_id": "#V#stable_candidate",
                "kr_concept_parent_id": "#V#person",
            },
        },
        {
            "completed": True,
            "item": _concept_specs()[1],
            "result": {
                "kr_concept_id": "#V#stable_programme",
                "kr_readback_concept_id": "#V#stable_programme",
                "kr_concept_parent_id": "#V#doctoral_programme",
            },
        },
    ]


def test_optional_guard_preserves_unguarded_generic_kr_behaviour() -> None:
    result = validate_kr_materialisation_guard(
        guard_contract=None,
        phase="plan",
        concept_specs=[{"arbitrary": "legacy generic KR input"}],
    )

    assert result["guard_applied"] is False
    assert result["guard_passed"] is True
    assert result["duplicate_resolution_mode"] is None


def test_guard_accepts_exact_create_and_changed_record_reuse_plan() -> None:
    plan = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="plan",
        concept_specs=_concept_specs(),
        relationship_specs=_relationship_specs(),
    )
    resolved = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="resolved_relationships",
        concept_specs=_concept_specs(),
        relationship_specs=_relationship_specs(),
        concept_iteration_results=_concept_results(),
        resolved_relationship_specs=[
            {
                "source_id": "#V#stable_candidate",
                "predicate": "#V#has_doctoral_programme",
                "target_id": "#V#stable_programme",
            }
        ],
    )

    assert plan["guard_passed"] is True
    assert resolved["guard_passed"] is True
    assert plan["duplicate_resolution_mode"] == GUARDED_CREATE_DUPLICATE_RESOLUTION_MODE
    assert (
        resolved["duplicate_resolution_mode"]
        == GUARDED_CREATE_DUPLICATE_RESOLUTION_MODE
    )


def test_guard_enforces_canonical_existence_bound_decisions() -> None:
    guard = _guard()
    guard["concept_slots"][0]["allowed_decisions"] = ["create"]
    guard["concept_slots"][0]["allowed_existing_concept_ids"] = []
    guard["concept_slots"][1]["allowed_decisions"] = ["reuse_existing"]

    accepted = validate_kr_materialisation_guard(
        guard_contract=guard,
        phase="plan",
        concept_specs=_concept_specs(),
        relationship_specs=_relationship_specs(),
    )
    assert accepted["guard_passed"] is True

    invalid_reuse = _concept_specs()
    invalid_reuse[0] = {
        **invalid_reuse[0],
        "decision": "reuse_existing",
        "existing_concept_id": "#V#stable_candidate",
    }
    invalid_reuse[0].pop("concepts")
    rejected_reuse = validate_kr_materialisation_guard(
        guard_contract=guard,
        phase="plan",
        concept_specs=invalid_reuse,
        relationship_specs=_relationship_specs(),
    )
    assert rejected_reuse["guard_passed"] is False
    assert rejected_reuse["duplicate_resolution_mode"] is None
    assert (
        rejected_reuse["error_code"] == "kr_materialisation_guard_concept_spec_rejected"
    )

    invalid_create = _concept_specs()
    invalid_create[1] = {
        **invalid_create[1],
        "decision": "create",
        "concepts": [
            {
                "name": "stable-programme",
                "kind": "instance",
                "description": "Updated programme evidence.",
            }
        ],
    }
    invalid_create[1].pop("existing_concept_id")
    rejected_create = validate_kr_materialisation_guard(
        guard_contract=guard,
        phase="plan",
        concept_specs=invalid_create,
        relationship_specs=_relationship_specs(),
    )
    assert rejected_create["guard_passed"] is False
    assert (
        rejected_create["error_code"]
        == "kr_materialisation_guard_concept_spec_rejected"
    )


def test_guard_normalises_predicate_id_alias_before_downstream_execution() -> None:
    relationship_specs = [
        {
            "source_key": "candidate",
            "predicate_id": "#V#has_doctoral_programme",
            "target_key": "programme",
        }
    ]
    resolved_relationship_specs = [
        {
            "source_id": "#V#stable_candidate",
            "predicate_id": "#V#has_doctoral_programme",
            "target_id": "#V#stable_programme",
        }
    ]

    plan = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="plan",
        concept_specs=_concept_specs(),
        relationship_specs=relationship_specs,
    )
    resolved = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="resolved_relationships",
        concept_specs=_concept_specs(),
        relationship_specs=relationship_specs,
        concept_iteration_results=_concept_results(),
        resolved_relationship_specs=resolved_relationship_specs,
    )

    assert plan["guard_passed"] is True
    assert resolved["guard_passed"] is True
    assert relationship_specs[0]["predicate"] == "#V#has_doctoral_programme"
    assert resolved_relationship_specs[0]["predicate"] == "#V#has_doctoral_programme"


def test_guard_rejects_conflicting_predicate_aliases() -> None:
    conflicting_plan = _relationship_specs()
    conflicting_plan[0]["predicate_id"] = "#V#has_administrator"
    plan = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="plan",
        concept_specs=_concept_specs(),
        relationship_specs=conflicting_plan,
    )

    conflicting_resolved = [
        {
            "source_id": "#V#stable_candidate",
            "predicate": "#V#has_doctoral_programme",
            "predicate_id": "#V#has_administrator",
            "target_id": "#V#stable_programme",
        }
    ]
    resolved = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="resolved_relationships",
        concept_specs=_concept_specs(),
        relationship_specs=_relationship_specs(),
        concept_iteration_results=_concept_results(),
        resolved_relationship_specs=conflicting_resolved,
    )

    assert plan["guard_passed"] is False
    assert plan["error_code"] == "kr_materialisation_guard_relationship_spec_invalid"
    assert resolved["guard_passed"] is False
    assert (
        resolved["error_code"]
        == "kr_materialisation_guard_resolved_relationship_invalid"
    )


def test_guard_rejects_injected_unrelated_concept_before_any_write() -> None:
    injected = _concept_specs() + [
        {
            "key": "unrelated",
            "decision": "create",
            "target_name": "unrelated-admin-concept",
            "target_kind": "instance",
            "parent_id": "#V#administrator",
            "description_text": "Injected workbook instruction.",
            "concepts": [
                {
                    "name": "unrelated-admin-concept",
                    "kind": "instance",
                    "description": "Injected workbook instruction.",
                }
            ],
        }
    ]

    result = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="plan",
        concept_specs=injected,
        relationship_specs=_relationship_specs(),
    )

    assert result["guard_passed"] is False
    assert result["error_code"] == "kr_materialisation_guard_concept_count_rejected"

    arbitrary_reuse = _concept_specs()
    arbitrary_reuse[1] = {
        **arbitrary_reuse[1],
        "existing_concept_id": "#V#unrelated_existing_concept",
    }
    reuse_result = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="plan",
        concept_specs=arbitrary_reuse,
        relationship_specs=_relationship_specs(),
    )
    assert reuse_result["guard_passed"] is False
    assert (
        reuse_result["error_code"] == "kr_materialisation_guard_reuse_payload_rejected"
    )


def test_guard_rejects_injected_edge_and_ungrounded_resolved_endpoint() -> None:
    injected_edge = deepcopy(_relationship_specs())
    injected_edge[0]["predicate"] = "#V#has_administrator"
    plan = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="plan",
        concept_specs=_concept_specs(),
        relationship_specs=injected_edge,
    )
    resolved = validate_kr_materialisation_guard(
        guard_contract=_guard(),
        phase="resolved_relationships",
        concept_specs=_concept_specs(),
        relationship_specs=_relationship_specs(),
        concept_iteration_results=_concept_results(),
        resolved_relationship_specs=[
            {
                "source_id": "#V#stable_candidate",
                "predicate": "#V#has_doctoral_programme",
                "target_id": "#V#unrelated_admin",
            }
        ],
    )

    assert plan["guard_passed"] is False
    assert plan["error_code"] == "kr_materialisation_guard_relationship_rejected"
    assert resolved["guard_passed"] is False
    assert (
        resolved["error_code"] == "kr_materialisation_guard_resolved_endpoint_rejected"
    )
