from __future__ import annotations

import json

import pytest
import requests

from scripts import run_live_kb_tool_prompt_sampler as sampler


def test_prompt_bank_file_matches_embedded_payload() -> None:
    file_payload = json.loads(sampler.PROMPT_BANK_PATH.read_text(encoding="utf-8"))
    assert file_payload == sampler.PROMPT_BANK_PAYLOAD


def test_evaluate_user_happiness_flags_dispatch_failure() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "what_papers_of_mine_do_you_know_about",
            "prompt": "What papers of mine do you know about?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        generate_payload={
            "response": "I don't currently have any papers of yours available from this conversation context."
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "custom_workflow",
                        "dispatch_terminal_failure_reason": "workflow_not_runnable",
                        "dispatch_terminal_failure_detail": (
                            "Workflow '#V#tool_calling_workflow' is not runnable; instance was not created."
                        ),
                    }
                },
                "tool_history": [],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert any("Dispatch failed" in reason for reason in evaluation["reasons"])


def test_evaluate_user_happiness_accepts_grounded_tool_answer() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "research_briefing_my_papers_recent_arxiv_and_jira",
            "prompt": (
                "Prepare a short research briefing for me: my represented papers, "
                "relevant recent arXiv work, and any linked Jira tasks."
            ),
            "knowledge_surfaces": ["kb", "arxiv", "jira"],
            "likely_tools": ["search_knowledge_base", "search_arxiv", "jira_search"],
        },
        generate_payload={
            "response": (
                "You have several represented papers on agent memory and symbolic "
                "reasoning. Recent arXiv work continues that theme, and the linked "
                "Jira issues are mainly about retrieval quality and paper workflows."
            )
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "tool_pipeline",
                        "dispatch_workflow_id": "#V#tool_calling_workflow",
                    }
                },
                "tool_history": [
                    {"tool": "search_knowledge_base", "success": True},
                    {"tool": "search_arxiv", "success": True},
                    {"tool": "jira_search", "success": True},
                ],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is True
    assert evaluation["verdict"] == "happy"
    assert evaluation["observed_tools"] == [
        "search_knowledge_base",
        "search_arxiv",
        "jira_search",
    ]


def test_evaluate_user_happiness_accepts_short_direct_answer() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "what_is_the_capital_of_france",
            "category": "general_knowledge",
            "complexity_class": "direct_context_or_background",
            "prompt": "What is the capital of France?",
            "knowledge_surfaces": ["background_knowledge"],
            "likely_tools": [],
        },
        generate_payload={"response": "Paris."},
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "direct_response",
                    }
                },
                "tool_history": [],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is True
    assert evaluation["verdict"] == "happy"
    assert evaluation["reasons"] == []


def test_choose_prompt_respects_complexity_class_filter() -> None:
    prompt = sampler._choose_prompt(
        sampler.PROMPT_BANK_PAYLOAD["prompts"],
        seed=7,
        prompt_id=None,
        allowed_complexity_classes=frozenset({"direct_context_or_background"}),
    )

    assert prompt["complexity_class"] == "direct_context_or_background"


def test_run_generate_background_omits_model_when_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_payloads: list[dict[str, object]] = []

    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        url = str(args[2])
        if url.endswith("/von/generate"):
            seen_payloads.append(dict(kwargs["json"]))  # type: ignore[index]
            return {"task_id": "task-123"}
        if url.endswith("/von/api/task/status/task-123"):
            return {"status": "completed"}
        if url.endswith("/von/api/task/result/task-123"):
            return {"result": {"response": "ok"}}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    task_id, generate_payload = sampler._run_generate_background(
        session=requests.Session(),
        base_url="http://127.0.0.1:5000",
        prompt="Who am I in this conversation?",
        model=None,
        timeout_seconds=30.0,
        poll_interval_seconds=0.2,
    )

    assert task_id == "task-123"
    assert generate_payload == {"response": "ok"}
    assert seen_payloads
    assert "model" not in seen_payloads[0]


