from __future__ import annotations

import json

import pytest
import requests

from scripts import run_live_kb_tool_prompt_sampler as sampler


def test_prompt_bank_file_matches_embedded_payload() -> None:
    file_payload = json.loads(sampler.PROMPT_BANK_PATH.read_text(encoding="utf-8"))
    assert file_payload == sampler.PROMPT_BANK_PAYLOAD


def test_write_json_output_creates_parent_directories(tmp_path) -> None:
    output_path = tmp_path / "nested" / "replay" / "summary.json"

    sampler._write_json_output(str(output_path), {"status": "ok"})

    assert json.loads(output_path.read_text(encoding="utf-8")) == {"status": "ok"}


def test_default_model_override_is_ollama_gemma4() -> None:
    assert sampler.DEFAULT_MODEL == "gemma4:26b"


def test_infer_provider_from_model_identifier_treats_ollama_tags_as_local() -> None:
    assert sampler._infer_provider_from_model_identifier("gemma4:e4b") == "ollama"
    assert sampler._infer_provider_from_model_identifier("ollama:llama3.1:8b") == "ollama"


def test_model_policy_rejects_premium_model_without_explicit_opt_in() -> None:
    report = sampler._build_model_policy_report(
        requested_model_arms=[
            {"arm_id": "arm_1", "label": "gpt-5.4-mini", "requested_model": "gpt-5.4-mini"}
        ],
        run_environment={"server_resolved_active_llm_model": "gemma4:26b"},
        allow_premium_model=False,
    )

    assert report["premium_model_deviation"] is True
    assert report["premium_model_deviation_count"] == 1
    with pytest.raises(RuntimeError, match="local-only model execution"):
        sampler._enforce_model_policy(report)


def test_model_policy_allows_local_ollama_default() -> None:
    report = sampler._build_model_policy_report(
        requested_model_arms=[
            {"arm_id": "arm_1", "label": "gemma4:26b", "requested_model": "gemma4:26b"}
        ],
        run_environment={"server_resolved_active_llm_model": "gpt-5.4-mini"},
        allow_premium_model=False,
    )

    sampler._enforce_model_policy(report)
    assert report["local_only_default"] is True
    assert report["premium_model_deviation"] is False
    assert report["arms"][0]["effective_provider"] == "ollama"


def test_model_policy_rejects_provider_prefixed_premium_model() -> None:
    report = sampler._build_model_policy_report(
        requested_model_arms=[
            {
                "arm_id": "arm_1",
                "label": "openai:gpt-5.4-mini",
                "requested_model": "openai:gpt-5.4-mini",
            }
        ],
        run_environment={},
        allow_premium_model=False,
    )

    assert report["premium_model_deviation"] is True
    assert report["arms"][0]["effective_provider"] == "openai"
    with pytest.raises(RuntimeError, match="local-only model execution"):
        sampler._enforce_model_policy(report)


def test_model_policy_reports_premium_opt_in() -> None:
    report = sampler._build_model_policy_report(
        requested_model_arms=[
            {"arm_id": "arm_1", "label": "gpt-5.4-mini", "requested_model": "gpt-5.4-mini"}
        ],
        run_environment={"server_resolved_active_llm_provider": "openai"},
        allow_premium_model=True,
    )

    sampler._enforce_model_policy(report)
    assert report["premium_model_allowed"] is True
    assert report["premium_model_deviation"] is True


def test_default_base_url_targets_agent_test_instance() -> None:
    assert sampler.DEFAULT_BASE_URL == "http://127.0.0.1:5010"


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


def test_evaluate_user_happiness_flags_canonical_concept_id_near_miss() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "represented_self_facts_vs_inferences",
            "category": "epistemic_summary",
            "complexity_class": "vontology_grounded",
            "prompt": (
                "Tell me about myself as represented here, but separate "
                "established facts from likely inferences."
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["fetch_concept", "get_predicate_incidence"],
        },
        generate_payload={
            "response": (
                "Based on the Vontology, here is a self-representation audit "
                "for **#V#michael_switbrock** with established facts and likely "
                "inferences separated below."
            )
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "custom_workflow",
                        "dispatch_workflow_id": "#V#entity_information_retrieval_workflow",
                    }
                },
                "tool_history": [
                    {"tool": "fetch_concept", "success": True},
                    {"tool": "get_predicate_incidence", "success": True},
                ],
            }
        },
        run_environment={
            "authenticated_user_concept_id": "#V#michael_witbrock",
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert any(
        "Canonical concept ID mismatch" in reason
        and "#V#michael_switbrock" in reason
        for reason in evaluation["reasons"]
    )
    fidelity = evaluation["canonical_concept_id_fidelity"]
    assert fidelity["status"] == "failed"
    assert fidelity["expected_concept_ids"] == ["#V#michael_witbrock"]
    assert fidelity["observed_concept_ids"] == ["#V#michael_switbrock"]
    assert fidelity["findings"] == [
        {
            "reason_code": "canonical_concept_id_mismatch",
            "severity": "failed",
            "expected_concept_id": "#V#michael_witbrock",
            "observed_concept_id": "#V#michael_switbrock",
            "edit_distance": 1,
            "message": (
                "Response displayed a near-miss Vontology concept ID "
                "#V#michael_switbrock where canonical ID "
                "#V#michael_witbrock was the expected grounded subject."
            ),
        }
    ]


def test_evaluate_user_happiness_accepts_exact_canonical_concept_id() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "represented_self_facts_vs_inferences",
            "category": "epistemic_summary",
            "complexity_class": "vontology_grounded",
            "prompt": "Tell me about myself as represented here.",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["fetch_concept"],
        },
        generate_payload={
            "response": (
                "The represented subject is #V#michael_witbrock: with related "
                "paper evidence such as #V#learning_to_tell_two_spirals_apart."
            )
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "custom_workflow",
                    }
                },
                "tool_history": [{"tool": "fetch_concept", "success": True}],
            }
        },
        run_environment={
            "authenticated_user_concept_id": "#V#michael_witbrock",
        },
    )

    assert evaluation["should_user_be_happy"] is True
    assert evaluation["canonical_concept_id_fidelity"]["status"] == "passed"
    assert evaluation["canonical_concept_id_fidelity"]["observed_concept_ids"] == [
        "#V#michael_witbrock",
        "#V#learning_to_tell_two_spirals_apart",
    ]
    assert evaluation["canonical_concept_id_fidelity"]["findings"] == []


