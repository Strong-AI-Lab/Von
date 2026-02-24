from __future__ import annotations

from typing import Any

from src.backend.services.jira_hygiene_service import (
    discover_jira_hygiene,
    propose_jira_hygiene_plan,
)


def test_discover_jira_hygiene_classifies_in_progress_candidates() -> None:
    def _fake_search_issues(**kwargs: Any) -> dict[str, Any]:
        jql = str(kwargs.get("jql") or "")
        if "issuetype = Epic" in jql:
            return {
                "issues": [
                    {
                        "key": "JVNAUTOSCI-800",
                        "fields": {
                            "summary": "Identity hardening",
                            "issuetype": {"name": "Epic"},
                            "status": {"name": "In Progress"},
                        },
                    }
                ]
            }
        if 'statusCategory = "In Progress"' in jql:
            return {
                "issues": [
                    {
                        "key": "JVNAUTOSCI-9010",
                        "fields": {
                            "summary": "Completed rollout with closed child scope",
                            "issuetype": {"name": "Task"},
                            "status": {
                                "name": "In Progress",
                                "statusCategory": {"name": "In Progress"},
                            },
                            "updated": "2026-02-10T00:00:00.000+0000",
                            "subtasks": [
                                {
                                    "fields": {
                                        "status": {
                                            "name": "Done",
                                            "statusCategory": {"name": "Done"},
                                        }
                                    }
                                }
                            ],
                        },
                    },
                    {
                        "key": "JVNAUTOSCI-9011",
                        "fields": {
                            "summary": "Stale in-progress issue without active child scope",
                            "issuetype": {"name": "Task"},
                            "status": {
                                "name": "In Progress",
                                "statusCategory": {"name": "In Progress"},
                            },
                            "updated": "2025-01-01T00:00:00.000+0000",
                            "subtasks": [],
                            "issuelinks": [],
                        },
                    },
                    {
                        "key": "JVNAUTOSCI-9012",
                        "fields": {
                            "summary": "Active in-progress issue with open child scope",
                            "issuetype": {"name": "Task"},
                            "status": {
                                "name": "In Progress",
                                "statusCategory": {"name": "In Progress"},
                            },
                            "updated": "2026-02-23T00:00:00.000+0000",
                            "subtasks": [
                                {
                                    "fields": {
                                        "status": {
                                            "name": "In Progress",
                                            "statusCategory": {"name": "In Progress"},
                                        }
                                    }
                                }
                            ],
                        },
                    },
                ]
            }
        return {"issues": []}

    result = discover_jira_hygiene(
        search_issues=_fake_search_issues,
        project_key="JVNAUTOSCI",
        max_issues=20,
        max_epics=10,
        include_cross_cutting=False,
        include_in_progress_candidates=True,
    )

    assert result.get("success") is True
    candidates = result.get("in_progress_candidates") or []
    by_key = {item.get("issue_key"): item for item in candidates if isinstance(item, dict)}

    assert by_key["JVNAUTOSCI-9010"]["recommended_action"] == "candidate_done"
    assert by_key["JVNAUTOSCI-9011"]["recommended_action"] == "candidate_todo"
    assert by_key["JVNAUTOSCI-9012"]["recommended_action"] == "keep_in_progress"

    review_summary = result.get("in_progress_review_summary") or {}
    assert review_summary.get("candidate_done") == 1
    assert review_summary.get("candidate_todo") == 1
    assert review_summary.get("keep_in_progress") == 1


def test_propose_jira_hygiene_plan_surfaces_in_progress_review_decisions() -> None:
    proposal = propose_jira_hygiene_plan(
        epic_catalogue=[],
        orphan_candidates=[],
        cross_cutting_candidates=[],
        in_progress_candidates=[
            {
                "issue_key": "JVNAUTOSCI-9100",
                "summary": "Ready for done review",
                "recommended_action": "candidate_done",
                "recommended_action_reason": "all_subtasks_done",
                "days_since_update": 3,
                "subtasks_total": 2,
                "subtasks_done": 2,
                "subtasks_open": 0,
                "linked_done_count": 0,
                "linked_not_done_count": 0,
            },
            {
                "issue_key": "JVNAUTOSCI-9101",
                "summary": "Likely stale and should return to To Do",
                "recommended_action": "candidate_todo",
                "recommended_action_reason": "stale_without_active_children",
                "days_since_update": 30,
                "subtasks_total": 0,
                "subtasks_done": 0,
                "subtasks_open": 0,
                "linked_done_count": 0,
                "linked_not_done_count": 0,
            },
            {
                "issue_key": "JVNAUTOSCI-9102",
                "summary": "Still active",
                "recommended_action": "keep_in_progress",
            },
        ],
        batch_size=5,
    )

    assert proposal.get("success") is True
    reasons = {str(item.get("reason")) for item in proposal.get("needs_decision", [])}
    assert "in_progress_review_candidate_done" in reasons
    assert "in_progress_review_candidate_todo" in reasons

    no_action_reasons = {str(item.get("reason")) for item in proposal.get("no_action", [])}
    assert "in_progress_review_keep" in no_action_reasons

    summary = proposal.get("proposal_summary") or {}
    review_counts = summary.get("in_progress_review_counts") or {}
    assert review_counts.get("candidate_done") == 1
    assert review_counts.get("candidate_todo") == 1
    assert review_counts.get("keep_in_progress") == 1
