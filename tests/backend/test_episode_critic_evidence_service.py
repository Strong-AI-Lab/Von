from types import SimpleNamespace

from src.backend.services.chat_auxiliary_prompt_service import (
    build_applied_prompt_snapshot,
)


def _sample_llm_debug() -> dict:
    applied_prompt_snapshot = build_applied_prompt_snapshot(
        user_concept_id="#V#user",
        namespace="#V#user@org",
        organisation_concept_id="#V#org",
        turn_id="req-critic-1",
        behaviour_fragments=[
            {"concept_id": "#V#critic_prompt", "content": "Be precise."}
        ],
        narration_fragments=[],
        screen_fragments=[],
    )
    return {
        "request_id": "req-critic-1",
        "model": "gpt-5.4-mini",
        "workflow_discovery": {"match_count": 1},
        "tool_invocations": [
            {
                "tool": "search_concepts",
                "arguments": {"query": "paper"},
                "result": {"results": [{"concept_id": "#V#paper", "name": "Paper"}]},
            }
        ],
        "search_evidence": [
            {
                "tool": "search_concepts",
                "query": "paper",
                "result": {"results": [{"concept_id": "#V#paper", "name": "Paper"}]},
            }
        ],
        "turn_execution_diagnostics": {
            "latest_progress": {"phase": "completed"},
            "workflow_stage_path": {"path": [{"stage_id": "tool_execute"}]},
        },
        "llm_allowed_tools": ["search_concepts"],
        "allowed_write_tools": [],
        "aux_llm_calls": [{"model": "gpt-5.4-mini", "prompt": "x", "response": "y"}],
        "applied_prompt_snapshot": applied_prompt_snapshot,
    }


def _history_context() -> dict:
    llm_debug = _sample_llm_debug()
    target_message = {
        "role": "assistant",
        "content": "Draft analysis",
        "timestamp": "2026-03-29T04:00:00Z",
        "llm_debug_data": llm_debug,
    }
    return {
        "user_id": "#V#user",
        "session_id": "sess-critic-1",
        "namespace": "#V#user@org",
        "org_id": "#V#org",
        "history": [
            {"role": "user", "content": "Inspect the paper representation"},
            target_message,
        ],
        "target_index": 1,
        "target_message": target_message,
        "target_llm_debug": llm_debug,
        "prompt_text": "Inspect the paper representation",
    }


def test_episode_critic_bundle_reconstructs_turn_record_and_emits_receipts(monkeypatch):
    from src.backend.services import episode_critic_evidence_service as svc

    monkeypatch.setattr(svc, "_load_turn_execution_record", lambda **_: None)
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: _history_context())
    monkeypatch.setattr(
        svc,
        "get_latest_workflow_use_episode",
        lambda **_: {"episode_id": "wfep_1", "workflow_id": "#V#tool_calling_workflow"},
    )
    monkeypatch.setattr(
        svc,
        "_resolve_workflow_definition_identity",
        lambda **_: (
            {
                "workflow_id": "#V#tool_calling_workflow",
                "definition_hash": "deadbeef",
                "source": "runtime_registry",
            },
            "workflow_registry",
        ),
    )

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(request_id="req-critic-1")

    assert result["success"] is True
    assert result["ready_for_critic"] is True
    assert result["fail_closed"] is False
    assert (
        result["source_resolution"]["turn_execution_record_source"]
        == "chat_history.reconstructed_turn_execution_record"
    )
    assert result["observed_evidence"]["turn_execution_record"]["request_id"] == "req-critic-1"
    assert result["observed_evidence"]["applied_prompt_snapshot"]["prompts"][0][
        "concept_id"
    ] == "#V#critic_prompt"
    assert result["observed_evidence"]["tool_ledger"]["search_evidence_count"] == 1
    assert result["expected_context"]["allowed_tool_families"] == ["search"]
    assert result["receipts"]["turn_execution_record"]["present"] is True
    assert result["receipts"]["applied_prompt_snapshot"]["present"] is True
    assert result["bundle_receipt"]["sha256"]