def test_evaluate_user_happiness_accepts_unrelated_retrieved_concept_id() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "represented_self_facts_vs_inferences",
            "category": "epistemic_summary",
            "complexity_class": "vontology_grounded",
            "prompt": "Tell me about myself as represented here.",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["fetch_concept"],
        },
        generate_payload={
            "response": (
                "The answer cites represented evidence from "
                "#V#learning_to_tell_two_spirals_apart and avoids fabricating a "
                "subject identifier."
            )
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "custom_workflow",
                    }
                },
                "tool_history": [{"tool": "fetch_concept", "success": True}],
            }
        },
        run_environment={
            "authenticated_user_concept_id": "#V#michael_witbrock",
        },
    )

    assert evaluation["should_user_be_happy"] is True
    fidelity = evaluation["canonical_concept_id_fidelity"]
    assert fidelity["status"] == "passed"
    assert fidelity["expected_concept_ids"] == ["#V#michael_witbrock"]
    assert fidelity["observed_concept_ids"] == [
        "#V#learning_to_tell_two_spirals_apart"
    ]
    assert fidelity["findings"] == []


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


def test_evaluate_user_happiness_flags_entity_retrieval_inability_marker() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "students_or_collaborators_and_relationships",
            "category": "relation_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": (
                "What students or collaborators of mine are represented in the KB, "
                "and what is my relationship to each?"
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        generate_payload={
            "response": (
                "I am unable to retrieve the information about your students or "
                "collaborators because the necessary tool was not permitted for "
                "this operation."
            )
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "custom_workflow",
                        "dispatch_workflow_id": "#V#entity_information_retrieval_workflow",
                    }
                },
                "tool_history": [],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert any(
        "concrete failure or access marker" in reason.lower()
        for reason in evaluation["reasons"]
    )


def test_evaluate_user_happiness_flags_missing_workflow_required_evidence_tools() -> (
    None
):
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "represented_self_facts_vs_inferences",
            "category": "epistemic_summary",
            "complexity_class": "vontology_grounded",
            "prompt": (
                "Tell me about myself as represented here, but separate "
                "established facts from likely inferences."
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        generate_payload={
            "response": (
                "The represented facts show your research network and projects."
            )
        },
        llm_debug_data={
            "tool_invocations": [{"tool": "find_relations_with_argument"}],
            "turn_execution_record": {
                "execution": {
                    "workflow_required_effects_contract": {
                        "schema_version": "workflow_required_effects_contract.v1",
                        "contract_id": "grounded_entity_information_retrieval_evidence",
                        "required_effects": [
                            {
                                "effect_id": "grounded_entity_information_evidence",
                                "effect_type": "grounded_evidence",
                                "required_tools": [
                                    "get_predicate_incidence",
                                    "find_relations_with_argument",
                                ],
                                "required_tools_match": "all",
                            }
                        ],
                    }
                }
            },
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "custom_workflow",
                        "dispatch_workflow_id": "#V#entity_information_retrieval_workflow",
                    }
                },
                "tool_history": [],
            },
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert any(
        "workflow-authored required evidence" in reason.lower()
        and "get_predicate_incidence" in reason
        for reason in evaluation["reasons"]
    )
    assert evaluation["missing_answer_evidence"] == [
        {
            "effect_id": "grounded_entity_information_evidence",
            "effect_type": "grounded_evidence",
            "required_tools": [
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "missing_tools": ["get_predicate_incidence"],
            "match": "all",
            "requirement_source": "workflow_required_effects_contract",
            "user_answer_required": True,
            "reason": (
                "Workflow-authored required evidence was not retrieved for "
                "grounded_entity_information_evidence; missing tools: "
                "get_predicate_incidence."
            ),
        }
    ]


def test_evaluate_user_happiness_keeps_diagnostic_only_evidence_out_of_ontology_verdict() -> (
    None
):
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "key_predicates_for_scientific_papers",
            "category": "ontology_predicate_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What are key predicates for scientific papers in Vontology?",
            "knowledge_surfaces": ["background_knowledge", "kb"],
            "likely_tools": ["search_concepts", "get_predicate_incidence"],
        },
        generate_payload={
            "response": (
                "Based on Vontology predicate incidence for represented scientific "
                "paper instances, key predicates include Has Author, Has First "
                "Author, hasName, and hasContent."
            )
        },
        llm_debug_data={
            "tool_invocations": [
                {"tool": "search_concepts"},
                {"tool": "get_predicate_incidence"},
            ],
            "turn_execution_record": {
                "execution": {
                    "workflow_required_effects_contract": {
                        "schema_version": "workflow_required_effects_contract.v1",
                        "required_effects": [
                            {
                                "effect_id": "conversation_locator",
                                "effect_type": "diagnostic_evidence",
                                "required_tools": [
                                    "conversation_telemetry_get_locator"
                                ],
                            },
                            {
                                "effect_id": "conversation_history",
                                "effect_type": "diagnostic_evidence",
                                "required_tools": [
                                    "chat_history_get_segments",
                                    "chat_history_get_debug_entry",
                                ],
                                "required_tools_match": "all",
                            },
                        ],
                    }
                }
            },
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "tool_pipeline",
                        "dispatch_workflow_id": "#V#tool_calling_workflow",
                    }
                },
                "tool_history": [],
            },
        },
    )

    assert evaluation["should_user_be_happy"] is True
    assert evaluation["verdict"] == "happy"
    assert evaluation["reasons"] == []
    assert evaluation["diagnostic_evidence_complete"] is False
    assert len(evaluation["diagnostic_evidence_reasons"]) == 2
    assert evaluation["missing_answer_evidence"] == []
    assert [
        entry["effect_id"] for entry in evaluation["missing_diagnostic_evidence"]
    ] == ["conversation_locator", "conversation_history"]
    assert all(
        entry["requirement_source"] == "workflow_required_effects_contract"
        for entry in evaluation["missing_evidence"]
    )
    assert all(
        entry["user_answer_required"] is False
        for entry in evaluation["missing_diagnostic_evidence"]
    )


