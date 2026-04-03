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


def test_workflow_routing_profile_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#hasWorkflowRoutingProfileJson" in ids
    assert "#V#has_workflow_routing_profile_json" in ids


def test_minimal_imposition_runtime_profile_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#has_minimal_imposition_runtime_profile" in ids
    assert "#V#has_minimal_imposition_runtime_profile_json" in ids


def test_workflow_discovery_exemplar_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#hasWorkflowDiscoveryExemplarsJson" in ids
    assert "#V#has_workflow_discovery_exemplars_json" in ids


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
    assert "#V#hasWorkflowStepCheckpointPolicyJson" in ids
    assert "#V#has_workflow_step_checkpoint_policy_json" in ids
    assert "#V#hasWorkflowStepMutationAuthorityJson" in ids
    assert "#V#has_workflow_step_mutation_authority_json" in ids
    assert "#V#hasWorkflowPlanStatePolicyJson" in ids
    assert "#V#has_workflow_plan_state_policy_json" in ids
    assert "#V#hasWorkflowCompletionGateJson" in ids
    assert "#V#has_workflow_completion_gate_json" in ids


def test_workflow_prompt_contract_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#workflow_step_uses_llm_prompt" in ids
    assert "#V#hasPromptName" in ids
    assert "#V#has_prompt_name" in ids
    assert "#V#usesAgentProfile" in ids
    assert "#V#usesModelPreference" in ids
    assert "#V#allowsTool" in ids
    assert "#V#hasPromptScope" in ids
    assert "#V#hasPromptVariables" in ids
    assert "#V#hasPromptSource" in ids
    assert "#V#hasToolResolutionPriority" in ids


def test_skill_interop_predicates_are_registered():
    ids = set(list_code_predicate_ids())

    assert "#V#has_skill_name" in ids
    assert "#V#has_skill_description" in ids
    assert "#V#has_skill_argument_hint" in ids
    assert "#V#is_user_invokable" in ids
    assert "#V#disables_model_invocation" in ids
    assert "#V#has_skill_source_scope" in ids
    assert "#V#has_skill_discovery_location" in ids
