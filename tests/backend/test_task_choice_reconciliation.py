"""Report-only choice recovery must never erase receipts or unrelated failures."""

from copy import deepcopy
import pytest
from src.backend.services.adaptive_turn_service import (
    _reconcile_effect_attempts_by_postcondition,
)


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "wrong_id",
        "missing_reason",
        "unrelated_field",
        "changed",
        "unknown_change",
        "unverified",
        "wrong_readback",
        "different_work",
        "different_scope",
    ],
)
def test_choice_reconciliation_requires_exact_no_change_attempt_and_verified_replacement(
    invalid,
):
    args = {
        "title": "Investigate",
        "description": "Find the defect",
        "organisation_concept_id": "#V#lab",
        "assignee_concept_id": "#V#manager",
        "report_to_concept_id": None,
    }
    recovery = {
        "failed_effect_id": "failed",
        "revised_choice_fields": ["assignee_concept_id"],
        "reason": "My choice, not a user constraint",
    }
    invocations = [
        {"tool": "task_create", "effect_id": "failed", "effective_arguments": args},
        {
            "tool": "task_create",
            "effect_id": "success",
            "effective_arguments": {**args, "assignee_concept_id": "#V#worker"},
            "payload": {"task_creation_recovery": recovery},
        },
    ]
    states = {
        "failed": {
            "effect_status": "failed",
            "changed": False,
            "failure_fact": {"error_code": "task_assignment_scope_denied"},
        },
        "success": {
            "effect_status": "succeeded",
            "changed": True,
            "canonical_readback": {
                "verified": True,
                "task_concept_id": "#V#task",
                "task_fields": {
                    "assignee_concept_id": "#V#worker",
                    "report_to_concept_id": None,
                },
            },
        },
    }
    if invalid == "wrong_id":
        recovery["failed_effect_id"] = "some-other-attempt"
    if invalid == "missing_reason":
        recovery["reason"] = " "
    if invalid == "unrelated_field":
        recovery["revised_choice_fields"] = ["description"]
    if invalid == "changed":
        states["failed"]["changed"] = True
    if invalid == "unknown_change":
        states["failed"]["changed"] = None
    if invalid == "unverified":
        states["success"]["canonical_readback"]["verified"] = False
    if invalid == "wrong_readback":
        states["success"]["canonical_readback"]["task_fields"][
            "assignee_concept_id"
        ] = "#V#other"
    if invalid == "different_work":
        invocations[1]["effective_arguments"]["description"] = "Unrelated task"
    if invalid == "different_scope":
        invocations[1]["effective_arguments"][
            "organisation_concept_id"
        ] = "#V#other_org"
    original = deepcopy(states)
    result = _reconcile_effect_attempts_by_postcondition(
        tool_invocations=invocations, effect_snapshot=states
    )
    assert states == original
    assert bool(result["failed"].get("recovered_by_effect_id")) is (invalid is None)
