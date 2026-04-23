from __future__ import annotations

from typing import Any

from src.backend.workflows.action_registry import WorkflowActionRequest, WorkflowEnvironment


def _request(data: dict) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id="episode_evaluation.persist_memory",
        inputs={},
        environment=WorkflowEnvironment(llm_client=None, user_namespace="#V#user@org"),
        data=data,
    )


def test_persist_memory_handler_surfaces_remediation_routing(monkeypatch):
    from src.backend.workflows.durable import episode_evaluation_workflow as mod

    monkeypatch.setattr(
        mod,
        "upsert_episode_critique_memory_from_episode_assessment",
        lambda **kwargs: {
            "success": True,
            "memory_id": "#V#episode_critique_memory_abc",
            "state": {
                "memory_id": "#V#episode_critique_memory_abc",
                "namespace": "#V#user@org",
                "improvement_suggestions": [
                    {
                        "suggestion_id": "workflow_change_alpha",
                        "category": "workflow_change",
                        "priority": "high",
                        "target_surface": "workflow",
                        "target_workflow_id": "#V#alpha_workflow",
                        "title": "Repair the workflow route",
                        "rationale": "Another eligible route existed.",
                        "suggested_change": "Tighten routing metadata for the workflow.",
                        "evidence_refs": ["expected_context.routing_quality_signals"],
                        "recursion_level": 0,
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "route_episode_critique_memory",
        lambda **kwargs: {
            "success": True,
            "decision": "task_and_jira",
            "reason_codes": ["repeat_threshold_met", "jira_escalation_threshold_met"],
            "routing_fingerprint": "fp-1610",
            "repeat_count": 3,
            "remediation_task_id": "#V#task_1610",
            "remediation_task_ids": ["#V#task_1610"],
            "remediation_issue_key": "JVNAUTOSCI-1610",
            "remediation_issue_keys": ["JVNAUTOSCI-1610"],
            "task_action": "created_new",
            "jira_action": "created_new",
        },
    )
    monkeypatch.setattr(
        mod,
        "launch_episode_self_improvement_workflows",
        lambda **kwargs: {
            "success": True,
            "launches": [
                {
                    "suggestion_id": "workflow_change_alpha",
                    "target_workflow_id": "#V#alpha_workflow",
                    "launch_workflow_id": "#V#episode_self_improvement_proposal_workflow",
                    "instance_id": "#V#wf_instance_alpha",
                    "success": True,
                    "status": "created",
                }
            ],
        },
    )

    handler = mod._build_persist_memory_handler()
    result = handler(
        _request(
            {
                "episode_evidence_bundle": {"episode_locator": {"request_id": "req-1610"}},
                "critic_assessment": {
                    "summary": "Repeated remediation-worthy finding",
                    "maintenance_follow_up_recommended": False,
                },
                "namespace": "#V#user@org",
                "user_id": "#V#user",
                "org_id": "#V#org",
            }
        )
    )

    assert result.ok
    assert result.outputs["episode_critique_memory_id"] == "#V#episode_critique_memory_abc"
    assert result.outputs["remediation_routing_decision"] == "task_and_jira"
    assert result.outputs["remediation_routing_fingerprint"] == "fp-1610"
    assert result.outputs["remediation_repeat_count"] == 3
    assert result.outputs["remediation_task_id"] == "#V#task_1610"
    assert result.outputs["remediation_issue_key"] == "JVNAUTOSCI-1610"
    assert result.outputs["improvement_suggestion_count"] == 1
    assert result.outputs["improvement_suggestion_categories"] == ["workflow_change"]
    assert result.outputs["improvement_suggestions"][0]["target_workflow_id"] == (
        "#V#alpha_workflow"
    )
    assert result.outputs["self_improvement_workflow_count"] == 1
    assert result.outputs["self_improvement_target_workflow_ids"] == [
        "#V#alpha_workflow"
    ]
    assert result.outputs["maintenance_follow_up_requested"] is False


def test_evidence_bundle_handler_surfaces_format_over_content_diagnostic(monkeypatch):
    from src.backend.workflows.durable import episode_evaluation_workflow as mod

    monkeypatch.setattr(
        mod,
        "build_episode_critic_evidence_bundle",
        lambda **kwargs: {
            "success": True,
            "ready_for_critic": True,
            "fail_closed": False,
            "episode_locator": {
                "request_id": "req-1890",
                "workflow_id": "#V#chat_assistant_workflow",
                "namespace": "#V#user@org",
            },
            "source_resolution": {"resolved_request_id": "req-1890"},
            "expected_context": {"allowed_tool_families": ["search"]},
            "observed_evidence": {"turn_execution_record": {"request_id": "req-1890"}},
            "capability_gaps": [],
            "fail_closed_reason_codes": [],
            "bundle_receipt": {"sha256": "bundle-receipt"},
            "format_over_content_diagnostic": {
                "status": "suspected",
                "summary": "Structured output may have displaced nuance.",
                "confidence": 0.68,
                "reason_codes": [
                    "structured_output_contract_present",
                    "grounded_tool_path_unused",
                ],
                "selected_model": "gpt-5.4-mini",
                "observed_stage_id": "selector_decision",
                "raw_response_format": "json_object",
            },
        },
    )

    handler = mod._build_evidence_bundle_handler()
    result = handler(
        _request(
            {
                "request_id": "req-1890",
                "namespace": "#V#user@org",
            }
        )
    )

    assert result.ok
    diagnostic = result.outputs["format_over_content_diagnostic"]
    assert diagnostic["status"] == "suspected"
    assert diagnostic["selected_model"] == "gpt-5.4-mini"
    fallback = result.outputs["episode_critic_fallback_assessment"]
    assert fallback["format_over_content_diagnostic"]["status"] == "suspected"
    assert fallback["improvement_suggestions"] == []
    assert fallback["evaluator_contract"]["schema_version"] == (
        "episode_evaluator_contract.v1"
    )
    grounded_axis = next(
        axis
        for axis in fallback["evaluator_contract"]["axes"]
        if axis["axis_id"] == "grounded_helpfulness"
    )
    assert grounded_axis["status"] == "inconclusive"
    assert any(
        "output contract" in recommendation.lower()
        for recommendation in fallback["recommendations"]
    )


def test_persist_memory_handler_merges_grounded_helpfulness_assessment(monkeypatch):
    from src.backend.workflows.durable import episode_evaluation_workflow as mod

    captured: dict[str, Any] = {}

    def _fake_upsert(**kwargs):
        captured["assessment"] = kwargs.get("assessment")
        return {
            "success": True,
            "memory_id": "#V#episode_critique_memory_gh_1",
            "state": {
                "memory_id": "#V#episode_critique_memory_gh_1",
                "namespace": "#V#user@org",
                "improvement_suggestions": [],
            },
        }

    monkeypatch.setattr(
        mod,
        "upsert_episode_critique_memory_from_episode_assessment",
        _fake_upsert,
    )
    monkeypatch.setattr(
        mod,
        "route_episode_critique_memory",
        lambda **kwargs: {"success": True, "decision": "memory_only"},
    )
    monkeypatch.setattr(
        mod,
        "launch_episode_self_improvement_workflows",
        lambda **kwargs: {"success": True, "launches": []},
    )

    handler = mod._build_persist_memory_handler()
    result = handler(
        _request(
            {
                "episode_evidence_bundle": {
                    "episode_locator": {
                        "request_id": "req-1999-merge",
                        "workflow_id": "#V#chat_assistant_workflow",
                        "namespace": "#V#user@org",
                    }
                },
                "critic_assessment": {
                    "verdict": "pass",
                    "confidence": 0.82,
                    "unresolved_check_count": 0,
                    "summary": "The route completed, but grounded helpfulness must still be tracked.",
                    "maintenance_follow_up_recommended": False,
                    "recommendations": [],
                    "root_causes": [],
                    "improvement_suggestions": [],
                },
                "grounded_helpfulness_assessment": {
                    "axis_id": "grounded_helpfulness",
                    "status": "fail",
                    "confidence": 0.91,
                    "summary": "The answer contradicted retained retrieval evidence.",
                    "reason_codes": [
                        "authoritative_answer_consistency_blocker_present"
                    ],
                    "counterfactual_recommended_action": (
                        "abstain_or_follow_up_instead_of_overclaiming"
                    ),
                },
                "namespace": "#V#user@org",
                "user_id": "#V#user",
                "org_id": "#V#org",
            }
        )
    )

    assert result.ok
    assessment = captured["assessment"]
    grounded_axis = next(
        axis
        for axis in assessment["evaluator_contract"]["axes"]
        if axis["axis_id"] == "grounded_helpfulness"
    )
    assert grounded_axis["status"] == "fail"
    assert grounded_axis["summary"] == (
        "The answer contradicted retained retrieval evidence."
    )
    assert grounded_axis["reason_codes"] == [
        "authoritative_answer_consistency_blocker_present"
    ]
    assert grounded_axis["counterfactual_recommended_action"] == (
        "abstain_or_follow_up_instead_of_overclaiming"
    )


def test_persist_memory_handler_emits_maintenance_launch_payload(monkeypatch):
    from src.backend.workflows.durable import episode_evaluation_workflow as mod

    monkeypatch.setattr(
        mod,
        "upsert_episode_critique_memory_from_episode_assessment",
        lambda **kwargs: {
            "success": True,
            "memory_id": "#V#episode_critique_memory_req_1837",
            "state": {
                "memory_id": "#V#episode_critique_memory_req_1837",
                "namespace": "#V#user@org",
            },
        },
    )
    monkeypatch.setattr(
        mod,
        "route_episode_critique_memory",
        lambda **kwargs: {"success": True, "decision": "maintenance_follow_up"},
    )
    monkeypatch.setattr(
        mod,
        "launch_episode_self_improvement_workflows",
        lambda **kwargs: {"success": True, "launches": []},
    )

    handler = mod._build_persist_memory_handler()
    result = handler(
        _request(
            {
                "episode_evidence_bundle": {
                    "episode_locator": {
                        "request_id": "req-1837",
                        "session_id": "session-1837",
                        "workflow_id": "#V#student_supervision_lookup_workflow",
                        "namespace": "#V#user@org",
                    }
                },
                "critic_assessment": {
                    "summary": "Workflow discovery surfaced a malformed workflow candidate.",
                    "maintenance_follow_up_recommended": True,
                    "maintenance_follow_up_reason": "malformed_discovery_candidate",
                },
                "namespace": "#V#user@org",
                "user_id": "#V#user",
                "org_id": "#V#org",
                "maintenance_apply_repairs_default": True,
            }
        )
    )

    assert result.ok
    assert result.outputs["episode_critique_memory_id"] == (
        "#V#episode_critique_memory_req_1837"
    )
    assert result.outputs["maintenance_follow_up_requested"] is True
    launch_inputs = result.outputs["maintenance_launch_inputs"]
    assert launch_inputs["episode_critique_memory_id"] == (
        "#V#episode_critique_memory_req_1837"
    )
    assert launch_inputs["request_id"] == "req-1837"
    assert launch_inputs["session_id"] == "session-1837"
    assert launch_inputs["selected_workflow_id"] == (
        "#V#student_supervision_lookup_workflow"
    )
    assert "malformed workflow candidate" in launch_inputs["incident_text"].lower()