def test_episode_critic_bundle_fails_closed_when_core_context_is_missing(monkeypatch):
    from src.backend.services import episode_critic_evidence_service as svc

    monkeypatch.setattr(
        svc,
        "_load_turn_execution_record",
        lambda **_: {
            "request_id": "req-critic-2",
            "session_id": "sess-critic-2",
            "namespace": "#V#user@org",
            "user_id": "#V#user",
            "workflow_selection": {"selected_workflow_id": "#V#tool_calling_workflow"},
            "execution": {"tool_invocations": [], "required_effects_contract": {}},
            "required_effects": [],
            "postcondition_checks": [],
            "completion_gate": {},
            "critic": {},
        },
    )
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: None)
    monkeypatch.setattr(svc, "get_latest_workflow_use_episode", lambda **_: None)
    monkeypatch.setattr(svc, "_resolve_workflow_definition_identity", lambda **_: (None, None))

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(request_id="req-critic-2")

    assert result["success"] is True
    assert result["ready_for_critic"] is False
    assert result["fail_closed"] is True
    assert "selected_llm_debug_missing" in result["fail_closed_reason_codes"]
    assert "neighbouring_context_missing" in result["fail_closed_reason_codes"]
    assert "workflow_definition_identity_missing" in result["fail_closed_reason_codes"]
    diagnostic = result["format_over_content_diagnostic"]
    assert diagnostic["status"] == "insufficient_evidence"
    assert "episode_bundle_fail_closed" in diagnostic["reason_codes"]
    assert "selected_llm_debug_missing" in diagnostic["reason_codes"]


def test_episode_critic_bundle_supports_workflow_terminal_instances_without_turn_record(
    monkeypatch,
):
    from src.backend.services import episode_critic_evidence_service as svc

    instance = SimpleNamespace(
        instance_id="wf-inst-1",
        workflow_id="#V#workflow_introspection_maintenance_workflow",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        status="completed",
        current_state="complete",
        step_index=4,
        retry_count=0,
        inputs={"session_id": "sess-wf-1"},
        outputs={"result": "done"},
        execution_trace_id="trace-1",
    )

    monkeypatch.setattr(svc, "_load_turn_execution_record", lambda **_: None)
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: None)
    monkeypatch.setattr(svc, "get_latest_workflow_use_episode", lambda **_: None)
    monkeypatch.setattr(svc, "get_workflow_execution_trace", lambda _trace_id: None)
    monkeypatch.setattr(
        svc,
        "_resolve_workflow_definition_identity",
        lambda **_: (
            {
                "workflow_id": "#V#workflow_introspection_maintenance_workflow",
                "definition_hash": "wf-hash-1",
                "source": "runtime_registry",
            },
            "workflow_registry",
        ),
    )

    class _StubManager:
        def get_instance(self, _instance_id):
            return instance

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(instance_id="wf-inst-1")

    assert result["success"] is True
    assert result["ready_for_critic"] is True
    assert result["fail_closed"] is False
    assert result["episode_locator"]["subject_kind"] == "workflow_terminal_instance"
    assert result["episode_locator"]["instance_id"] == "wf-inst-1"
    assert result["expected_context"]["completion_criteria"]["terminal_status"] == "completed"
    assert result["expected_context"]["workflow_definition_identity"]["definition_hash"] == (
        "wf-hash-1"
    )
    assert (
        result["observed_evidence"]["workflow_instance"]["status"]["instance_id"]
        == "wf-inst-1"
    )