def test_summarise_server_diag_extracts_relevant_server_fields() -> None:
    summary = sampler._summarise_server_diag(
        {
            "version": "v20250421_1015_backend+g1c5361c7f89b",
            "python_version": "3.13.12",
            "effective_user_concept_id": "#V#michael_witbrock",
            "header_user_concept_id": "#V#michael_witbrock",
            "session_user_concept_id": "#V#michael_witbrock",
            "uptime_sec": 5196.125129,
            "durable_workflow_startup": {"ready": True},
            "durable_workflows": {
                "worker_running": False,
                "scheduler_running": False,
            },
            "version_details": {
                "git_branch": "jvnautosci-1894-replay-programme",
                "git_commit": "abc123def456",
                "git_short_commit": "abc123d",
                "git_dirty": None,
            },
        }
    )

    assert summary["server_reported_version"] == "v20250421_1015_backend+g1c5361c7f89b"
    assert summary["server_reported_python_version"] == "3.13.12"
    assert summary["server_reported_git_branch"] == "jvnautosci-1894-replay-programme"
    assert summary["server_reported_git_commit"] == "abc123def456"
    assert summary["server_effective_user_concept_id"] == "#V#michael_witbrock"
    assert summary["server_durable_workflow_ready"] is True
    assert summary["server_worker_running"] is False


def test_summarise_active_llm_info_extracts_relevant_fields() -> None:
    summary = sampler._summarise_active_llm_info(
        {
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "status": "ready",
            "ping_ok": True,
            "error": None,
        }
    )

    assert summary["server_resolved_active_llm_provider"] == "openai"
    assert summary["server_resolved_active_llm_model"] == "gpt-5.4-mini"
    assert summary["server_resolved_active_llm_status"] == "ready"
    assert summary["server_resolved_active_llm_ping_ok"] is True
    assert summary["server_resolved_active_llm_error"] is None


def test_build_summary_includes_replay_guide_metadata() -> None:
    summary = sampler._build_summary(
        prompt_entry={
            "id": "what_papers_of_mine_do_you_know_about",
            "category": "entity_relative_kb_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What papers of mine do you know about?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        task_id="task-123",
        session_id="session-123",
        request_id="request-123",
        history_location={"history_index": 2, "session_id": "session-123"},
        generate_payload={"response": "Test response."},
        llm_debug_data={
            "model": "gpt-5.4-nano",
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "dispatch_workflow_id": "#V#tool_calling_workflow",
                        "selected_execution_mode": "tool_pipeline",
                    }
                },
                "tool_history": [],
            }
        },
        evaluation={"verdict": "happy", "should_user_be_happy": True},
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v2",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model="gemma4:26b",
        run_environment={
            "base_url": "http://127.0.0.1:5000",
            "authenticated_user_concept_id": "#V#michael_witbrock",
            "authenticated_organisation_concept_id": "university_of_auckland_strong_ai_lab",
            "local_repo_git_branch": "jvnautosci-1894-replay-programme",
            "local_repo_git_head": "abc123",
            "server_reported_git_branch": "jvnautosci-1894-replay-programme",
            "server_reported_git_commit": "abc123",
            "server_resolved_active_llm_provider": "ollama",
            "server_resolved_active_llm_model": "gemma4:26b",
            "requested_model": "gemma4:26b",
            "run_started_at_utc": "2026-04-16T19:00:00+00:00",
            "session_name": "test session",
        },
    )

    assert summary["guidance"]["replay_guide_path"] == sampler.REAL_PATH_REPLAY_GUIDE
    assert "real_path_server_replay_and_telemetry_loop.md" in summary["guidance"][
        "replay_guide_note"
    ]
    assert summary["prompt"]["complexity_class"] == "vontology_grounded"
    assert summary["selection"]["prompt_bank_schema_version"] == "live_kb_tool_prompt_bank.v2"
    assert summary["selection"]["requested_complexity_classes"] == [
        "vontology_grounded"
    ]
    assert summary["selection"]["seed"] == 17
    assert summary["selection"]["requested_model"] == "gemma4:26b"
    assert summary["environment"]["base_url"] == "http://127.0.0.1:5000"
    assert summary["environment"]["authenticated_user_concept_id"] == "#V#michael_witbrock"
    assert summary["environment"]["local_repo_git_branch"] == "jvnautosci-1894-replay-programme"
    assert summary["environment"]["server_reported_git_branch"] == "jvnautosci-1894-replay-programme"
    assert summary["environment"]["server_resolved_active_llm_model"] == "gemma4:26b"