def test_evaluate_user_happiness_flags_dispatch_missing_answer_required_tools() -> None:
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "key_predicates_for_scientific_papers",
            "category": "ontology_predicate_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What are key predicates for scientific papers in Vontology?",
            "knowledge_surfaces": ["background_knowledge", "kb"],
            "likely_tools": ["search_concepts", "get_predicate_incidence"],
        },
        generate_payload={
            "response": (
                "I couldn't complete that request because the authoritative "
                "conversation-turn workflow did not produce a user-visible response."
            )
        },
        llm_debug_data={
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "tool_pipeline",
                        "dispatch_workflow_id": "#V#tool_calling_workflow",
                        "required_effects_required_tools": [
                            "vontology_concept_search",
                            "fetch_concept",
                            "get_predicate_incidence",
                        ],
                        "required_effects_missing_required_tools": [
                            "get_predicate_incidence"
                        ],
                        "required_effects_unresolved_effect_ids": [
                            "effect_prompt_required_evidence_get_predicate_incidence"
                        ],
                        "required_effects_unresolved_effect_types": [
                            "required_evidence"
                        ],
                    }
                },
                "tool_history": [
                    {"tool": "vontology_concept_search", "success": True},
                    {"tool": "fetch_concept", "success": False},
                ],
            }
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert any(
        "concrete failure or access marker" in reason.lower()
        for reason in evaluation["reasons"]
    )
    assert any(
        entry["requirement_source"] == "workflow_dispatch_required_effects"
        and entry["missing_tools"] == ["get_predicate_incidence"]
        and entry["user_answer_required"] is True
        for entry in evaluation["missing_answer_evidence"]
    )


def test_evaluate_user_happiness_fails_missing_diagnostic_tools_for_diagnostic_prompt() -> (
    None
):
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry={
            "id": "diagnose_last_turn_telemetry",
            "category": "turn_diagnostics",
            "complexity_class": "tool_augmented",
            "prompt": "Diagnose the last conversation turn telemetry.",
            "knowledge_surfaces": ["conversation_telemetry"],
            "likely_tools": [
                "conversation_telemetry_get_locator",
                "chat_history_get_segments",
            ],
            "requires_diagnostic_evidence": True,
        },
        generate_payload={
            "response": (
                "The previous turn appears to have selected a workflow, but the "
                "diagnostic locator and chat history were not inspected."
            )
        },
        llm_debug_data={
            "turn_execution_record": {
                "execution": {
                    "workflow_required_effects_contract": {
                        "required_effects": [
                            {
                                "effect_id": "conversation_locator",
                                "effect_type": "diagnostic_evidence",
                                "required_tools": [
                                    "conversation_telemetry_get_locator"
                                ],
                            },
                            {
                                "effect_id": "conversation_history",
                                "effect_type": "diagnostic_evidence",
                                "required_tools": ["chat_history_get_segments"],
                            },
                        ]
                    }
                }
            },
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "selected_execution_mode": "tool_pipeline",
                        "dispatch_workflow_id": "#V#tool_calling_workflow",
                    }
                },
                "tool_history": [],
            },
        },
    )

    assert evaluation["should_user_be_happy"] is False
    assert evaluation["verdict"] == "unhappy"
    assert evaluation["diagnostic_evidence_complete"] is False
    assert [entry["effect_id"] for entry in evaluation["missing_answer_evidence"]] == [
        "conversation_locator",
        "conversation_history",
    ]
    assert all(
        entry["user_answer_required"] is True
        for entry in evaluation["missing_diagnostic_evidence"]
    )
    assert any(
        "conversation_telemetry_get_locator" in reason
        for reason in evaluation["reasons"]
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

    assert by_id["create_von_task_for_replay_review"]["likely_tools"] == ["task_create"]
    assert by_id["mark_replay_related_von_task_in_progress"]["likely_tools"] == [
        "task_search",
        "task_update_status",
    ]
    assert (
        by_id["send_myself_a_von_message_about_replay_results"]["requires_tool_use"]
        is True
    )
    assert by_id["count_my_unread_von_messages"]["allows_grounded_empty_result"] is True


def test_prompt_bank_includes_trivial_text_relation_replay_case() -> None:
    prompts = sampler.PROMPT_BANK_PAYLOAD["prompts"]
    by_id = {
        prompt["id"]: prompt
        for prompt in prompts
        if isinstance(prompt, dict) and isinstance(prompt.get("id"), str)
    }

    prompt = by_id["text_relations_for_michael_witbrock_concept"]
    assert prompt["category"] == "represented_relation_lookup"
    assert prompt["complexity_class"] == "vontology_grounded"
    assert prompt["likely_tools"] == [
        "search_concepts",
        "get_text_relations_summary",
        "get_text_relations",
    ]
    assert prompt["requires_tool_use"] is True


def test_prompt_bank_includes_jira_replay_regressions() -> None:
    prompts = sampler.PROMPT_BANK_PAYLOAD["prompts"]
    by_id = {
        prompt["id"]: prompt
        for prompt in prompts
        if isinstance(prompt, dict) and isinstance(prompt.get("id"), str)
    }

    direct_issue = by_id["tell_me_about_jvnautosci_150_in_jira"]
    assert direct_issue["prompt"] == "Tell me about JVNAUTOSCI-150 in JIRA"
    assert direct_issue["likely_tools"] == ["jira_get_issue"]
    assert direct_issue["requires_tool_use"] is True

    parent_subtasks = by_id["parent_and_subtasks_for_jvnautosci_150"]
    assert parent_subtasks["likely_tools"] == ["jira_get_issue"]
    assert parent_subtasks["category"] == "single_tool_jira_summary"

    repair_task = by_id["summarise_jvnautosci_2097_tool_plan_repair_task"]
    assert repair_task["likely_tools"] == ["jira_get_issue"]
    assert "tool-calling failure" in repair_task["prompt"]

    repair_search = by_id["which_jira_task_tracks_tool_call_repair_critic"]
    assert repair_search["likely_tools"] == ["jira_search"]
    assert repair_search["category"] == "single_tool_jira_search"

    gmail_listing = by_id["list_last_ten_zhan_gmail_messages"]
    assert gmail_listing["category"] == "single_tool_gmail_listing"
    assert gmail_listing["complexity_class"] == "tool_augmented"
    assert gmail_listing["likely_tools"] == [
        "gmail_list_messages",
        "gmail_get_message",
    ]
    assert gmail_listing["requires_tool_use"] is True


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
        presenter_mode=False,
        timeout_seconds=30.0,
        poll_interval_seconds=0.2,
    )

    assert task_id == "task-123"
    assert generate_payload == {"response": "ok"}
    assert seen_payloads
    assert "model" not in seen_payloads[0]
    assert "presenter_mode" not in seen_payloads[0]


