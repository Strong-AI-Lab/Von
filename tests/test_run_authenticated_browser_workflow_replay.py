from __future__ import annotations

import scripts.run_authenticated_browser_workflow_replay as replay


def test_extract_progress_facts_from_nested_progress_payloads() -> None:
    payload = {
        "progress_snapshots": [
            {
                "diagnostic_events": [
                    {
                        "progress_facts": [
                            {
                                "fact_id": "gmail_message_subject",
                                "label": "Email message",
                                "contract_id": "#V#gmail_arxiv_email_message_subject_progress_fact",
                                "source_path": "context.current_message.subject",
                                "redacted": True,
                            }
                        ]
                    }
                ],
                "selected_workflow_execution": {
                    "events": [
                        {
                            "selected_workflow_id": replay.GMAIL_ARXIV_WORKFLOW_ID,
                            "progress_facts": [
                                {
                                    "fact_id": "arxiv_paper_id",
                                    "label": "arXiv paper",
                                    "contract_id": "#V#gmail_arxiv_paper_id_progress_fact",
                                    "source_path": "context.arxiv_id",
                                }
                            ],
                        }
                    ]
                },
            }
        ]
    }

    facts = replay.extract_progress_facts(payload)
    fact_ids = {fact["fact_id"] for fact in facts}

    assert fact_ids == {"gmail_message_subject", "arxiv_paper_id"}
    assert replay.extract_selected_workflow_ids(payload) == [
        replay.GMAIL_ARXIV_WORKFLOW_ID
    ]


def test_classify_replay_reports_auth_blocker_before_workflow_assertions() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": False, "error": "Browser-test auth is disabled"},
        auth_status={"authenticated": False},
        gmail_preflight={},
        task_evidence={"last_task_status": {"status": "unknown"}},
        selected_workflow_ids=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "browser_test_auth_blocker"
    assert "disabled" in analysis["blocker"]["reason"]


def test_classify_replay_reports_gmail_profile_blocker_when_auth_ready() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={
            "gmail_capability_ready": False,
            "requested_gmail_profile": None,
            "configured_gmail_profiles": [],
        },
        task_evidence={"last_task_status": {"status": "completed"}},
        selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "gmail_oauth_or_profile_blocker"


def test_classify_replay_passes_when_expected_workflow_and_projection_seen() -> None:
    facts = [
        {"fact_id": fact_id, "contract_id": contract_id}
        for fact_id, contract_id in zip(
            replay.GMAIL_ARXIV_FACT_IDS,
            replay.GMAIL_ARXIV_CONTRACT_IDS,
        )
    ]

    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={
            "gmail_capability_ready": True,
            "requested_gmail_profile": "zhan_gmail",
            "configured_gmail_profiles": ["zhan_gmail"],
            "oauth_status": {"has_tokens": True},
        },
        task_evidence={"last_task_status": {"status": "completed"}},
        selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        progress_facts=facts,
    )

    assert analysis["verdict"] == "pass"
    assert analysis["blocker"] is None
    assert analysis["missing_progress_fact_ids"] == []
    assert analysis["missing_contract_ids"] == []
