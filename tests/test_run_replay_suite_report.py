from __future__ import annotations

import subprocess

import pytest

from scripts import run_replay_suite_report as replay_suite


def test_discovery_includes_prompt_bank_and_workflow_seed_cases() -> None:
    cases = replay_suite.discover_replay_cases(max_prompt_cases=5)

    assert any(
        replay_suite._safe_text(case.get("source_category")) == "prompt-bank"
        for case in cases
    )
    assert any(
        replay_suite._safe_text(case.get("source_category")) == "arxiv-workflow"
        for case in cases
    )
    prompt_case = next(
        case
        for case in cases
        if replay_suite._safe_text(case.get("source_category")) == "prompt-bank"
    )
    assert replay_suite._safe_text(prompt_case.get("prompt_id"))
    assert isinstance(prompt_case.get("required_tools"), list)
    assert (
        "#V#live_prompt_sampler_replay_evaluation_rubric_v1"
        not in (prompt_case["required_concepts"])
    )


def test_email_arxiv_prompt_bank_replay_is_discoverable() -> None:
    cases = replay_suite.discover_replay_cases(
        prompt_ids={"list_arxiv_titles_from_zhan_gmail_messages"}
    )
    case = next(
        case
        for case in cases
        if case.get("replay_id")
        == "prompt-bank:list_arxiv_titles_from_zhan_gmail_messages"
    )

    assert case["source_category"] == "prompt-bank"
    assert case["surface_exercised"] == "/von/generate"
    assert case["required_tools"] == [
        "get_paper_metadata",
        "gmail_get_message",
        "gmail_list_messages",
        "search_arxiv",
    ]
    assert case["required_workflows"] == ["#V#general_mail_review_workflow"]

    row = replay_suite._build_table_row(
        case,
        {
            "result": "dry_run",
            "health": "n/a",
            "failure_stall_reason": "",
            "evidence": [],
        },
    )
    assert "get_paper_metadata" in row["required_tools_workflows"]
    assert "#V#general_mail_review_workflow" in row["required_tools_workflows"]


def test_markdown_table_rendering_contains_expected_columns() -> None:
    markdown = replay_suite.render_markdown_table(
        [
            {
                "replay_id": "prompt-bank:who_am_i",
                "source": "scripts/live_kb_tool_prompt_bank.json",
                "what_it_tests": "identity_context",
                "surface_exercised": "/von/generate",
                "required_tools_workflows": "search_knowledge_base",
                "mode_environment": "AgentTest/local model",
                "result": "passed",
                "health": "1/1 (100%)",
                "failure_stall_reason": "",
                "evidence": "request_id=abc",
            }
        ]
    )

    assert "| Replay ID | Source | What it tests |" in markdown
    assert "prompt-bank:who_am_i" in markdown
    assert "request_id=abc" in markdown


def test_non_runnable_case_is_classified_without_execution() -> None:
    result = replay_suite.execute_replay_case(
        case={
            "replay_id": "workflow:#V#synthetic_workflow_regression_suite_workflow",
            "runnable": False,
            "not_runnable_reason": "No dedicated harness.",
        },
        base_url="http://127.0.0.1:5010",
        timeout_seconds=5.0,
        dry_run=False,
        model="gemma4:26b",
        allow_non_agent_test_server=False,
    )

    assert result["result"] == "not_runnable"
    assert "No dedicated harness" in result["failure_stall_reason"]


def test_timeout_is_classified_when_subprocess_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="python", timeout=0.01)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)
    result = replay_suite.execute_replay_case(
        case={
            "replay_id": "prompt-bank:who_am_i_in_this_conversation",
            "runnable": True,
            "execution_kind": "prompt_sampler",
            "prompt_id": "who_am_i_in_this_conversation",
        },
        base_url="http://127.0.0.1:5010",
        timeout_seconds=0.01,
        dry_run=False,
        model="gemma4:26b",
        allow_non_agent_test_server=False,
    )

    assert result["result"] == "timed_out"
    assert "timeout" in result["failure_stall_reason"].lower()