def test_run_generate_background_can_request_presenter_mode(
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
        prompt="Summarise this for the presenter UI",
        model="gpt-5.4-mini",
        presenter_mode=True,
        timeout_seconds=30.0,
        poll_interval_seconds=0.2,
    )

    assert task_id == "task-123"
    assert generate_payload == {"response": "ok"}
    assert seen_payloads
    assert seen_payloads[0]["model"] == "gpt-5.4-mini"
    assert seen_payloads[0]["presenter_mode"] is True


def test_run_generate_background_sends_local_model_provider_override(
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

    sampler._run_generate_background(
        session=requests.Session(),
        base_url="http://127.0.0.1:5000",
        prompt="What text relations are used with the concept for Michael Witbrock?",
        model="gemma4:e4b",
        presenter_mode=False,
        timeout_seconds=30.0,
        poll_interval_seconds=0.2,
    )

    assert seen_payloads
    assert seen_payloads[0]["model"] == "gemma4:e4b"
    assert seen_payloads[0]["model_provider"] == "ollama"
    assert seen_payloads[0]["selected_model_provider"] == "ollama"


def test_run_generate_background_cancels_task_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        url = str(args[2])
        calls.append(url)
        if url.endswith("/von/generate"):
            return {"task_id": "task-stalled"}
        if url.endswith("/von/api/task/status/task-stalled"):
            return {
                "status": "running",
                "task_id": "task-stalled",
                "progress": {"step": "llm.action"},
            }
        if url.endswith("/von/api/task/cancel/task-stalled"):
            return {"success": True, "task_id": "task-stalled"}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)
    monkeypatch.setattr(sampler.time, "time", iter([100.0, 101.0, 131.0]).__next__)
    monkeypatch.setattr(sampler.time, "sleep", lambda _seconds: None)

    with pytest.raises(sampler.BackgroundGenerateTaskError) as exc_info:
        sampler._run_generate_background(
            session=requests.Session(),
            base_url="http://127.0.0.1:5010",
            prompt="What text relations are used with the concept for Michael Witbrock?",
            model="gemma4:26b",
            presenter_mode=False,
            timeout_seconds=30.0,
            poll_interval_seconds=0.2,
        )

    assert exc_info.value.task_id == "task-stalled"
    assert exc_info.value.status_payload["status"] == "running"
    assert exc_info.value.cancellation_payload == {
        "success": True,
        "task_id": "task-stalled",
    }
    assert calls[-1].endswith("/von/api/task/cancel/task-stalled")


def test_main_writes_single_attempt_failure_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "single_attempt_failure.json"

    monkeypatch.setattr(sampler, "_emit_replay_guide_note", lambda: None)
    monkeypatch.setattr(
        sampler,
        "_load_prompt_bank",
        lambda: {"schema_version": "live_kb_tool_prompt_bank.v3", "prompts": []},
    )
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
            "server_agent_test_instance": True,
        },
    )

    def fake_run_replay_plan(**kwargs: object) -> tuple[dict[str, object], bool]:
        raise sampler.BackgroundGenerateTaskError(
            "Background generate task did not complete before timeout",
            task_id="task-stalled",
            status_payload={"status": "running", "task_id": "task-stalled"},
            cancellation_payload={"success": True, "task_id": "task-stalled"},
        )

    monkeypatch.setattr(sampler, "_run_replay_plan", fake_run_replay_plan)

    exit_code = sampler.main(
        [
            "--prompt-text",
            "What text relations are used with the concept for Michael Witbrock?",
            "--model",
            "gemma4:e4b",
            "--output-json",
            str(output_path),
        ]
    )

    assert exit_code == 1
    output = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert output == written
    assert written["status"] == "error"
    assert written["conversation"]["background_task_id"] == "task-stalled"
    failure = written["response"]["failure"]
    assert failure["background_task"]["status_payload"]["status"] == "running"
    assert failure["background_task"]["cancellation_payload"] == {
        "success": True,
        "task_id": "task-stalled",
    }


def test_replay_session_creation_payload_marks_sampler_chat_as_test_run() -> None:
    payload = sampler._build_replay_session_creation_payload("Replay run")

    assert payload == {
        "session_name": "Replay run",
        "origin_kind": "coding_agent_test",
        "created_by_actor_concept_id": "#V#von_system",
        "created_by_actor_type": "#V#coding_agent",
        "is_agent_created": True,
        "test_artifact_kind": "live_kb_tool_prompt_sampler_chat_session",
    }


def test_multi_arm_session_creation_payload_keeps_test_run_provenance() -> None:
    session_name = sampler._build_arm_session_name(
        base_session_name="Replay run",
        arm_metadata={
            "arm_id": "arm_2",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
        },
    )

    payload = sampler._build_replay_session_creation_payload(session_name)

    assert payload["session_name"] == "Replay run [arm_2:active_authenticated_model]"
    assert payload["origin_kind"] == "coding_agent_test"
    assert payload["is_agent_created"] is True
    assert payload["test_artifact_kind"] == "live_kb_tool_prompt_sampler_chat_session"