def test_episode_critic_bundle_surfaces_routing_quality_signals(monkeypatch):
    from src.backend.services import episode_critic_evidence_service as svc

    turn_record = {
        "request_id": "req-critic-routing-1",
        "session_id": "sess-critic-routing-1",
        "namespace": "#V#user@org",
        "user_id": "#V#user",
        "workflow_selection": {
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "selector",
            "selection_rationale": "selector_selected_discovered_candidate",
        },
        "workflow_routing_diagnostics": {
            "selected_workflow_id": "#V#tool_calling_workflow",
            "selector_verdict": "rag_selected",
            "selector_source": "selector",
            "selection_rationale": "selector_selected_discovered_candidate",
            "selector": {
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
            },
            "discovery": {
                "routing_match_ids": [
                    "#V#arxiv_paper_representation_workflow",
                    "#V#arxiv_paper_ingestion_testing_workflow",
                ]
            },
            "dispatch": {
                "selected_execution_mode": "tool_pipeline",
                "dispatch_terminal_status": "follow_up_required",
                "zero_tools_executed": True,
                "failure_codes": ["tool_dispatch_handoff_zero_execution"],
            },
        },
        "execution": {
            "tool_invocations": [],
            "required_effects_contract": {},
        },
        "required_effects": [],
        "postcondition_checks": [],
        "completion_gate": {
            "decision": "escalation_required",
            "requires_follow_up": True,
            "blocking_failure_codes": ["tool_dispatch_handoff_zero_execution"],
        },
        "critic": {
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
        },
    }

    monkeypatch.setattr(svc, "_load_turn_execution_record", lambda **_: turn_record)
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: _history_context())
    monkeypatch.setattr(
        svc,
        "get_latest_workflow_use_episode",
        lambda **_: {
            "episode_id": "wfep-routing-1",
            "workflow_id": "#V#tool_calling_workflow",
        },
    )
    monkeypatch.setattr(
        svc,
        "_resolve_workflow_definition_identity",
        lambda **_: (
            {
                "workflow_id": "#V#tool_calling_workflow",
                "definition_hash": "wf-routing-hash",
                "source": "runtime_registry",
            },
            "workflow_registry",
        ),
    )

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(
        request_id="req-critic-routing-1"
    )

    assert result["success"] is True
    signals = result["expected_context"]["routing_quality_signals"]
    assert signals["selected_workflow_id"] == "#V#tool_calling_workflow"
    assert signals["selector_prompt_id"] == "#V#chat_turn_classifier_prompt"
    assert signals["routing_match_ids"] == [
        "#V#arxiv_paper_representation_workflow",
        "#V#arxiv_paper_ingestion_testing_workflow",
    ]
    assert signals["unselected_routing_match_ids"] == [
        "#V#arxiv_paper_representation_workflow",
        "#V#arxiv_paper_ingestion_testing_workflow",
    ]
    assert signals["dispatch_zero_execution"] is True
    assert (
        "unselected_routing_matches_present" in signals["diagnostic_flags"]
    )
    assert "dispatch_zero_execution" in signals["diagnostic_flags"]
    assert "completion_requires_follow_up" in signals["diagnostic_flags"]