def test_prompt_sampler_subprocess_timeout_allows_post_cancel_reporting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["timeout"] = kwargs.get("timeout")
        command = args[0]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"status":"ok","repeat":{"collected_attempt_count":1,'
                '"attempt_count":1,"meets_minimum_collection_rate":true}}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = replay_suite.execute_replay_case(
        case={
            "replay_id": "prompt-bank:who_am_i_in_this_conversation",
            "runnable": True,
            "execution_kind": "prompt_sampler",
            "prompt_id": "who_am_i_in_this_conversation",
        },
        base_url="http://127.0.0.1:5010",
        timeout_seconds=5.0,
        dry_run=False,
        model="gemma4:26b",
        allow_non_agent_test_server=False,
    )

    assert result["result"] == "collected"
    assert captured["timeout"] == pytest.approx(25.0)


def test_prompt_sampler_failure_reason_reports_post_cancel_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = args[0]
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=(
                '{"status":"error","response":{"failure":{"message":"Background generate task did not complete before timeout",'
                '"background_task":{"cancellation_payload":{"post_cancellation_terminal":true,'
                '"post_cancellation_status_payload":{"status":"cancelled"}}}}}}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = replay_suite.execute_replay_case(
        case={
            "replay_id": "prompt-bank:who_am_i_in_this_conversation",
            "runnable": True,
            "execution_kind": "prompt_sampler",
            "prompt_id": "who_am_i_in_this_conversation",
        },
        base_url="http://127.0.0.1:5010",
        timeout_seconds=5.0,
        dry_run=False,
        model="gemma4:26b",
        allow_non_agent_test_server=False,
    )

    assert result["result"] == "failed"
    assert (
        "post-cancellation status=cancelled terminal=true"
        in result["failure_stall_reason"]
    )


def test_explicit_stronger_model_is_forwarded_without_a_premium_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(*args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = list(args[0])
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"ok"}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = replay_suite.execute_replay_case(
        case={
            "replay_id": "prompt-bank:case-1",
            "runnable": True,
            "execution_kind": "prompt_sampler",
            "prompt_id": "case-1",
        },
        base_url="http://127.0.0.1:5010",
        timeout_seconds=5.0,
        dry_run=False,
        model="openai:gpt-frontier",
        allow_non_agent_test_server=False,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[command.index("--model") + 1] == "openai:gpt-frontier"
    assert "--allow-premium-model" not in command
    assert result["result"] == "collected"


def test_filter_replay_cases_respects_explicit_source_filter() -> None:
    filtered = replay_suite.filter_replay_cases(
        [
            {"replay_id": "prompt-bank:who_am_i", "source_category": "prompt-bank"},
            {
                "replay_id": "workflow:#V#arxiv_paper_ingestion_testing_workflow",
                "source_category": "arxiv-workflow",
            },
        ],
        source_filters={"prompt-bank"},
        replay_ids=None,
    )

    assert [case["replay_id"] for case in filtered] == ["prompt-bank:who_am_i"]


def test_extract_prompt_sampler_failure_reason_prefers_nested_failure_message() -> None:
    reason = replay_suite._extract_prompt_sampler_failure_reason(
        {
            "response": {
                "failure": {
                    "message": "Background generate task did not complete before timeout"
                }
            },
            "evaluation": {"reasons": ["fallback reason should not be used"]},
        },
        stderr_text="stderr fallback",
        fallback="default fallback",
    )

    assert reason == "Background generate task did not complete before timeout"


def test_extract_prompt_sampler_failure_reason_strips_note_wrapped_json_error() -> None:
    reason = replay_suite._extract_prompt_sampler_failure_reason(
        {
            "error": (
                "NOTE: Use this random prompt sampler together with "
                "docs/engineering/real_path_server_replay_and_telemetry_loop.md\n"
                '{"status": "error", "error": "model \'gemma4:26b\' not found"}'
            )
        },
        stderr_text="",
        fallback="default fallback",
    )

    assert reason == "model 'gemma4:26b' not found"