def test_establish_authenticated_session_sends_test_run_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_create_payloads: list[dict[str, object]] = []

    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        url = str(args[2])
        if url.endswith("/von/api/session/set_user_concept"):
            return {"ok": True}
        if url.endswith("/von/api/session/set_organisation"):
            return {"ok": True}
        if url.endswith("/von/api/session/context"):
            return {"user_id": "#V#michael_witbrock"}
        if url.endswith("/von/api/session/create_chat_session"):
            seen_create_payloads.append(dict(kwargs["json"]))  # type: ignore[index]
            return {"session_id": "session-123"}
        if url.endswith("/von/reset"):
            return {"ok": True}
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    session_id, window_session_id = sampler._establish_authenticated_session(
        session=requests.Session(),
        base_url="http://127.0.0.1:5000",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="university_of_auckland_strong_ai_lab",
        session_name="JVNAUTOSCI-1894 live prompt sample",
    )

    assert session_id == "session-123"
    assert window_session_id
    assert seen_create_payloads == [
        {
            "session_name": "JVNAUTOSCI-1894 live prompt sample",
            "origin_kind": "coding_agent_test",
            "created_by_actor_concept_id": "#V#von_system",
            "created_by_actor_type": "#V#coding_agent",
            "is_agent_created": True,
            "test_artifact_kind": "live_kb_tool_prompt_sampler_chat_session",
        }
    ]


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
            "requested_provider": None,
        },
        {
            "arm_id": "arm_2",
            "label": "gemma4:26b",
            "requested_model": "gemma4:26b",
            "requested_provider": "ollama",
        },
        {
            "arm_id": "arm_3",
            "label": "gpt-5.4-mini",
            "requested_model": "gpt-5.4-mini",
            "requested_provider": "openai",
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
            "agent_test_instance": True,
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
    assert summary["server_agent_test_instance"] is True


def test_augment_run_environment_with_server_diag_prefers_health_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        url = str(args[2])
        calls.append(url)
        if url.endswith("/health"):
            return {
                "version": "v20250421_1015_backend+gabc123",
                "agent_test_instance": True,
                "version_details": {
                    "git_branch": "main",
                    "git_commit": "abc123",
                    "git_short_commit": "abc123",
                    "git_dirty": None,
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    summary = sampler._augment_run_environment_with_server_diag(
        session=requests.Session(),
        base_url="http://127.0.0.1:5000",
        run_environment={"base_url": "http://127.0.0.1:5000"},
    )

    assert calls == ["http://127.0.0.1:5000/health"]
    assert summary["server_metadata_source"] == "health"
    assert summary["server_metadata_error"] is None
    assert summary["server_reported_git_branch"] == "main"
    assert summary["server_reported_git_commit"] == "abc123"


def test_augment_run_environment_with_server_diag_records_lookup_error_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        url = str(args[2])
        calls.append(url)
        raise RuntimeError(f"timeout for {url}")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    summary = sampler._augment_run_environment_with_server_diag(
        session=requests.Session(),
        base_url="http://127.0.0.1:5000",
        run_environment={"base_url": "http://127.0.0.1:5000"},
    )

    assert calls == [
        "http://127.0.0.1:5000/health",
        "http://127.0.0.1:5000/diag",
    ]
    assert summary["server_metadata_source"] is None
    assert "diag:" in str(summary["server_metadata_error"])


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


def test_augment_run_environment_with_active_llm_info_records_lookup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("llm info timeout")

    monkeypatch.setattr(sampler, "_request_json", fake_request_json)

    summary = sampler._augment_run_environment_with_active_llm_info(
        session=requests.Session(),
        base_url="http://127.0.0.1:5000",
        user_concept_id="#V#michael_witbrock",
        organisation_concept_id="university_of_auckland_strong_ai_lab",
        run_environment={"base_url": "http://127.0.0.1:5000"},
    )

    assert summary["server_resolved_active_llm_lookup_error"] == "llm info timeout"


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
            },
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
            "server_metadata_source": "health",
            "server_metadata_error": None,
            "server_resolved_active_llm_provider": "ollama",
            "server_resolved_active_llm_model": "gemma4:26b",
            "server_resolved_active_llm_lookup_error": None,
            "requested_model": "gemma4:26b",
            "run_started_at_utc": "2026-04-16T19:00:00+00:00",
            "session_name": "test session",
        },
    )

    assert summary["guidance"]["replay_guide_path"] == sampler.REAL_PATH_REPLAY_GUIDE
    assert (
        "real_path_server_replay_and_telemetry_loop.md"
        in summary["guidance"]["replay_guide_note"]
    )
    assert summary["prompt"]["complexity_class"] == "vontology_grounded"
    assert (
        summary["selection"]["prompt_bank_schema_version"]
        == "live_kb_tool_prompt_bank.v3"
    )
    assert summary["selection"]["requested_complexity_classes"] == [
        "vontology_grounded"
    ]
    assert summary["selection"]["seed"] == 17
    assert summary["selection"]["requested_model"] == "gemma4:26b"
    assert summary["environment"]["base_url"] == "http://127.0.0.1:5000"
    assert (
        summary["environment"]["authenticated_user_concept_id"] == "#V#michael_witbrock"
    )
    assert (
        summary["environment"]["local_repo_git_branch"]
        == "jvnautosci-1894-replay-programme"
    )
    assert (
        summary["environment"]["server_reported_git_branch"]
        == "jvnautosci-1894-replay-programme"
    )
    assert summary["environment"]["server_resolved_active_llm_model"] == "gemma4:26b"
    model_report = summary["model_portfolio_evaluation"]
    assert model_report["schema_version"] == "model_portfolio_replay_report.v1"
    assert model_report["replay_set_id"] == "JVNAUTOSCI-1894"
    assert model_report["stage_evidence_schema_version"] == (
        "model_stage_suitability_evidence.v1"
    )
    assert [entry["workflow_stage"] for entry in model_report["stage_evidence"]] == [
        "workflow_selector",
        "turn_answer",
    ]
    assert model_report["certification_decision"]["promotion_authorised"] is False
    assert "insufficient_distinct_replay_cases" in (
        model_report["certification_decision"]["promotion_blockers"]
    )


def test_build_summary_flags_empty_success_llm_output_as_suspect() -> None:
    summary = sampler._build_summary(
        prompt_entry={
            "id": "represented_self_facts_vs_inferences",
            "category": "epistemic_summary",
            "complexity_class": "vontology_grounded",
            "prompt": "Tell me about myself as represented here.",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        task_id="task-empty",
        session_id="session-empty",
        request_id="request-empty",
        history_location={"history_index": 4, "session_id": "session-empty"},
        generate_payload={"response": ""},
        llm_debug_data={
            "model": "gemma4:26b",
            "stage_diagnostics": [
                {
                    "stage_id": "plain_response",
                    "stage_label": "Plain response",
                    "latest_status": "llm_call_end",
                    "latest_llm_exchange": {
                        "llm_request_state": "completed",
                        "selected_model": "gemma4:26b",
                        "response_preview": {"char_count": 0, "text": ""},
                    },
                }
            ],
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "selector": {
                        "response": {
                            "char_count": 120,
                            "text": "{'workflow_id':'#V#chat_assistant_workflow'}",
                        },
                        "selection_metadata": {
                            "structured_selection_detected": False,
                            "raw_response_format": "text",
                        },
                        "selection_resolution": "candidate_label_exact_match",
                    },
                    "dispatch": {
                        "dispatch_workflow_id": "#V#chat_assistant_workflow",
                        "selected_execution_mode": "direct_response",
                    },
                },
                "tool_history": [],
            },
        },
        evaluation={
            "verdict": "unhappy",
            "should_user_be_happy": False,
            "reasons": ["No assistant response text was returned."],
            "missing_evidence": [],
            "missing_answer_evidence": [],
        },
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model="gemma4:26b",
        run_environment={"base_url": "http://127.0.0.1:5010"},
    )

    model_report = summary["model_portfolio_evaluation"]
    assert len(model_report["empty_success_suspects"]) == 2
    assert model_report["selector"]["structured_output_valid"] is False
    answer_evidence = model_report["stage_evidence"][1]
    assert answer_evidence["workflow_stage"] == "turn_answer"
    assert answer_evidence["verdict"] == "failed"
    assert answer_evidence["metrics"]["empty_success_suspect_count"] == 2
    assert answer_evidence["promotion_eligible"] is False
    assert "single_prompt_replay_evidence_only" in answer_evidence[
        "promotion_blockers"
    ]


