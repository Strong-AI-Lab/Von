from __future__ import annotations

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
    assert result.outputs["maintenance_follow_up_requested"] is False
