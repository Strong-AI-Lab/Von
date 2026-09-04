from __future__ import annotations

import json
from pathlib import Path

from src.backend.services import workflow_repo_seed_bootstrap as seed_bootstrap
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.workflow_launch_input_contracts import (
    normalise_workflow_launch_input_contract,
)


_SEED_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
)
_PROMPT_SEED_PATH = _SEED_DIR / "operational_learning_release_evaluator_prompt_seed.md"
_WORKFLOW_SEED_PATH = (
    _SEED_DIR / "operational_learning_release_authority_workflow_seed_bundle.json"
)


def _release_evaluator_workflow() -> dict:
    bundle = json.loads(_WORKFLOW_SEED_PATH.read_text(encoding="utf-8"))
    return next(
        workflow
        for workflow in bundle["workflows"]
        if workflow["workflow_id"]
        == "#V#operational_learning_release_evaluator_workflow"
    )


def test_release_evaluator_seed_authority_uses_canonical_launch_contract() -> None:
    raw_bundle = json.loads(_WORKFLOW_SEED_PATH.read_text(encoding="utf-8"))
    assert raw_bundle["seed_version"] == "10"
    assert raw_bundle["known_legacy_authority_payload_sha256_by_seed_version"] == {
        "#V#operational_learning_release_evaluator_workflow": {
            "9": [
                "db13d6e6b85e519a66058a55c8436f11ab3a0874d3cc66735ce28d4e8306a4a0"
            ]
        },
        "#V#operational_learning_candidate_proposal_workflow": {
            "9": [
                "5204bd0bab0cfe4c804695c106f67ca97192fb056442ccc9f330c68c4cc7a34a"
            ]
        },
    }

    workflow_id = "#V#operational_learning_release_evaluator_workflow"
    raw_workflow = next(
        workflow
        for workflow in raw_bundle["workflows"]
        if workflow["workflow_id"] == workflow_id
    )
    raw_contract = raw_workflow["launch_input_contract"]
    canonical_contract, error = normalise_workflow_launch_input_contract(raw_contract)
    assert error is None
    assert canonical_contract is not None
    assert canonical_contract != raw_contract

    bundle = authority_service.load_repo_seed_workflow_bundle(_WORKFLOW_SEED_PATH)
    publication_spec = bundle["publication_specs"][workflow_id]
    workflow_type_ids = tuple(bundle["workflow_type_ids"][workflow_id])
    workflow_text_relations = tuple(bundle["workflow_text_relations"][workflow_id])
    launch_input_contract = bundle["workflow_launch_input_contracts"][workflow_id]
    step_text_relations = {
        step_concept_id: tuple(bundle["step_text_relations"].get(step_concept_id) or ())
        for step_concept_id in authority_service.publication_spec_step_concept_ids(
            workflow_id=workflow_id,
            spec=publication_spec,
        )
        if bundle["step_text_relations"].get(step_concept_id)
    }

    authority_payload = (
        seed_bootstrap._workflow_seed_authority_payload_from_bundle_surfaces(
            workflow_id=workflow_id,
            publication_spec=publication_spec,
            source_workflow_type_ids=workflow_type_ids,
            scoped_workflow_type_ids=workflow_type_ids,
            source_workflow_text_relations=workflow_text_relations,
            scoped_workflow_text_relations=workflow_text_relations,
            source_launch_input_contract=launch_input_contract,
            source_step_text_relations=step_text_relations,
            scoped_step_text_relations=step_text_relations,
        )
    )

    assert authority_payload["launch_input_contract"] == canonical_contract
    assert set(raw_contract["required_inputs"]) <= set(
        authority_payload["launch_input_contract"]["required_inputs"]
    )
    assert {
        "bound_candidate_id",
        "bound_candidate_release_sha256",
        "bound_candidate_namespace",
        "bound_candidate_user_id",
        "bound_candidate_org_id",
        "bound_candidate_context_sha256",
        "bound_experiment_evidence_sha256",
        "bound_certification_evidence_sha256",
    } <= set(authority_payload["launch_input_contract"]["required_inputs"])
    assert seed_bootstrap._stable_payload_sha256(authority_payload) == (
        "12312c4804bdf9684805f86e48b82935845469b55f4954dd053b61c1f8e13a9b"
    )