def test_build_summary_records_canonical_concept_id_mismatch_evidence() -> None:
    prompt_entry = {
        "id": "represented_self_facts_vs_inferences",
        "category": "epistemic_summary",
        "complexity_class": "vontology_grounded",
        "prompt": "Tell me about myself as represented here.",
        "knowledge_surfaces": ["kb"],
        "likely_tools": ["fetch_concept", "get_predicate_incidence"],
    }
    generate_payload = {
        "response": (
            "Based on the current Vontology, this is a self-representation "
            "audit for #V#michael_switbrock."
        )
    }
    llm_debug_data = {
        "model": "gemma4:26b",
        "turn_execution_diagnostics": {
            "workflow_routing_diagnostics": {
                "dispatch": {
                    "dispatch_workflow_id": "#V#entity_information_retrieval_workflow",
                    "selected_execution_mode": "custom_workflow",
                }
            },
            "tool_history": [
                {"tool": "fetch_concept", "success": True},
                {"tool": "get_predicate_incidence", "success": True},
            ],
        },
    }
    run_environment = {
        "base_url": "http://127.0.0.1:5010",
        "authenticated_user_concept_id": "#V#michael_witbrock",
    }
    evaluation = sampler._evaluate_user_happiness(
        prompt_entry=prompt_entry,
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
        run_environment=run_environment,
    )

    summary = sampler._build_summary(
        prompt_entry=prompt_entry,
        task_id="task-canonical",
        session_id="session-canonical",
        request_id="request-canonical",
        history_location={"history_index": 2, "session_id": "session-canonical"},
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
        evaluation=evaluation,
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model="gemma4:26b",
        run_environment=run_environment,
    )

    answer_evidence = summary["model_portfolio_evaluation"]["stage_evidence"][1]
    assert answer_evidence["workflow_stage"] == "turn_answer"
    assert answer_evidence["verdict"] == "failed"
    assert (
        answer_evidence["metrics"]["canonical_concept_id_fidelity_status"]
        == "failed"
    )
    assert answer_evidence["metrics"]["canonical_concept_id_mismatch_count"] == 1
    artifact = answer_evidence["evidence_artifact"]
    assert artifact["canonical_concept_id_fidelity"]["findings"][0][
        "reason_code"
    ] == "canonical_concept_id_mismatch"
    assert "#V#michael_switbrock" in (answer_evidence["rationale"] or "")


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
            "requested_provider": None,
        },
        {
            "arm_id": "arm_2",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
            "requested_provider": None,
        },
    ]
    assert summary["comparison"]["arm_count"] == 2
    assert summary["comparison"]["happy_arm_count"] == 2
    assert summary["comparison"]["all_should_user_be_happy"] is True
    assert summary["comparison"]["telemetry_models"] == [
        "gemma4:26b",
        "gpt-5.4-mini",
    ]
    portfolio_report = summary["model_portfolio_report"]
    assert portfolio_report["schema_version"] == "model_portfolio_replay_report.v1"
    assert portfolio_report["arm_count"] == 2
    assert portfolio_report["stage_evidence_count"] == 4
    assert portfolio_report["policy_update"]["authorised"] is False
    assert (
        portfolio_report["aggregate_certification_decision"]["promotion_authorised"]
        is False
    )


def test_build_repeated_replay_summary_reports_success_rate() -> None:
    prompt_entry = {
        "id": "text_relations_for_michael_witbrock_concept",
        "category": "represented_relation_lookup",
        "complexity_class": "vontology_grounded",
        "prompt": "What text relations are used with the concept for Michael Witbrock?",
        "knowledge_surfaces": ["kb"],
        "likely_tools": ["get_text_relations_summary"],
        "requires_tool_use": True,
    }
    attempts = [
        {"status": "ok", "evaluation": {"should_user_be_happy": True}},
        {"status": "ok", "evaluation": {"should_user_be_happy": True}},
        {"status": "error", "evaluation": {"should_user_be_happy": False}},
    ]

    summary = sampler._build_repeated_replay_summary(
        prompt_entry=prompt_entry,
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["vontology_grounded"],
        seed=17,
        requested_model="gemma4:26b",
        requested_model_arms=[
            {"arm_id": "arm_1", "label": "gemma4:26b", "requested_model": "gemma4:26b"}
        ],
        run_environment={
            "base_url": "http://127.0.0.1:5010",
            "model_policy": {"local_only_default": True},
        },
        attempt_summaries=attempts,
        success_count=2,
        minimum_success_rate=0.95,
    )

    assert summary["mode"] == "repeated_replay_suite"
    assert summary["status"] == "failed"
    assert summary["repeat"] == {
        "attempt_count": 3,
        "successful_attempt_count": 2,
        "failed_attempt_count": 1,
        "success_rate": pytest.approx(2 / 3),
        "minimum_success_rate": 0.95,
        "meets_minimum_success_rate": False,
    }
    assert summary["environment"]["model_policy"]["local_only_default"] is True


def test_build_replay_arm_plan_adds_prompt_variant_arms() -> None:
    model_arms = sampler._build_model_arm_plan(
        requested_model="gemma4:26b",
        compare_models=["gpt-5.4-mini"],
        include_active_model_arm=False,
    )

    arms = sampler._build_replay_arm_plan(
        model_arms=model_arms,
        base_prompt_id="#V#mail_answer_prompt",
        prompt_variant_ids=["#V#gemma_mail_answer_prompt_v2"],
        workflow_stage_id="turn_answer",
        target_workflow_id="#V#general_mail_review_workflow",
        replay_set_id="JVNAUTOSCI-2318",
        replay_case_id="mail-listing-failure",
    )

    assert [arm["candidate_prompt_variant_id"] for arm in arms] == [
        None,
        "#V#gemma_mail_answer_prompt_v2",
        None,
        "#V#gemma_mail_answer_prompt_v2",
    ]
    assert {arm["base_prompt_id"] for arm in arms} == {"#V#mail_answer_prompt"}
    assert {arm["workflow_stage_id"] for arm in arms} == {"turn_answer"}
    assert {arm["target_workflow_id"] for arm in arms} == {
        "#V#general_mail_review_workflow"
    }


