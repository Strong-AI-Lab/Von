from __future__ import annotations

import json

import pytest
import requests

from scripts import run_live_kb_tool_prompt_sampler as sampler


def test_prompt_bank_file_matches_embedded_payload() -> None:
    file_payload = json.loads(sampler.PROMPT_BANK_PATH.read_text(encoding="utf-8"))
    assert file_payload == sampler.PROMPT_BANK_PAYLOAD


def test_default_model_override_is_ollama_gemma4() -> None:
    assert sampler.DEFAULT_MODEL == "gemma4:26b"


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


def test_evaluate_user_happiness_flags_explicit_timeout_failure_response() -> None:
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
                "I couldn't complete that request because the authoritative "
                "conversation-turn workflow failed. "
                "workflow_llm_step_timeout:LLM call timed out after 45s "
                "(stage=llm.action, model=gemma4:26b)"
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
                ],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert any(
        "concrete failure or access marker" in reason.lower()
        for reason in evaluation["reasons"]
    )


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


def test_evaluate_user_happiness_requires_tool_use_for_operational_prompt() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "create_von_task_for_replay_review",
            "category": "von_task_creation",
            "complexity_class": "tool_augmented",
            "prompt": (
                "Create a Von task for me titled 'Review replay results' with a "
                "short description saying it came from the JVNAUTOSCI-1894 "
                "replay programme."
            ),
            "knowledge_surfaces": ["turn_context", "von_tasks"],
            "likely_tools": ["task_create"],
            "requires_tool_use": True,
        },
        generate_payload={
            "response": "I can help with that, but I would need to create the task first."
        },
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

    assert evaluation["should_user_be_happy"] is False
    assert any(
        "required operational tool use" in reason for reason in evaluation["reasons"]
    )


def test_evaluate_user_happiness_accepts_grounded_empty_operational_result() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "list_my_pending_von_tasks",
            "category": "von_task_listing",
            "complexity_class": "tool_augmented",
            "prompt": "List my pending Von tasks.",
            "knowledge_surfaces": ["turn_context", "von_tasks"],
            "likely_tools": ["task_list"],
            "requires_tool_use": True,
            "allows_grounded_empty_result": True,
        },
        generate_payload={
            "response": "I don't currently have any pending Von tasks for you."
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
                    {"tool": "task_list", "success": True},
                ],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is True
    assert evaluation["verdict"] == "happy"
    assert evaluation["reasons"] == []


def test_evaluate_user_happiness_rejects_inventory_only_claim_of_relationship() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "what_papers_of_mine_do_you_know_about",
            "prompt": "What papers of mine do you know about?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        generate_payload={
            "response": "I currently have 35 of your papers stored in my system."
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
                    {"tool": "list_papers", "success": True},
                ],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert any(
        "inventory-only tool evidence" in reason for reason in evaluation["reasons"]
    )


def test_choose_prompt_respects_complexity_class_filter() -> None:
    prompt = sampler._choose_prompt(
        sampler.PROMPT_BANK_PAYLOAD["prompts"],
        seed=7,
        prompt_id=None,
        allowed_complexity_classes=frozenset({"direct_context_or_background"}),
    )

    assert prompt["complexity_class"] == "direct_context_or_background"


def test_prompt_bank_includes_operational_task_and_message_cases() -> None:
    prompts = sampler.PROMPT_BANK_PAYLOAD["prompts"]
    by_id = {
        prompt["id"]: prompt
        for prompt in prompts
        if isinstance(prompt, dict) and isinstance(prompt.get("id"), str)
    }

    assert by_id["create_von_task_for_replay_review"]["likely_tools"] == [
        "task_create"
    ]
    assert by_id["mark_replay_related_von_task_in_progress"]["likely_tools"] == [
        "task_search",
        "task_update_status",
    ]
    assert by_id["send_myself_a_von_message_about_replay_results"][
        "requires_tool_use"
    ] is True
    assert by_id["count_my_unread_von_messages"][
        "allows_grounded_empty_result"
    ] is True


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