def test_episode_critic_bundle_surfaces_answer_artifacts_and_support_receipts(
    monkeypatch,
):
    from src.backend.services import episode_critic_evidence_service as svc

    turn_record = {
        "request_id": "req-critic-grounded-1",
        "session_id": "sess-critic-grounded-1",
        "namespace": "#V#user@org",
        "user_id": "#V#user",
        "workflow_selection": {
            "selected_workflow_id": "#V#tool_calling_workflow",
        },
        "workflow_routing_diagnostics": {
            "selector": {
                "context_lineage": {
                    "base_context_source": "augmented_context",
                    "stage_added_message_count": 1,
                },
                "context_summary": {"message_count": 3},
            }
        },
        "execution": {
            "tool_invocations": [
                {
                    "tool": "search_concepts",
                    "result_summary": "Found 1 result",
                }
            ],
            "search_evidence": [
                {
                    "tool": "search_concepts",
                    "query": "grounded represented links",
                    "result": {
                        "results": [
                            {
                                "id": "result:1",
                                "text": "Example Record is linked to Test User.",
                            }
                        ]
                    },
                }
            ],
            "summary": {
                "required_evidence_answer_consistency_blocked": True,
                "required_evidence_answer_consistency_source": "critic_verdict",
            },
            "required_effects_contract": {},
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract_state.v1",
                "required_effect_ids": [
                    "effect_prompt_required_evidence_answer_consistency"
                ],
            },
            "workflow_stage_path": {"path": [{"stage_id": "direct_response"}]},
            "selected_workflow_trace": {
                "direct_response_context_lineage": {
                    "base_context_source": "augmented_context",
                    "turn_memory_context": {"status": "available"},
                }
            },
        },
        "required_effects": [
            {
                "effect_id": "effect_prompt_required_evidence_answer_consistency",
                "effect_type": "required_evidence_answer_consistency",
                "status": "not_satisfied",
            }
        ],
        "postcondition_checks": [],
        "completion_gate": {
            "decision": "partial",
            "requires_follow_up": True,
            "blocking_failure_codes": [
                "authoritative_required_evidence_answer_consistency_mismatch"
            ],
            "evidence_payload": {
                "required_evidence_answer_consistency_blocker": {
                    "effect_type": "required_evidence_answer_consistency",
                    "status": "not_satisfied",
                    "failure_code": (
                        "authoritative_required_evidence_answer_consistency_mismatch"
                    ),
                    "decision": "partial",
                    "blocker_source": "critic_verdict",
                }
            },
        },
        "critic": {
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
        },
        "completion_report": {
            "response_text": "I couldn't find any grounded represented links.",
        },
        "final_response": {
            "response_sha256": "resp-hash-1",
            "completion_claim_detected": True,
            "completion_claim_validated": False,
        },
    }

    monkeypatch.setattr(svc, "_load_turn_execution_record", lambda **_: turn_record)
    monkeypatch.setattr(svc, "_resolve_history_context", lambda **_: _history_context())
    monkeypatch.setattr(
        svc,
        "get_latest_workflow_use_episode",
        lambda **_: {
            "episode_id": "wfep-grounded-1",
            "workflow_id": "#V#tool_calling_workflow",
        },
    )
    monkeypatch.setattr(
        svc,
        "_resolve_workflow_definition_identity",
        lambda **_: (
            {
                "workflow_id": "#V#tool_calling_workflow",
                "definition_hash": "wf-grounded-hash",
                "source": "runtime_registry",
            },
            "workflow_registry",
        ),
    )

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(
        request_id="req-critic-grounded-1"
    )

    assert result["success"] is True
    answer_artifacts = result["observed_evidence"]["answer_artifacts"]
    assert answer_artifacts["response_text"] == (
        "I couldn't find any grounded represented links."
    )
    assert answer_artifacts["response_sha256"] == "resp-hash-1"
    assert answer_artifacts["response_text_source"] == (
        "turn_execution_record.completion_report.response_text"
    )

    support = result["observed_evidence"]["answer_support_evidence"]
    assert support["required_evidence_answer_consistency_blocked"] is True
    assert support["required_evidence_answer_consistency_source"] == "critic_verdict"
    assert support["required_evidence_answer_consistency_blocker"]["blocker_source"] == (
        "critic_verdict"
    )
    assert support["required_evidence_answer_consistency_effects"][0]["effect_type"] == (
        "required_evidence_answer_consistency"
    )

    lineage = result["observed_evidence"]["response_context_lineage"]
    assert lineage["selector_context_lineage"]["base_context_source"] == (
        "augmented_context"
    )
    assert lineage["direct_response_context_lineage"]["turn_memory_context"] == {
        "status": "available"
    }
    assert result["receipts"]["answer_artifacts"]["present"] is True
    assert result["receipts"]["answer_support_evidence"]["present"] is True
    assert result["receipts"]["response_context_lineage"]["present"] is True