def test_build_summary_records_prompt_variant_selection_and_scoring_blockers() -> None:
    summary = sampler._build_summary(
        prompt_entry={
            "id": "mail-listing-failure",
            "category": "failure_case_replay",
            "complexity_class": "tool_augmented",
            "prompt": "List my last six email messages.",
            "knowledge_surfaces": ["conversation_history"],
            "likely_tools": ["gmail_list_messages", "gmail_get_message"],
            "requires_tool_use": True,
        },
        task_id="task-mail",
        session_id="session-mail",
        request_id="request-mail",
        history_location={"history_index": 9, "session_id": "session-mail"},
        generate_payload={
            "response": "Here are the six messages.",
            "request_id": "request-mail",
            "session_id": "session-mail",
        },
        llm_debug_data={
            "model": "gemma4:26b",
            "prompt_variant_selection": {
                "base_prompt_concept_id": "#V#mail_answer_prompt",
                "selected_prompt_concept_id": "#V#gemma_mail_answer_prompt_v2",
                "match_reason": "model_family",
                "fallback_reason": None,
            },
            "completion_gate_verdict": {"decision": "partial"},
            "turn_execution_diagnostics": {
                "workflow_routing_diagnostics": {
                    "dispatch": {
                        "dispatch_workflow_id": "#V#general_mail_review_workflow",
                        "selected_execution_mode": "custom_workflow",
                    }
                },
                "tool_history": [
                    {"tool": "gmail_list_messages", "success": True},
                    {"tool": "gmail_get_message", "success": True},
                ],
                "response_surfaces": {
                    "evidence_consistency": {
                        "status": "inconsistent",
                        "scoring_caveats": [
                            "completion_gate_non_success_with_user_visible_response"
                        ],
                        "disagreement_codes": [
                            "critic_pass_with_completion_gate_non_success"
                        ],
                    }
                },
            },
        },
        evaluation={"verdict": "happy", "should_user_be_happy": True},
        prompt_bank_schema_version="live_kb_tool_prompt_bank.v3",
        requested_complexity_classes=["tool_augmented"],
        seed=17,
        requested_model="gemma4:26b",
        run_environment={"base_url": "http://127.0.0.1:5010"},
        arm_metadata={
            "arm_id": "arm_2",
            "label": "gemma:variant",
            "requested_model": "gemma4:26b",
            "base_prompt_id": "#V#mail_answer_prompt",
            "candidate_prompt_variant_id": "#V#gemma_mail_answer_prompt_v2",
        },
    )

    prompt_variant = summary["prompt_variant_evaluation"]
    assert prompt_variant["candidate_prompt_variant_selected"] is True
    assert prompt_variant["match_reason"] == "model_family"
    assert prompt_variant["promotion_blockers"] == []
    scoring = summary["replay_scoring_consistency"]
    assert scoring["non_promotable"] is True
    assert "response_surface_inconsistent" in scoring["promotion_blockers"]
    answer_evidence = summary["model_portfolio_evaluation"]["stage_evidence"][1]
    assert answer_evidence["prompt_id"] == "#V#mail_answer_prompt"
    assert answer_evidence["prompt_variant_id"] == "#V#gemma_mail_answer_prompt_v2"
    assert "response_surface_inconsistent" in answer_evidence["promotion_blockers"]


def test_experiment_observation_captures_prompt_variant_arm() -> None:
    summary = {
        "arm": {
            "arm_id": "arm_2",
            "label": "gemma:variant",
            "requested_model": "gemma4:26b",
            "replay_set_id": "JVNAUTOSCI-2318",
            "replay_case_id": "mail-listing-failure",
        },
        "prompt": {"id": "mail-listing-failure"},
        "conversation": {
            "request_id": "request-mail",
            "history_location": {"session_id": "session-mail", "history_index": 9},
        },
        "telemetry": {
            "model": "gemma4:26b",
            "selected_workflow_id": "#V#general_mail_review_workflow",
            "selected_execution_mode": "custom_workflow",
            "tool_count": 2,
            "tool_history": [{"tool": "gmail_get_message", "success": True}],
        },
        "evaluation": {"should_user_be_happy": True, "reasons": []},
        "response": {"text": "Here are the six messages."},
        "prompt_variant_evaluation": {
            "base_prompt_id": "#V#mail_answer_prompt",
            "candidate_prompt_variant_id": "#V#gemma_mail_answer_prompt_v2",
            "selected_prompt_id": "#V#gemma_mail_answer_prompt_v2",
            "candidate_prompt_variant_selected": True,
            "normal_prompt_variant_resolution_observed": True,
            "promotion_blockers": [],
        },
        "replay_scoring_consistency": {
            "completion_gate_status": "pass",
            "response_surface_status": "consistent",
            "non_promotable": False,
        },
    }

    observation = sampler._build_experiment_observation_from_arm_summary(summary)

    assert observation["verdict"] == "pass"
    assert observation["observed_outcome"]["candidate_prompt_variant_id"] == (
        "#V#gemma_mail_answer_prompt_v2"
    )
    assert observation["candidate_validation"]["valid"] is None
    assert observation["candidate_validation"]["structural_prompt_variant_blockers"] == []
    assert (
        observation["candidate_validation"]["evaluation_authority"]["authoritative"]
        is False
    )
    assert observation["workflow_execution"]["workflow_id"] == (
        "#V#general_mail_review_workflow"
    )
    assert observation["turn_execution_request_ids"] == ["request-mail"]


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
            "server_agent_test_instance": True,
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
            "--allow-premium-model",
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
    assert output["environment"]["base_url"] == "http://127.0.0.1:5010"
    assert output["environment"]["server_agent_test_instance"] is True
    assert output["comparison"]["arm_count"] == 3
    assert output["selection"]["requested_model_arms"] == [
        {
            "arm_id": "arm_1",
            "label": sampler.ACTIVE_AUTHENTICATED_MODEL_LABEL,
            "requested_model": None,
            "requested_provider": None,
        },
        {
            "arm_id": "arm_2",
            "label": "gemma4:26b",
            "requested_model": "gemma4:26b",
            "requested_provider": "ollama",
        },
        {
            "arm_id": "arm_3",
            "label": "gpt-5.4-mini",
            "requested_model": "gpt-5.4-mini",
            "requested_provider": "openai",
        },
    ]


