from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.backend.services import operational_certification_vontology_service as service
from src.backend.services.prompt_template_service import PromptTemplateService
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.trace_model import WorkflowExecutionTrace
from src.backend.workflows.workflow_concept_authority_service import (
    build_repo_seed_workflow_definitions,
)


def test_operational_absence_probe_seed_is_read_only_and_deterministic() -> None:
    bundle = json.loads(service._WORKFLOW_BUNDLE_PATH.read_text(encoding="utf-8"))
    workflows = {workflow["workflow_id"]: workflow for workflow in bundle["workflows"]}

    assert bundle["seed_version"] == "6"
    assert bundle["known_legacy_authority_payload_sha256_by_seed_version"][
        service.OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID
    ] == {
        "4": [
            "7b3220522c36640eaded425fdcca04646abffcbdd8d4f664c0060af339cf52ff"
        ]
    }
    probe = workflows[service.OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID]
    steps = probe["publication_spec"]["steps"]
    action_ids = [step.get("action_id") for step in steps if step.get("action_id")]
    assert action_ids == [
        "workflow_control.context_template",
        "workflow_mcp.invoke_tool",
        "workflow_control.context_set",
        "workflow_control.context_set",
        "workflow_control.context_project",
    ]
    assert "llm.action" not in action_ids
    assert all(step.get("mutation_authority") is None for step in steps)
    resolve_step = next(
        step for step in steps if step["state_id"] == "resolve_target_name"
    )
    assert [
        binding[1]
        for binding in resolve_step["static_input_bindings"]
        if binding[0] == "tool_name"
    ] == ["resolve_concept_by_name"]
    project_step = next(
        step for step in steps if step["state_id"] == "project_probe_result"
    )
    assert project_step["writes_context_keys"] == [
        "represented_operational_state_probe_result"
    ]
    mark_absent = next(step for step in steps if step["state_id"] == "mark_absent")
    assignments = dict(mark_absent["static_input_bindings"])["assignments"]
    probe_evidence = next(
        item["value"] for item in assignments if item["key"] == "probe_evidence"
    )[0]
    assert probe_evidence == {
        "schema_version": "operational_absence_probe_resolution_lineage.v1",
        "kind": "canonical_exact_name_resolution",
        "tool": "resolve_concept_by_name",
        "target_name": {"$context_key": "target_name"},
        "status": {"$context_key": "resolution_status"},
        "resolved_concept_id": {"$context_key": "resolved_concept_id"},
        "candidates": {"$context_key": "resolution_candidates"},
    }


def test_operational_absence_probe_projects_actual_resolver_result_lineage() -> None:
    definition = build_repo_seed_workflow_definitions(
        bundle_paths=[service._WORKFLOW_BUNDLE_PATH],
        target_workflow_ids=[service.OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID],
    )[service.OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID]
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)
    registry.register(
        ActionSpec(
            action_id="workflow_mcp.invoke_tool",
            handler=lambda _request: WorkflowActionResult(
                status="success",
                outputs={
                    "status": "not_found",
                    "resolved_concept_id": None,
                    "candidates": [],
                    "mcp_resolved_tool": "resolve_concept_by_name",
                },
            ),
        )
    )
    trace = WorkflowExecutionTrace(
        workflow_id=service.OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID,
        user_namespace="test-namespace",
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        definition,
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            model="none",
            user_namespace="test-namespace",
        ),
        data={
            "isolation_id": "isolation-123",
            "namespace": "test-namespace",
        },
        trace=trace,
    )

    assert result.completed is True
    probe_result = result.data["represented_operational_state_probe_result"]
    assert probe_result["target_absent"] is True
    assert probe_result["evidence"] == [
        {
            "schema_version": "operational_absence_probe_resolution_lineage.v1",
            "kind": "canonical_exact_name_resolution",
            "tool": "resolve_concept_by_name",
            "target_name": "Operational certification isolation-123",
            "status": "not_found",
            "resolved_concept_id": None,
            "candidates": [],
        }
    ]
    action_ids = [action["action_id"] for action in trace.actions]
    assert "workflow_mcp.invoke_tool" in action_ids
    assert "workflow_control.context_project" in action_ids