def test_episode_critic_bundle_surfaces_format_over_content_suspicion(monkeypatch):
    from src.backend.services import episode_critic_evidence_service as svc

    turn_record = {
        "request_id": "req-critic-format-1",
        "session_id": "sess-critic-format-1",
        "namespace": "#V#user@org",
        "user_id": "#V#user",
        "workflow_selection": {
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selector_verdict": "rag_default",
            "selector_source": "selector",
            "selection_rationale": "selector_selected_default_candidate",
        },
        "workflow_routing_diagnostics": {
            "selected_workflow_id": "#V#chat_assistant_workflow",
            "selector_verdict": "rag_default",
            "selector_source": "selector",
            "selection_rationale": "selector_selected_default_candidate",
            "discovery": {
                "routing_match_ids": [
                    "#V#scholarly_paper_representation_workflow",
                    "#V#chat_assistant_workflow",
                ]
            },
            "selector": {
                "prompt_id": "#V#chat_turn_classifier_prompt",
                "requested_prompt_ids": ["#V#chat_turn_classifier_prompt"],
                "model_name": "gpt-5.4-mini",
                "raw_response_format": "json_object",
                "selection_metadata": {
                    "raw_response_format": "json_object",
                    "structured_selection_detected": True,
                },
                "selected_model_candidate": {
                    "provider": "openai",
                    "model": "gpt-5.4-mini",
                },
            },
            "dispatch": {
                "selected_execution_mode": "direct_response",
                "dispatch_terminal_status": "follow_up_required",
                "zero_tools_executed": True,
                "failure_codes": ["tool_dispatch_handoff_zero_execution"],
            },
        },
        "execution": {
            "tool_invocations": [],
            "search_evidence": [],
            "required_effects_contract": {},
        },
        "required_effects": [],
        "postcondition_checks": [],
        "completion_gate": {
            "decision": "follow_up_required",
            "requires_follow_up": True,
            "blocking_failure_codes": ["tool_dispatch_handoff_zero_execution"],
        },
        "critic": {
            "workflow_id": "#V#kb_mutation_postcondition_critic_workflow",
        },
    }

    monkeypatch.setattr(svc, "_load_turn_execution_record", lambda **_: turn_record)
    monkeypatch.setattr(
        svc,
        "_resolve_history_context",
        lambda **_: {
            "user_id": "#V#user",
            "session_id": "sess-critic-format-1",
            "namespace": "#V#user@org",
            "org_id": "#V#org",
            "history": [
                {"role": "user", "content": "What papers of mine do you know about?"},
                {
                    "role": "assistant",
                    "content": "Draft analysis",
                    "timestamp": "2026-03-29T04:00:00Z",
                    "llm_debug_data": {
                        "request_id": "req-critic-format-1",
                        "model": "gpt-5.4-mini",
                        "workflow_discovery": {"match_count": 2},
                        "tool_invocations": [],
                        "search_evidence": [],
                        "turn_execution_diagnostics": {
                            "latest_progress": {"phase": "completed"},
                            "workflow_stage_path": {
                                "path": [{"stage_id": "selector_decision"}]
                            },
                        },
                        "llm_allowed_tools": ["search_concepts"],
                        "allowed_write_tools": [],
                    },
                },
            ],
            "target_index": 1,
            "target_message": {
                "role": "assistant",
                "content": "Draft analysis",
                "timestamp": "2026-03-29T04:00:00Z",
            },
            "target_llm_debug": {
                "request_id": "req-critic-format-1",
                "model": "gpt-5.4-mini",
                "workflow_discovery": {"match_count": 2},
                "tool_invocations": [],
                "search_evidence": [],
                "turn_execution_diagnostics": {
                    "latest_progress": {"phase": "completed"},
                    "workflow_stage_path": {
                        "path": [{"stage_id": "selector_decision"}]
                    },
                },
                "llm_allowed_tools": ["search_concepts"],
                "allowed_write_tools": [],
            },
            "prompt_text": "What papers of mine do you know about?",
        },
    )
    monkeypatch.setattr(
        svc,
        "get_latest_workflow_use_episode",
        lambda **_: {
            "episode_id": "wfep-format-1",
            "workflow_id": "#V#chat_assistant_workflow",
        },
    )
    monkeypatch.setattr(
        svc,
        "_resolve_workflow_definition_identity",
        lambda **_: (
            {
                "workflow_id": "#V#chat_assistant_workflow",
                "definition_hash": "wf-format-hash",
                "source": "runtime_registry",
            },
            "workflow_registry",
        ),
    )

    class _StubManager:
        def get_instance(self, _instance_id):
            return None

        def list_instances(self, **_kwargs):
            return []

    monkeypatch.setattr(svc, "WorkflowInstanceManager", lambda: _StubManager())

    result = svc.build_episode_critic_evidence_bundle(
        request_id="req-critic-format-1"
    )

    assert result["success"] is True
    diagnostic = result["format_over_content_diagnostic"]
    assert diagnostic["status"] == "suspected"
    assert diagnostic["selected_model"] == "gpt-5.4-mini"
    assert diagnostic["selected_provider"] == "openai"
    assert diagnostic["observed_stage_id"] == "selector_decision"
    assert diagnostic["raw_response_format"] == "json_object"
    assert diagnostic["grounded_tool_path_available"] is True
    assert diagnostic["grounded_tool_path_unused"] is True
    assert "structured_output_contract_present" in diagnostic["reason_codes"]
    assert "grounded_tool_path_unused" in diagnostic["reason_codes"]
    assert "completion_requires_follow_up" in diagnostic["reason_codes"]