def test_main_records_prompt_variant_arms_to_experiment_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    replay_calls: list[dict[str, object]] = []
    recorded: dict[str, object] = {}

    monkeypatch.setattr(sampler, "_emit_replay_guide_note", lambda: None)
    monkeypatch.setattr(
        sampler,
        "_load_prompt_bank",
        lambda: {"schema_version": "live_kb_tool_prompt_bank.v3", "prompts": []},
    )
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
            "server_agent_test_instance": True,
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
        arm_raw = kwargs["arm_metadata"]
        assert isinstance(arm_raw, dict)
        arm: dict[str, object] = dict(arm_raw)
        return {
            "status": "ok",
            "arm": arm,
            "prompt": {"id": "mail-listing-failure"},
            "conversation": {"request_id": f"req-{len(replay_calls)}"},
            "evaluation": {"should_user_be_happy": True},
            "telemetry": {
                "model": kwargs["requested_model"],
                "selected_workflow_id": "#V#general_mail_review_workflow",
                "selected_execution_mode": "custom_workflow",
            },
            "response": {"text": "Replay response."},
            "prompt_variant_evaluation": {
                "base_prompt_id": arm.get("base_prompt_id"),
                "candidate_prompt_variant_id": arm.get("candidate_prompt_variant_id"),
                "selected_prompt_id": arm.get("candidate_prompt_variant_id")
                or arm.get("base_prompt_id"),
                "candidate_prompt_variant_selected": (
                    arm.get("candidate_prompt_variant_id") is not None
                ),
                "promotion_blockers": [],
            },
            "replay_scoring_consistency": {"non_promotable": False},
            "model_portfolio_evaluation": {
                "stage_evidence": [],
                "empty_success_suspects": [],
            },
        }

    def fake_record_experiment_observations(**kwargs: object) -> dict[str, object]:
        recorded.update(kwargs)
        return {
            "success": True,
            "run_id": kwargs["run_id"],
            "recorded_observation_count": len(kwargs["arm_summaries"]),  # type: ignore[arg-type]
        }

    monkeypatch.setattr(sampler, "_run_prompt_replay_arm", fake_run_prompt_replay_arm)
    monkeypatch.setattr(
        sampler,
        "_record_experiment_observations",
        fake_record_experiment_observations,
    )

    exit_code = sampler.main(
        [
            "--prompt-text",
            "List my last six email messages.",
            "--replay-case-id",
            "mail-listing-failure",
            "--model",
            "gemma4:26b",
            "--base-prompt-id",
            "#V#mail_answer_prompt",
            "--prompt-variant-id",
            "#V#gemma_mail_answer_prompt_v2",
            "--experiment-run-id",
            "#V#experiment_run_mail_prompt_variants",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert [call["requested_model"] for call in replay_calls] == [
        "gemma4:26b",
        "gemma4:26b",
    ]
    assert [
        call["arm_metadata"]["candidate_prompt_variant_id"]  # type: ignore[index]
        for call in replay_calls
    ] == [None, "#V#gemma_mail_answer_prompt_v2"]
    assert recorded["run_id"] == "#V#experiment_run_mail_prompt_variants"
    assert len(recorded["arm_summaries"]) == 2  # type: ignore[arg-type]
    assert output["experiment_recording"]["recorded_observation_count"] == 2


def test_main_can_start_from_failure_conversation_ref_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    replay_calls: list[dict[str, object]] = []
    failure_intake_calls: list[dict[str, object]] = []

    monkeypatch.setattr(sampler, "_emit_replay_guide_note", lambda: None)
    monkeypatch.setattr(
        sampler,
        "_load_prompt_bank",
        lambda: {"schema_version": "live_kb_tool_prompt_bank.v3", "prompts": []},
    )
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
            "server_agent_test_instance": True,
        },
    )
    monkeypatch.setattr(
        sampler,
        "_augment_run_environment_with_active_llm_info",
        lambda **kwargs: kwargs["run_environment"],
    )

    def fake_collect_failure_case_prompt_entry(
        **kwargs: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        failure_intake_calls.append(kwargs)
        return (
            {
                "id": "req-failure",
                "category": "failure_case_replay",
                "complexity_class": "tool_augmented",
                "prompt": "List my last six email messages.",
                "knowledge_surfaces": ["conversation_history"],
                "likely_tools": ["gmail_get_message"],
                "requires_tool_use": True,
                "source_kind": "failure_case_intake",
                "source_request_id": "req-failure",
                "source_workflow_id": "#V#general_mail_review_workflow",
            },
            {"success": True, "request_id": "req-failure"},
        )

    def fake_run_prompt_replay_arm(**kwargs: object) -> dict[str, object]:
        replay_calls.append(kwargs)
        prompt_entry = kwargs["prompt_entry"]
        assert isinstance(prompt_entry, dict)
        return {
            "status": "ok",
            "prompt": {"id": prompt_entry["id"], "text": prompt_entry["prompt"]},
            "conversation": {"request_id": "req-replay"},
            "evaluation": {"should_user_be_happy": True},
            "telemetry": {
                "model": kwargs["requested_model"],
                "selected_workflow_id": "#V#general_mail_review_workflow",
                "selected_execution_mode": "custom_workflow",
            },
            "response": {"text": "Replay response."},
            "prompt_variant_evaluation": {"promotion_blockers": []},
            "replay_scoring_consistency": {"non_promotable": False},
            "model_portfolio_evaluation": {
                "stage_evidence": [],
                "empty_success_suspects": [],
            },
        }

    monkeypatch.setattr(
        sampler,
        "_collect_failure_case_prompt_entry",
        fake_collect_failure_case_prompt_entry,
    )
    monkeypatch.setattr(sampler, "_run_prompt_replay_arm", fake_run_prompt_replay_arm)

    exit_code = sampler.main(
        [
            "--failure-conversation-ref-json",
            '{"kind":"von_conversation_ref","conversation_ref":{"session_id":"session-1"}}',
            "--failure-request-id",
            "req-failure",
            "--model",
            "gemma4:26b",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert failure_intake_calls[0]["request_id"] == "req-failure"
    assert failure_intake_calls[0]["conversation_ref"] == {
        "kind": "von_conversation_ref",
        "conversation_ref": {"session_id": "session-1"},
    }
    first_prompt_entry = replay_calls[0]["prompt_entry"]
    assert isinstance(first_prompt_entry, dict)
    assert first_prompt_entry["prompt"] == "List my last six email messages."
    assert output["failure_case_intake"] == {"success": True, "request_id": "req-failure"}