def test_operational_evaluator_loads_bounded_experience_context_before_judging() -> None:
    bundle = json.loads(service._WORKFLOW_BUNDLE_PATH.read_text(encoding="utf-8"))
    workflows = {workflow["workflow_id"]: workflow for workflow in bundle["workflows"]}

    assert "workflow_invoke_subworkflow" in bundle["supported_action_ids"]
    evaluator = workflows[service.OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID]
    publication = evaluator["publication_spec"]
    assert publication["initial_state"] == "workflow_experience_context_prelude"

    steps = {step["state_id"]: step for step in publication["steps"]}
    prelude = steps["workflow_experience_context_prelude"]
    assert prelude["action_id"] == "workflow_invoke_subworkflow"
    assert prelude["execution_mode"] == "subworkflow"
    assert prelude["invoked_workflow_id"] == "#V#workflow_experience_context_prelude"
    assert {item["key"]: item["value"] for item in prelude["static_input_bindings"]} == {
        "workflow_experience_target_workflow_id": (
            service.OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID
        ),
        "failure_mode": "capture",
        "max_transitions": 10,
    }
    assert prelude["conditional_transitions"] == [
        {
            "to_state": "evaluate_trial",
            "reason": "workflow_experience_context_loaded",
            "condition_spec": {"kind": "always"},
        }
    ]

    expected_output_mappings = {
        "result.workflow_success_guidance_history": (
            "workflow_success_guidance_history"
        ),
        "result.workflow_failure_avoidance_history": (
            "workflow_failure_avoidance_history"
        ),
        "result.workflow_low_imposition_exploration_history": (
            "workflow_low_imposition_exploration_history"
        ),
        "result.workflow_experience_profile_concept_id": (
            "workflow_experience_profile_concept_id"
        ),
    }
    assert {
        mapping["tool_output_field"]: mapping["context_key"]
        for mapping in prelude["tool_output_mapping_specs"]
    } == expected_output_mappings
    assert set(prelude["writes_context_keys"]) == set(expected_output_mappings.values())

    evaluate_context_fields = {
        item["context_key"]: item["label"]
        for item in steps["evaluate_trial"]["llm_policy"]["context_fields"]
    }
    assert set(expected_output_mappings.values()) <= set(evaluate_context_fields)
    assert "soft hints" in evaluate_context_fields["workflow_success_guidance_history"]
    assert "never treat guidance as trial evidence" in evaluate_context_fields[
        "workflow_failure_avoidance_history"
    ]
    assert "must not weaken evaluation checks" in evaluate_context_fields[
        "workflow_low_imposition_exploration_history"
    ]


def test_operational_evaluator_projects_experience_guidance_into_llm_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = build_repo_seed_workflow_definitions(
        bundle_paths=[service._WORKFLOW_BUNDLE_PATH],
        target_workflow_ids=[service.OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID],
    )[service.OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID]
    guidance = {
        "workflow_success_guidance_history": [{"text": "Reuse typed receipts."}],
        "workflow_failure_avoidance_history": [
            {"text": "Do not infer success from final wording."}
        ],
        "workflow_low_imposition_exploration_history": [
            {"text": "Inspect the persisted trace before requesting more evidence."}
        ],
        "workflow_experience_profile_concept_id": "#V#evaluator_model_profile",
    }
    invocation_inputs: dict[str, object] = {}

    def _experience_prelude(request):
        invocation_inputs.update(request.inputs)
        return WorkflowActionResult(status="success", outputs={"result": guidance})

    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="workflow_invoke_subworkflow",
            handler=_experience_prelude,
        )
    )
    represented_result = {
        "schema_version": "represented_operational_evaluator_result.v1",
        "evaluator_id": service.OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID,
        "scenario_id": "scenario-a",
        "trial_index": 1,
        "verdict": "pass",
        "terminal_state": "verified",
        "typed_terminal_outcome_available": True,
        "causal_stage_evidence_complete": True,
        "explicitly_inconclusive": False,
        "recoverable_fault_recovered": False,
        "evidence": [],
        "fabricated_evidence": [],
        "forbidden_effects": [],
        "namespace_violations": [],
        "false_success_claims": [],
        "reason": "Verified from represented evidence.",
    }
    llm_client = MagicMock()
    llm_client.generate.return_value = json.dumps(represented_result)
    monkeypatch.setattr(
        PromptTemplateService,
        "resolve_prompt_text",
        lambda _self, concept_ids, **_kwargs: (
            list(concept_ids)[0],
            "Represented evaluator prompt.",
        ),
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        evaluator,
        environment=WorkflowEnvironment(llm_client=llm_client, model="gpt-test"),
        data={
            "scenario_contract": {"scenario_id": "scenario-a"},
            "trial_index": 1,
            "trial_observation": {"execution_trace_id": "trace-a"},
        },
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert invocation_inputs == {
        "workflow_id": "#V#workflow_experience_context_prelude",
        "workflow_experience_target_workflow_id": (
            service.OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID
        ),
        "failure_mode": "capture",
        "max_transitions": 10,
        "requested_model": None,
    }
    assert result.data["workflow_success_guidance_history"] == guidance[
        "workflow_success_guidance_history"
    ]
    prompt = llm_client.generate.call_args.args[0]
    assert "Reuse typed receipts." in prompt
    assert "Do not infer success from final wording." in prompt
    assert "Inspect the persisted trace before requesting more evidence." in prompt
    assert "#V#evaluator_model_profile" in prompt


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


def test_forced_prompt_seed_revalidates_post_write_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = [
        {
            "errors_by_target": {
                service.OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID: [
                    "prompt_content_missing"
                ]
            }
        },
        {"errors_by_target": {}, "validated_prompt_ids": ["prompt"]},
    ]
    calls: list[object] = []

    def _ensure(**_kwargs):
        calls.append(object())
        return reports.pop(0)

    monkeypatch.setattr(service, "ensure_prompt_concept_support", _ensure)
    monkeypatch.setattr(
        service,
        "prompt_concept_has_content",
        lambda _concept_id: True,
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **_kwargs: {"success": True},
    )

    result = service._ensure_evaluator_prompt(force_prompt_seed=True)

    assert len(calls) == 2
    assert result["seeded"] is True
    assert result["content_ready"] is True
    assert result["errors_by_target"] == {}
    assert result["success"] is True
