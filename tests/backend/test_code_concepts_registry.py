from __future__ import annotations

from src.backend.vontology.code_concepts_registry import (
    PREDICATE_TYPE_ID,
    MENTIONED_IN_VON_CODE_ID,
    build_virtual_concept_doc,
    list_code_predicate_ids,
)


def test_background_launch_policy_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#hasBackgroundLaunchPolicyJson" in ids
    assert "#V#has_background_launch_policy_json" in ids
    assert "#V#hasMinimumBackgroundLaunchIntervalSeconds" in ids
    assert "#V#hasMinimumBackgroundLaunchIntervalMinutes" in ids


def test_background_launch_policy_virtual_doc_is_predicate_instance():
    doc = build_virtual_concept_doc("#V#hasBackgroundLaunchPolicyJson")

    assert doc is not None
    assert doc.get("concept_id") == "#V#hasBackgroundLaunchPolicyJson"
    inst_of = (
        doc.get("relationships", {}).get("is_an_instance_of", [])
        if isinstance(doc, dict)
        else []
    )
    assert PREDICATE_TYPE_ID in inst_of
    assert MENTIONED_IN_VON_CODE_ID in inst_of


def test_workflow_runtime_policy_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#onApprovalRequiredNextStep" in ids
    assert "#V#on_approval_required_next_step" in ids
    assert "#V#hasWorkflowStepRetryPolicyJson" in ids
    assert "#V#has_workflow_step_retry_policy_json" in ids
    assert "#V#hasWorkflowStepApprovalGateJson" in ids
    assert "#V#has_workflow_step_approval_gate_json" in ids
    assert "#V#hasWorkflowStepIdempotencyPolicyJson" in ids
    assert "#V#has_workflow_step_idempotency_policy_json" in ids
