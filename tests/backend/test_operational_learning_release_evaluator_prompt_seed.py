from __future__ import annotations

import json
from pathlib import Path


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
