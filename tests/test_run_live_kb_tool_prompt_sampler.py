from __future__ import annotations

import json

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
