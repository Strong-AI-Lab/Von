from __future__ import annotations

from typing import Any

import scripts.run_authenticated_browser_workflow_replay as replay


def test_establish_browser_test_session_skips_fixture_refresh(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    def _fake_request_json(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"success": True, "user_concept_id": "#V#zhan_von_witbrock"}

    monkeypatch.setattr(replay, "_request_json", _fake_request_json)
    session = _FakeSession()

    result = replay.establish_browser_test_session(
        session=session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5001",
        window_session_id="window-1",
        timeout_seconds=12.0,
    )

    assert result["success"] is True
    assert session.headers["X-Von-Window-Session"] == "window-1"
    assert captured["kwargs"]["json"] == {
        "window_session_id": "window-1",
        "refresh_fixture": False,
    }


def test_workflow_capability_preflight_waits_through_transient_build(
    monkeypatch,
) -> None:
    payloads = [
        {
            "ready": False,
            "workflow_discovery_available": False,
            "status": "building",
            "build_in_progress": True,
            "size": 0,
            "detail": "Workflow capability index still building.",
        },
        {
            "ready": True,
            "workflow_discovery_available": True,
            "status": "ready",
            "build_in_progress": False,
            "size": 83,
            "detail": "Workflow capability index ready.",
        },
    ]
    sleeps: list[float] = []

    def _fake_request_json(*_args, **_kwargs):
        return payloads.pop(0)

    monkeypatch.setattr(replay, "_request_json", _fake_request_json)
    monkeypatch.setattr(replay.time, "sleep", lambda seconds: sleeps.append(seconds))

    preflight = replay.build_workflow_capability_preflight(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5001",
        timeout_seconds=30.0,
        poll_interval_seconds=0.5,
    )

    assert preflight["workflow_discovery_available"] is True
    assert preflight["preflight_wait"]["completed"] is True
    assert preflight["preflight_wait"]["attempt_count"] == 2
    assert [item["status"] for item in preflight["preflight_wait"]["snapshots"]] == [
        "building",
        "ready",
    ]
    assert sleeps == [0.5]


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
        target_session_context={"target_session_context_ready": True},
        database_preflight={"database_runtime_available": True},
        llm_preflight={"llm_runtime_available": True},
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


def test_submit_background_generate_sends_workflow_inputs(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_request_json(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"task_id": "task-1"}

    monkeypatch.setattr(replay, "_request_json", _fake_request_json)

    case = replay.ReplayCase(
        case_id="launch-inputs",
        prompt="Run the selected workflow.",
        expected_workflow_id="#V#workflow",
        expected_progress_fact_ids=(),
        expected_contract_ids=(),
        workflow_inputs={
            "gmail_max_results": 1,
            "base_gmail_query": "arxiv.org newer_than:365d",
            "": "ignored",
        },
    )

    result = replay.submit_background_generate(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5001",
        case=case,
        client_request_id="request-1",
        conversation_session_id="session-1",
        gmail_profile="vonwitbrock-gmail",
        model="qwen3:8b",
        presenter_mode=False,
        thinking_card_mode="debug",
    )

    assert result == {"task_id": "task-1"}
    assert captured["args"][1:3] == ("POST", "http://127.0.0.1:5001/von/generate")
    assert captured["kwargs"]["json"]["workflow_inputs"] == {
        "gmail_max_results": 1,
        "base_gmail_query": "arxiv.org newer_than:365d",
    }


def test_gmail_arxiv_idempotence_initial_turn_sets_bounded_workflow_inputs() -> None:
    turns = replay.build_gmail_arxiv_idempotence_turn_cases(
        gmail_query="arxiv.org newer_than:365d"
    )

    assert [turn.turn_id for turn in turns] == ["initial", "repeat", "verify"]
    assert turns[0].case.workflow_inputs == {
        "base_gmail_query": "arxiv.org newer_than:365d",
        "gmail_max_results": 1,
        "max_results": 1,
    }
    assert turns[1].case.workflow_inputs is None
    assert turns[2].case.workflow_inputs is None


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


def test_classify_replay_keeps_secondary_preflight_blockers_visible() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": False, "error": "Browser-test auth is disabled"},
        auth_status={"authenticated": False},
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={"last_task_status": {"status": "unknown"}},
        selected_workflow_ids=[],
        observed_workflow_ids=[],
        selector_diagnostics=[],
        progress_facts=[],
        workflow_capability_preflight={
            "ready": False,
            "workflow_discovery_available": False,
            "status": "error",
            "summary": "Workflow capability index requires rebuild.",
        },
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "browser_test_auth_blocker"
    assert [item["type"] for item in analysis["precondition_blockers"]] == [
        "browser_test_auth_blocker",
        "workflow_capability_index_blocker",
    ]
    workflow_blocker = analysis["precondition_blockers"][1]
    assert workflow_blocker["workflow_capability_preflight"]["status"] == "error"


def test_classify_replay_reports_database_blocker_before_selector() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        database_preflight={
            "database_runtime_available": False,
            "connected": False,
            "error": "Mongo read/write health was not stable.",
        },
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={"last_task_status": {"status": "completed"}},
        selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        observed_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        selector_diagnostics=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "database_runtime_blocker"
    assert analysis["precondition_blockers"][0]["type"] == "database_runtime_blocker"


def test_classify_replay_reports_llm_model_blocker_before_selector() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        llm_preflight={
            "llm_runtime_available": False,
            "provider": "ollama",
            "model_checked": "gemma4:e4b",
            "selected_model_available": False,
            "error": "The effective replay model is not ready.",
        },
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={"last_task_status": {"status": "completed"}},
        selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        observed_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        selector_diagnostics=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "llm_runtime_blocker"
    assert analysis["blocker"]["llm_preflight"]["model_checked"] == "gemma4:e4b"


def test_apply_target_session_context_reports_mismatch(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    class _FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    def _fake_request_json(_session, method, url, **kwargs):
        calls.append((method, url, dict(kwargs)))
        if url.endswith("/set_user_concept"):
            return {"status": "updated", "user_id": "#V#michael_witbrock"}
        if url.endswith("/set_organisation"):
            return {
                "status": "updated",
                "organisation_id": "#V#university_of_auckland_strong_ai_lab",
            }
        if url.endswith("/session/context"):
            return {
                "authenticated": True,
                "user_id": "#V#zhan_von_witbrock",
                "organisation_id": "#V#university_of_auckland_strong_ai_lab",
                "namespace": "#V#zhan_von_witbrock@university_of_auckland_strong_ai_lab",
            }
        raise AssertionError(url)

    monkeypatch.setattr(replay, "_request_json", _fake_request_json)
    session = _FakeSession()

    result = replay.apply_target_session_context(
        session=session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5001",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="#V#university_of_auckland_strong_ai_lab",
    )

    assert result["target_session_context_ready"] is False
    assert result["mismatch_reasons"] == [
        "requested user #V#michael_witbrock but effective user is #V#zhan_von_witbrock"
    ]
    assert session.headers["X-User-Concept-ID"] == "#V#michael_witbrock"
    assert [call[0] for call in calls] == ["POST", "POST", "GET"]


def test_build_gmail_preflight_requires_live_access_test(monkeypatch) -> None:
    monkeypatch.setattr(
        replay,
        "_query_settings",
        lambda **_kwargs: {
            "gmail_profiles": ["vonwitbrock-gmail"],
            "gmail_default_profile": None,
        },
    )
    monkeypatch.setattr(
        replay,
        "_query_gmail_oauth_status",
        lambda **_kwargs: {"has_tokens": True},
    )
    monkeypatch.setattr(
        replay,
        "_query_gmail_access_test",
        lambda **_kwargs: {
            "success": False,
            "status": "reauthorisation_required",
            "error": "invalid_grant",
        },
    )

    preflight = replay.build_gmail_preflight(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5001",
        gmail_profile="vonwitbrock-gmail",
    )

    assert preflight["gmail_capability_ready"] is False
    assert preflight["access_test"]["error"] == "invalid_grant"


def test_build_gmail_preflight_passes_with_live_access_test(monkeypatch) -> None:
    monkeypatch.setattr(
        replay,
        "_query_settings",
        lambda **_kwargs: {
            "gmail_profiles": ["vonwitbrock-gmail"],
            "gmail_default_profile": None,
        },
    )
    monkeypatch.setattr(
        replay,
        "_query_gmail_oauth_status",
        lambda **_kwargs: {"has_tokens": True},
    )
    monkeypatch.setattr(
        replay,
        "_query_gmail_access_test",
        lambda **_kwargs: {"success": True, "status": "access_ok"},
    )

    preflight = replay.build_gmail_preflight(
        session=object(),  # type: ignore[arg-type]
        base_url="http://127.0.0.1:5001",
        gmail_profile="vonwitbrock-gmail",
    )

    assert preflight["gmail_capability_ready"] is True
    assert preflight["access_test"]["status"] == "access_ok"


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


def test_classify_replay_reports_workflow_capability_index_blocker_before_selector() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={"last_task_status": {"status": "completed"}},
        selected_workflow_ids=["#V#tool_calling_workflow"],
        observed_workflow_ids=[],
        selector_diagnostics=[
            {"selected_workflow_id": "#V#tool_calling_workflow"}
        ],
        progress_facts=[],
        workflow_capability_preflight={
            "ready": False,
            "workflow_discovery_available": False,
            "status": "building",
            "summary": "Workflow capability index still building.",
            "detail": "Workflow discovery is waiting on the authoritative capability index.",
        },
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "workflow_capability_index_blocker"
    assert analysis["blocker"]["workflow_capability_preflight"]["status"] == "building"
    assert analysis["workflow_capability_preflight"]["workflow_discovery_available"] is False
    assert analysis["precondition_blockers"][0]["type"] == (
        "workflow_capability_index_blocker"
    )


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


def test_classify_replay_ignores_gmail_arxiv_progress_fact_names() -> None:
    case = replay.ReplayCase(
        case_id="arxiv-paper-representation",
        prompt="Ingest https://arxiv.org/abs/2406.15341",
        expected_workflow_id="#V#arxiv_paper_representation_workflow",
        expected_progress_fact_ids=(),
        expected_contract_ids=(),
    )

    analysis = replay.classify_replay(
        case=case,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={},
        task_evidence={
            "last_task_status": {"status": "completed"},
            "task_result": {
                "result": {
                    "response_text": (
                        "Verified workflow experience profile concept: "
                        "#V#workflow_llm_experience_profile_v_conversation_turn_execution_workflow_gpt_5_4_nano."
                    )
                }
            },
            "thinking_card_progress_facts": [
                {
                    "fact_id": "arxiv_paper_title",
                    "contract_id": "#V#gmail_arxiv_paper_title_progress_fact",
                },
                {
                    "fact_id": "arxiv_paper_id",
                    "contract_id": "#V#gmail_arxiv_metadata_arxiv_id_progress_fact",
                },
            ],
        },
        selected_workflow_ids=["#V#arxiv_paper_representation_workflow"],
        observed_workflow_ids=["#V#arxiv_paper_representation_workflow"],
        selector_diagnostics=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "pass"
    assert analysis["blocker"] is None


def test_classify_replay_ignores_gmail_auth_text_in_llm_prompts() -> None:
    case = replay.ReplayCase(
        case_id="arxiv-paper-representation",
        prompt="Ingest https://arxiv.org/abs/2406.15341",
        expected_workflow_id="#V#arxiv_paper_representation_workflow",
        expected_progress_fact_ids=(),
        expected_contract_ids=(),
    )

    analysis = replay.classify_replay(
        case=case,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={},
        task_evidence={
            "last_task_status": {"status": "completed"},
            "progress_snapshots": [
                {
                    "diagnostic_events": [
                        {
                            "llm_request": {
                                "context_messages": [
                                    {
                                        "content": {
                                            "text": (
                                                "AUTHENTICATION STATUS: Authenticated. "
                                                "Use Gmail tools only when the user asks "
                                                "for mail; if not authenticated, explain "
                                                "that OAuth tokens are required."
                                            )
                                        }
                                    }
                                ]
                            }
                        }
                    ],
                    "stage_diagnostics": [
                        {
                            "latest_llm_exchange": {
                                "prompt_preview": {
                                    "text": (
                                        "Prompt text says Gmail OAuth tokens may "
                                        "be required if mail is not connected."
                                    )
                                }
                            }
                        }
                    ],
                }
            ],
        },
        selected_workflow_ids=["#V#arxiv_paper_representation_workflow"],
        observed_workflow_ids=["#V#arxiv_paper_representation_workflow"],
        selector_diagnostics=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "pass"
    assert analysis["blocker"] is None


def test_classify_replay_reports_explicit_gmail_oauth_failure_text() -> None:
    case = replay.ReplayCase(
        case_id="generic-gmail-posthoc-failure",
        prompt="Inspect Gmail",
        expected_workflow_id="#V#tool_calling_workflow",
        expected_progress_fact_ids=(),
        expected_contract_ids=(),
    )

    analysis = replay.classify_replay(
        case=case,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={},
        task_evidence={
            "last_task_status": {"status": "completed"},
            "task_result": {
                "result": {
                    "response_text": (
                        "Gmail OAuth token is missing; the Gmail profile is not "
                        "authenticated."
                    )
                }
            },
        },
        selected_workflow_ids=["#V#tool_calling_workflow"],
        observed_workflow_ids=["#V#tool_calling_workflow"],
        selector_diagnostics=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "gmail_oauth_or_profile_blocker"


def test_classify_replay_reports_tool_registration_before_gmail_profile_text() -> None:
    case = replay.ReplayCase(
        case_id="gmail-workflow-recovery-dispatch-failure",
        prompt="Represent a Gmail-linked arXiv paper",
        expected_workflow_id=replay.GMAIL_ARXIV_WORKFLOW_ID,
        expected_progress_fact_ids=(),
        expected_contract_ids=(),
        requires_gmail=True,
    )

    analysis = replay.classify_replay(
        case=case,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={
            "last_task_status": {
                "status": "completed",
                "progress": {
                    "result_summary": (
                        "The 'general_mail_review_workflow' tool is not "
                        "registered, preventing retrieval of Gmail messages. "
                        "Please confirm the Gmail profile."
                    ),
                },
            },
            "tool_history": [
                {
                    "tool": "general_mail_review_workflow",
                    "error": (
                        "mcp_invoke_failed:general_mail_review_workflow:"
                        "\"Method 'general_mail_review_workflow' not registered.\""
                    ),
                }
            ],
        },
        selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        observed_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        selector_diagnostics=[],
        progress_facts=[],
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "tool_registration_blocker"
    assert "general_mail_review_workflow" in analysis["blocker"]["observed_tools"]


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


def test_classify_replay_reports_context_build_blocker_before_selector() -> None:
    analysis = replay.classify_replay(
        case=replay.GMAIL_ARXIV_REPLAY_CASE,
        auth_login={"success": True},
        auth_status={"authenticated": True},
        gmail_preflight={"gmail_capability_ready": True},
        task_evidence={
            "last_task_status": {
                "status": "cancelled",
                "progress": {
                    "stage": "context_build",
                    "phase": "context_build",
                    "subtask": "request setup",
                },
            }
        },
        selected_workflow_ids=[],
        observed_workflow_ids=[],
        selector_diagnostics=[
            {"status": "running"},
            {"phase": "context_build", "status": "thinking"},
            {"status": "cancelled"},
        ],
        progress_facts=[],
        workflow_capability_preflight={
            "ready": True,
            "workflow_discovery_available": True,
            "status": "ready",
        },
    )

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "context_build_blocker"
    assert "no selector candidate set" in analysis["blocker"]["reason"]


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


def _idempotence_turn_report(
    turn_id: str,
    payload: dict,
    *,
    selected_workflow_ids: list[str] | None = None,
) -> dict:
    return {
        "turn_id": turn_id,
        "analysis": {
            "verdict": "pass",
            "blocker": None,
            "terminal_task_status": "completed",
            "selected_workflow_ids": selected_workflow_ids or [],
        },
        "last_task_status": {"status": "completed"},
        "task_result": {"result": payload},
        "progress_snapshots": [
            {
                "progress_facts": payload.get("progress_facts", []),
            }
        ],
    }


def _passing_idempotence_reports() -> list[dict]:
    target_payload = {
        "workflow_id": replay.GMAIL_ARXIV_WORKFLOW_ID,
        "mcp_tool": "gmail_get_message",
        "message_id": "msg-1",
        "message_processing_marker": "#V#gmail_message_processed_msg_1",
        "processed_message_id": "msg-1",
        "thread_id": "thread-1",
        "arxiv_id": "https://arxiv.org/abs/2406.15341v1",
        "paper_concept_id": "#V#paper_on_arxiv_2406_15341",
        "file_copy_concept_id": "#V#file_copy_2406_15341",
        "effective_gmail_query": "arxiv.org",
    }
    repeat_payload = {
        "paper_concept_id": "#V#paper_on_arxiv_2406_15341",
        "arxiv_id": "2406.15341",
        "processed_message_id": "msg-1",
        "message_processing_status": "processed",
        "effective_gmail_query": "arxiv.org",
    }
    verify_payload = {
        "paper_concept_id": "#V#paper_on_arxiv_2406_15341",
        "processed_message_id": "msg-1",
        "message_processing_marker": "#V#gmail_message_processed_msg_1",
    }
    return [
        _idempotence_turn_report(
            "initial",
            target_payload,
            selected_workflow_ids=[replay.GMAIL_ARXIV_WORKFLOW_ID],
        ),
        _idempotence_turn_report("repeat", repeat_payload),
        _idempotence_turn_report("verify", verify_payload),
    ]


def test_extract_gmail_arxiv_idempotence_evidence_from_nested_payload() -> None:
    report = _passing_idempotence_reports()[0]

    evidence = replay.extract_gmail_arxiv_idempotence_evidence(report)

    assert evidence["message_ids"] == ["msg-1"]
    assert evidence["thread_ids"] == ["thread-1"]
    assert evidence["arxiv_ids"] == ["2406.15341"]
    assert evidence["paper_concept_ids"] == ["#V#paper_on_arxiv_2406_15341"]
    assert evidence["file_copy_concept_ids"] == ["#V#file_copy_2406_15341"]
    assert "gmail_get_message" in evidence["observed_tools"]
    assert evidence["gmail_mutation_tools"] == []
    assert evidence["message_processing_marker_message_ids"] == ["msg-1"]
    assert evidence["message_processing_marker_seen"] is True


def test_analyse_gmail_arxiv_idempotence_sequence_passes_on_stable_ids() -> None:
    analysis = replay.analyse_gmail_arxiv_idempotence_sequence(
        _passing_idempotence_reports()
    )

    assert analysis["verdict"] == "pass"
    assert analysis["blocker"] is None
    assert analysis["target"]["gmail_message_id"] == "msg-1"
    assert analysis["target"]["arxiv_id"] == "2406.15341"
    assert analysis["target"]["paper_concept_id"] == "#V#paper_on_arxiv_2406_15341"
    assert analysis["target"]["file_copy_concept_ids"] == ["#V#file_copy_2406_15341"]


def test_analyse_gmail_arxiv_idempotence_sequence_blocks_missing_message_marker() -> None:
    reports = _passing_idempotence_reports()
    reports[0]["task_result"]["result"]["message_id"] = "msg-1"
    reports[0]["task_result"]["result"].pop("message_processing_marker")
    reports[0]["task_result"]["result"].pop("processed_message_id")

    analysis = replay.analyse_gmail_arxiv_idempotence_sequence(reports)

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "message_processing_marker_missing"


def test_analyse_gmail_arxiv_idempotence_sequence_blocks_gmail_write_tools() -> None:
    reports = _passing_idempotence_reports()
    reports[0]["task_result"]["result"]["mcp_tool"] = "gmail_modify_labels"
    reports[0]["task_result"]["result"]["tool_arguments"] = {
        "message_id": "msg-1",
        "add_labels": ["vontology/ingested"],
    }

    analysis = replay.analyse_gmail_arxiv_idempotence_sequence(reports)

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "gmail_write_tool_used"


def test_analyse_gmail_arxiv_idempotence_sequence_blocks_duplicate_paper_ids() -> None:
    reports = _passing_idempotence_reports()
    reports[1]["task_result"]["result"]["paper_concept_id"] = (
        "#V#paper_on_arxiv_2406_15341_duplicate"
    )

    analysis = replay.analyse_gmail_arxiv_idempotence_sequence(reports)

    assert analysis["verdict"] == "blocked"
    assert analysis["blocker"]["type"] == "duplicate_paper_concepts_observed"
