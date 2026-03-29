from __future__ import annotations

from typing import Any

from src.backend.services import episode_critique_routing_service as svc


class _StubCollection:
    def __init__(self, *, count: int) -> None:
        self._count = count

    def count_documents(self, _query, limit=0):  # noqa: ARG002
        return self._count


def _sample_state(
    *,
    verdict: str = "follow_up_required",
    confidence: float = 0.85,
    severity: str = "high",
) -> dict[str, Any]:
    return {
        "memory_id": "#V#episode_critique_memory_abc",
        "request_id": "req-1610-1",
        "session_id": "sess-1610-1",
        "namespace": "#V#user@org",
        "user_id": "#V#user",
        "org_id": "#V#org",
        "description": "Repeated critic finding for routing",
        "subject_episode": {
            "workflow_id": "#V#tool_calling_workflow",
            "workflow_definition_identity": {"workflow_name": "Tool Calling Workflow"},
        },
        "critic": {
            "verdict": verdict,
            "confidence": confidence,
            "unresolved_check_count": 2,
            "assessment": {
                "summary": "Search quality drift remained unresolved.",
                "maintenance_follow_up_recommended": severity in {"high", "critical"},
                "maintenance_follow_up_reason": (
                    "search_quality_regression"
                    if severity in {"high", "critical"}
                    else None
                ),
                "root_causes": [
                    {
                        "cause_id": "search_quality_regression",
                        "severity": severity,
                        "rationale": "Concept search ranked an irrelevant concept too highly.",
                    }
                ],
            },
        },
        "implicated": {
            "workflow_ids": ["#V#tool_calling_workflow"],
            "tool_names": ["search_concepts"],
        },
        "remediation": {"task_ids": [], "jira_issue_keys": []},
    }


def test_route_episode_critique_memory_keeps_novel_low_confidence_signal_in_memory(
    monkeypatch,
):
    recorded: dict[str, Any] = {}

    monkeypatch.setattr(
        svc,
        "get_episode_critique_memories_collection",
        lambda: _StubCollection(count=0),
    )
    monkeypatch.setattr(
        svc,
        "find_task_by_external_reference",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_routing",
        lambda **kwargs: recorded.update(kwargs)
        or {
            "success": True,
            "state": {
                **_sample_state(verdict="fail", confidence=0.4, severity="low"),
                "routing": kwargs["routing"],
                "remediation": {"task_ids": [], "jira_issue_keys": []},
            },
        },
    )
    monkeypatch.setattr(
        svc,
        "create_task",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("create_task should not be called")
        ),
    )
    monkeypatch.setattr(
        svc,
        "upsert_deduplicated_jira_issue",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("jira upsert should not be called")
        ),
    )

    result = svc.route_episode_critique_memory(
        memory_state=_sample_state(verdict="fail", confidence=0.4, severity="low"),
        actor_concept_id="#V#user",
    )

    assert result["success"] is True
    assert result["decision"] == svc.ROUTING_DECISION_MEMORY_ONLY
    assert result["task_action"] == "not_needed"
    assert result["jira_action"] == "not_needed"
    assert recorded["routing"]["decision"] == svc.ROUTING_DECISION_MEMORY_ONLY
    assert "confidence_below_task_threshold" in recorded["routing"]["reason_codes"]


def test_route_episode_critique_memory_reuses_existing_task_and_jira(monkeypatch):
    recorded: dict[str, Any] = {}
    external_reference_updates: list[dict[str, Any]] = []
    jira_calls: list[dict[str, Any]] = []
    existing_task = {
        "task_concept_id": "#V#task_existing",
        "external_references": {
            svc.EPISODE_CRITIQUE_REMEDIATION_SOURCE_SYSTEM: {
                "external_id": "stale",
                "memory_ids": ["#V#episode_critique_memory_old"],
                "jira_issue_keys": ["JVNAUTOSCI-1700"],
            }
        },
    }

    monkeypatch.setattr(
        svc,
        "get_episode_critique_memories_collection",
        lambda: _StubCollection(count=2),
    )
    monkeypatch.setattr(
        svc,
        "find_task_by_external_reference",
        lambda **kwargs: existing_task,
    )
    monkeypatch.setattr(
        svc,
        "create_task",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("existing remediation task should be reused")
        ),
    )
    monkeypatch.setattr(
        svc,
        "upsert_task_external_reference",
        lambda task_concept_id, **kwargs: external_reference_updates.append(
            {"task_concept_id": task_concept_id, **kwargs}
        )
        or {"task_concept_id": task_concept_id},
    )
    monkeypatch.setattr(
        svc,
        "upsert_deduplicated_jira_issue",
        lambda **kwargs: jira_calls.append(kwargs)
        or {
            "success": True,
            "mode": "updated_existing",
            "issue_key": "JVNAUTOSCI-1700",
        },
    )
    monkeypatch.setattr(
        svc,
        "record_episode_critique_memory_routing",
        lambda **kwargs: recorded.update(kwargs)
        or {
            "success": True,
            "state": {
                **_sample_state(),
                "routing": kwargs["routing"],
                "remediation": {
                    "task_ids": ["#V#task_existing"],
                    "jira_issue_keys": ["JVNAUTOSCI-1700"],
                },
            },
        },
    )

    result = svc.route_episode_critique_memory(
        memory_state=_sample_state(),
        actor_concept_id="#V#user",
    )

    assert result["success"] is True
    assert result["decision"] == svc.ROUTING_DECISION_TASK_AND_JIRA
    assert result["task_action"] == "reused_existing"
    assert result["jira_action"] == "updated_existing"
    assert result["remediation_task_id"] == "#V#task_existing"
    assert result["remediation_issue_key"] == "JVNAUTOSCI-1700"
    assert jira_calls
    assert jira_calls[0]["existing_comment"]
    assert external_reference_updates
    assert (
        external_reference_updates[-1]["reference_payload"]["jira_issue_keys"]
        == ["JVNAUTOSCI-1700"]
    )
    assert recorded["routing"]["repeat_count"] == 3
    assert recorded["routing"]["decision"] == svc.ROUTING_DECISION_TASK_AND_JIRA