def test_build_model_arm_plan_includes_active_arm_and_deduplicates() -> None:
    arms = sampler._build_model_arm_plan(
        requested_model="gemma4:26b",
        compare_models=["gpt-5.4-mini", "gemma4:26b", "gpt-5.4-mini"],
        include_active_model_arm=True,
    )

    assert arms == [
        {
            "arm_id": "arm_1",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
        },
        {
            "arm_id": "arm_2",
            "label": "gemma4:26b",
            "requested_model": "gemma4:26b",
        },
        {
            "arm_id": "arm_3",
            "label": "gpt-5.4-mini",
            "requested_model": "gpt-5.4-mini",
        },
    ]
    assert (
        sampler._build_arm_session_name(
            base_session_name="Replay run",
            arm_metadata=arms[1],
        )
        == "Replay run [arm_2:gemma4:26b]"
    )


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
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
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
    assert summary["selection"]["prompt_bank_schema_version"] == "live_kb_tool_prompt_bank.v3"
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


def test_build_multi_arm_summary_reports_requested_arms_and_comparison() -> None:
    prompt_entry = {
        "id": "what_papers_of_mine_do_you_know_about",
        "category": "entity_relative_kb_lookup",
        "complexity_class": "vontology_grounded",
        "prompt": "What papers of mine do you know about?",
        "knowledge_surfaces": ["kb"],
        "likely_tools": ["search_knowledge_base"],
    }
    run_environment = {
        "base_url": "http://127.0.0.1:5000",
        "authenticated_user_concept_id": "#V#michael_witbrock",
        "session_name": "comparison run",
        "server_resolved_active_llm_model": "gpt-5.4-mini",
    }
    arm_a = sampler._build_summary(
        prompt_entry=prompt_entry,
        task_id="task-a",
        session_id="session-a",
        request_id="request-a",
        history_location={"history_index": 2, "session_id": "session-a"},
        generate_payload={"response": "Answer from gemma."},
        llm_debug_data={
            "model": "gemma4:26b",
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "dispatch_workflow_id": "#V#tool_calling_workflow",
                        "selected_execution_mode": "tool_pipeline",
                    }
                },
                "tool_history": [],
            },
        },
        evaluation={"verdict": "happy", "should_user_be_happy": True},
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model="gemma4:26b",
        run_environment={
            **run_environment,
            "requested_model": "gemma4:26b",
            "session_name": "comparison run [arm_1:gemma4:26b]",
        },
        arm_metadata={
            "arm_id": "arm_1",
            "label": "gemma4:26b",
            "requested_model": "gemma4:26b",
        },
    )
    arm_b = sampler._build_summary(
        prompt_entry=prompt_entry,
        task_id="task-b",
        session_id="session-b",
        request_id="request-b",
        history_location={"history_index": 2, "session_id": "session-b"},
        generate_payload={"response": "Answer from the active model."},
        llm_debug_data={
            "model": "gpt-5.4-mini",
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "dispatch_workflow_id": "#V#direct_response",
                        "selected_execution_mode": "direct_response",
                    }
                },
                "tool_history": [],
            },
        },
        evaluation={"verdict": "happy", "should_user_be_happy": True},
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model=None,
        run_environment={
            **run_environment,
            "requested_model": None,
            "session_name": "comparison run [arm_2:active_authenticated_model]",
        },
        arm_metadata={
            "arm_id": "arm_2",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
        },
    )

    summary = sampler._build_multi_arm_summary(
        prompt_entry=prompt_entry,
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model="gemma4:26b",
        requested_model_arms=[
            {
                "arm_id": "arm_1",
                "label": "gemma4:26b",
                "requested_model": "gemma4:26b",
            },
            {
                "arm_id": "arm_2",
                "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
                "requested_model": None,
            },
        ],
        run_environment=run_environment,
        arm_summaries=[arm_a, arm_b],
    )

    assert summary["mode"] == "multi_arm_comparison"
    assert summary["selection"]["requested_model"] == "gemma4:26b"
    assert summary["selection"]["requested_model_arms"] == [
        {
            "arm_id": "arm_1",
            "label": "gemma4:26b",
            "requested_model": "gemma4:26b",
        },
        {
            "arm_id": "arm_2",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
        },
    ]
    assert summary["comparison"]["arm_count"] == 2
    assert summary["comparison"]["happy_arm_count"] == 2
    assert summary["comparison"]["all_should_user_be_happy"] is True
    assert summary["comparison"]["telemetry_models"] == [
        "gemma4:26b",
        "gpt-5.4-mini",
    ]