def test_release_evaluator_uses_transparent_projection_for_llm_context() -> None:
    workflow = _release_evaluator_workflow()
    launch_contract = workflow["launch_input_contract"]
    required_inputs = set(launch_contract["required_inputs"])
    assert {
        "experiment_evidence",
        "certification_evidence",
        "represented_learning_release_evaluator_evidence_projection",
    } <= required_inputs
    mappings = {
        item["target_context_key"]: item["source_expression"]
        for item in launch_contract["input_mappings"]
    }
    assert mappings["represented_learning_release_evaluator_evidence_projection"] == (
        "inputs.represented_learning_release_evaluator_evidence_projection"
    )
    assert mappings["bound_candidate_id"] == (
        "inputs.represented_learning_candidate_context.candidate_id"
    )
    assert mappings["bound_candidate_context_sha256"] == (
        "inputs.represented_learning_candidate_context.context_sha256"
    )
    assert mappings["bound_experiment_evidence_sha256"] == (
        "inputs.represented_learning_release_evaluator_evidence_projection.experiment_evidence.evidence_sha256"
    )
    assert mappings["bound_certification_evidence_sha256"] == (
        "inputs.represented_learning_release_evaluator_evidence_projection.certification_evidence.evidence_sha256"
    )

    evaluate_step = next(
        step
        for step in workflow["publication_spec"]["steps"]
        if step["state_id"] == "evaluate_release"
    )
    context_fields = {
        item["context_key"]: item["label"]
        for item in evaluate_step["llm_policy"]["context_fields"]
    }
    assert "represented_learning_candidate_context" in context_fields
    assert (
        "represented_learning_release_evaluator_evidence_projection" in context_fields
    )
    assert "experiment_evidence" not in context_fields
    assert "certification_evidence" not in context_fields
    assert "authority_revision_sha256" not in context_fields
    assert "authority_prompt_revision_sha256" not in context_fields
    assert "authority_execution_request_id" not in context_fields
    projection_label = context_fields[
        "represented_learning_release_evaluator_evidence_projection"
    ]
    assert "Full canonical wrappers remain in workflow context" in projection_label
    assert "digest-only material proves binding only" in projection_label

    assert evaluate_step["next_state"] == "bind_release_decision"
    assert evaluate_step["validation_policy"]["required_json_fields"] == [
        "schema_version",
        "decision",
        "rationale",
    ]
    output_mappings = {
        item["context_key"]: item["tool_output_field"]
        for item in evaluate_step["tool_output_mapping_specs"]
    }
    assert output_mappings == {
        "represented_learning_release_judgement": "validated_json",
        "represented_learning_release_judgement_decision": ("validated_json.decision"),
        "represented_learning_release_judgement_rationale": (
            "validated_json.rationale"
        ),
    }

    bind_step = next(
        step
        for step in workflow["publication_spec"]["steps"]
        if step["state_id"] == "bind_release_decision"
    )
    assert bind_step["action_id"] == "workflow_control.context_set"
    assignments = dict(bind_step["static_input_bindings"])["assignments"]
    bound_by_key = {item["key"]: item["value"] for item in assignments}
    assert (
        bound_by_key["represented_learning_release_decision"]
        == bound_by_key["workflow_authority_output"]
    )
    decision = bound_by_key["represented_learning_release_decision"]
    assert decision["decision"] == {
        "$context_key": "represented_learning_release_judgement_decision"
    }
    assert decision["candidate_id"] == {"$context_key": "bound_candidate_id"}
    assert decision["authority"]["authority_revision_sha256"] == {
        "$context_key": "authority_revision_sha256"
    }
    assert decision["authority"]["authority_prompt_revision_sha256"] == {
        "$context_key": "authority_prompt_revision_sha256"
    }
    assert decision["experiment_evidence_sha256"] == {
        "$context_key": "bound_experiment_evidence_sha256"
    }
    assert decision["rationale"] == {
        "$context_key": "represented_learning_release_judgement_rationale"
    }


def test_release_evaluator_prompt_treats_digest_only_material_as_unavailable() -> None:
    prompt = _PROMPT_SEED_PATH.read_text(encoding="utf-8")

    assert "represented_learning_release_evaluator_evidence_projection.v1" in prompt
    assert "represented_learning_release_evaluator_digest_only_reference.v1" in (prompt)
    assert "proves only that omitted material was bound" in prompt
    assert "treat the evidence as unavailable and do not promote" in prompt
    assert "Full canonical wrappers remain in workflow context" in prompt
    assert "represented_learning_release_judgement.v1" in prompt
    assert "The next represented workflow step binds" in prompt
    assert "Do not reproduce or invent those hard-interface values" in prompt
