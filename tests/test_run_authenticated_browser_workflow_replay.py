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
    assert replay.extract_observed_workflow_ids(payload) == []


def test_build_report_uses_task_status_snapshots_for_route_and_progress_evidence() -> (
    None
):
    report = replay.build_replay_report(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        environment={},
        window_session_id="window-1",
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={"gmail_capability_ready": True},
        chat_session={},
        submission={"task_id": "task-1"},
        task_evidence={
            "task_statuses": [
                {
                    "status": "running",
                    "progress": {
                        "selected_workflow_id": replay.GMAIL_ARXIV_WORKFLOW_ID,
                        "workflow_id": replay.GMAIL_ARXIV_WORKFLOW_ID,
                        "progress_facts": [
                            {
                                "fact_id": "gmail_message_subject",
                                "contract_id": (
                                    "#V#gmail_arxiv_email_message_subject_progress_fact"
                                ),
                                "redacted": True,
                                "source_path": "context.current_message.subject",
                            }
                        ],
                    },
                }
            ],
            "progress_snapshots": [],
            "last_task_status": {"status": "running"},
            "last_progress": {},
            "task_result": {},
        },
    )

    assert report["task_status_snapshot_count"] == 1
    assert report["analysis"]["selected_workflow_ids"] == [
        replay.GMAIL_ARXIV_WORKFLOW_ID
    ]
    assert report["analysis"]["observed_workflow_ids"] == [
        replay.GMAIL_ARXIV_WORKFLOW_ID
    ]
    assert report["thinking_card_progress_facts"] == [
        {
            "fact_id": "gmail_message_subject",
            "contract_id": "#V#gmail_arxiv_email_message_subject_progress_fact",
            "redacted": True,
            "source_path": "context.current_message.subject",
        }
    ]


def test_classify_replay_reports_auth_blocker_before_workflow_assertions() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": False, "error": "Browser-test auth is disabled"},
        auth_status={"authenticated": False},
        gmail_preflight={},
        task_evidence={"last_task_status": {"status": "unknown"}},
        selected_workflow_ids=[],
        observed_workflow_ids=[],
        selector_diagnostics=[],
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
        observed_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        selector_diagnostics=[],
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
        observed_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        selector_diagnostics=[],
        progress_facts=facts,
    )

    assert analysis["verdict"] == "pass"
    assert analysis["blocker"] is None
    assert analysis["missing_progress_fact_ids"] == []
    assert analysis["missing_contract_ids"] == []


def test_classify_replay_reports_selector_blocker_before_running_timeout() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={"last_task_status": {"status": "running"}},
        selected_workflow_ids=[],
        observed_workflow_ids=["#V#kb_mutation_postcondition_critic_workflow"],
        selector_diagnostics=[
            {
                "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
                "phase": "tool_plan",
            }
        ],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "selector_or_dispatch_blocker"
    assert "#V#kb_mutation_postcondition_critic_workflow" in analysis["blocker"][
        "observed_workflow_ids"
    ]


def test_classify_replay_reports_expected_workflow_action_blocker() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={
            "last_task_status": {
                "status": "cancelled",
                "progress": {
                    "workflow_id": "#V#arxiv_paper_representation_workflow",
                    "state_id": "import_arxiv_pdf_from_url",
                    "action_id": "import_url_file_copy",
                    "phase": "cancelled",
                },
            }
        },
        selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        observed_workflow_ids=[
            replay.GMAIL_ARXIV_WORKFLOW_ID,
            "#V#arxiv_paper_representation_workflow",
        ],
        selector_diagnostics=[
            {
                "selected_workflow_id": replay.GMAIL_ARXIV_WORKFLOW_ID,
                "workflow_id": replay.GMAIL_ARXIV_WORKFLOW_ID,
            }
        ],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["selected_expected_workflow"] is True
    assert analysis["blocker"]["type"] == "workflow_action_terminal_state_blocker"
    assert analysis["blocker"]["last_workflow_id"] == (
        "#V#arxiv_paper_representation_workflow"
    )
    assert analysis["blocker"]["last_state_id"] == "import_arxiv_pdf_from_url"
    assert analysis["blocker"]["last_action_id"] == "import_url_file_copy"
    assert (
        "#V#gmail_arxiv_email_message_subject_progress_fact"
        in analysis["blocker"]["missing_contract_ids"]
    )