def test_main_builds_multi_arm_comparison_from_one_prompt_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    choose_calls: list[dict[str, object]] = []
    replay_calls: list[dict[str, object]] = []

    monkeypatch.setattr(sampler, "_emit_replay_guide_note", lambda: None)
    monkeypatch.setattr(
        sampler,
        "_load_prompt_bank",
        lambda: {
            "schema_version": "live_kb_tool_prompt_bank.v3",
            "prompts": [
                {
                    "id": "who_am_i_in_this_conversation",
                    "category": "identity_context",
                    "complexity_class": "direct_context_or_background",
                    "prompt": "Who am I in this conversation?",
                    "knowledge_surfaces": ["turn_context"],
                    "likely_tools": [],
                }
            ],
        },
    )

    def fake_choose_prompt(*args: object, **kwargs: object) -> dict[str, object]:
        choose_calls.append({"args": args, "kwargs": kwargs})
        return {
            "id": "who_am_i_in_this_conversation",
            "category": "identity_context",
            "complexity_class": "direct_context_or_background",
            "prompt": "Who am I in this conversation?",
            "knowledge_surfaces": ["turn_context"],
            "likely_tools": [],
        }

    monkeypatch.setattr(sampler, "_choose_prompt", fake_choose_prompt)
    monkeypatch.setattr(
        sampler,
        "_collect_run_environment",
        lambda **kwargs: {
            "base_url": kwargs["base_url"],
            "requested_model": kwargs["requested_model"],
            "session_name": kwargs["session_name"],
        },
    )
    monkeypatch.setattr(
        sampler,
        "_augment_run_environment_with_server_diag",
        lambda **kwargs: {
            **kwargs["run_environment"],
            "server_reported_git_commit": "abc123",
        },
    )
    monkeypatch.setattr(
        sampler,
        "_augment_run_environment_with_active_llm_info",
        lambda **kwargs: {
            **kwargs["run_environment"],
            "server_resolved_active_llm_model": "gpt-5.4-mini",
        },
    )

    def fake_run_prompt_replay_arm(**kwargs: object) -> dict[str, object]:
        replay_calls.append(kwargs)
        arm = dict(kwargs["arm_metadata"])  # type: ignore[arg-type]
        requested_model = kwargs["requested_model"]
        telemetry_model = requested_model or "gpt-5.4-mini"
        return {
            "status": "ok",
            "arm": arm,
            "evaluation": {"should_user_be_happy": True},
            "telemetry": {
                "model": telemetry_model,
                "selected_workflow_id": "#V#direct_response",
                "selected_execution_mode": "direct_response",
            },
            "response": {"text": f"Response from {telemetry_model}"},
        }

    monkeypatch.setattr(sampler, "_run_prompt_replay_arm", fake_run_prompt_replay_arm)

    exit_code = sampler.main(
        [
            "--prompt-id",
            "who_am_i_in_this_conversation",
            "--model",
            "gemma4:26b",
            "--compare-model",
            "gpt-5.4-mini",
            "--include-active-model-arm",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert len(choose_calls) == 1
    assert [call["requested_model"] for call in replay_calls] == [
        None,
        "gemma4:26b",
        "gpt-5.4-mini",
    ]
    assert all(
        isinstance(prompt_entry := call.get("prompt_entry"), dict)
        and prompt_entry.get("prompt") == "Who am I in this conversation?"
        for call in replay_calls
    )
    assert output["mode"] == "multi_arm_comparison"
    assert output["comparison"]["arm_count"] == 3
    assert output["selection"]["requested_model_arms"] == [
        {
            "arm_id": "arm_1",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
        },
        {
            "arm_id": "arm_2",
            "label": "gemma4:26b",
            "requested_model": "gemma4:26b",
        },
        {
            "arm_id": "arm_3",
            "label": "gpt-5.4-mini",
            "requested_model": "gpt-5.4-mini",
        },
    ]
