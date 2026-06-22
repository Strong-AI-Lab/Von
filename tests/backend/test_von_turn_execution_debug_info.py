from __future__ import annotations

from typing import Any


def test_finalise_llm_debug_info_passes_supervised_structured_outputs_into_turn_record(
    monkeypatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    captured: dict[str, Any] = {}

    def _fake_build_turn_execution_record(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "workflow_routing_diagnostics": {
                "selected_workflow_id": "#V#chat_assistant_workflow"
            },
            "execution": {
                "selected_workflow_trace": kwargs.get("selected_workflow_trace"),
            },
            "critic": {"verdict": kwargs.get("critic_verdict")},
            "completion_gate_verdict": kwargs.get("completion_gate_verdict"),
            "completion_report": kwargs.get("completion_report"),
        }

    monkeypatch.setattr(
        von_routes, "build_turn_execution_record", _fake_build_turn_execution_record
    )
    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )

    llm_debug_info = {
        "request_id": "req-structured-debug",
        "interaction_timestamp_utc": "2026-04-08T00:00:00+00:00",
        "response": "Workflow monitor response",
        "tool_invocations": [],
        "search_evidence": [],
        "turn_execution_diagnostics": {},
        "aux_llm_calls": [],
        "selected_workflow_trace": {
            "workflow_id": "#V#chat_assistant_workflow",
            "execution_mode": "direct_response",
        },
        "critic_verdict": {"verdict": "pass"},
        "completion_gate_verdict": {"decision": "completed"},
        "completion_report": {
            "schema_version": "conversation_turn_selected_workflow_result.v1",
            "workflow_id": "#V#chat_assistant_workflow",
        },
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text="Show the workflow monitor row for this turn.",
        response_text="Workflow monitor response",
        session_id="session-structured-debug",
        namespace="#V#michael_witbrock@sail_lab",
        user_id="#V#michael_witbrock",
        org_id="#V#sail_lab",
        workflow_discovery={"selected_workflow_id": "#V#chat_assistant_workflow"},
        workflow_routing={"workflow_id": "#V#chat_assistant_workflow"},
    )

    assert captured["selected_workflow_trace"] == {
        "workflow_id": "#V#chat_assistant_workflow",
        "execution_mode": "direct_response",
    }
    assert captured["critic_verdict"] == {"verdict": "pass"}
    assert captured["completion_gate_verdict"] == {"decision": "completed"}
    assert captured["completion_report"] == {
        "schema_version": "conversation_turn_selected_workflow_result.v1",
        "workflow_id": "#V#chat_assistant_workflow",
    }
    assert captured["tool_invocations"] == []
    turn_execution_record = result["turn_execution_record"]
    assert (
        turn_execution_record["execution"]["selected_workflow_trace"]
        == captured["selected_workflow_trace"]
    )
    assert turn_execution_record["critic"]["verdict"] == {"verdict": "pass"}
    assert turn_execution_record["completion_gate_verdict"] == {"decision": "completed"}
    assert result["completion_gate_verdict"]["decision"] == "completed"


def test_finalise_llm_debug_info_prefers_turn_record_tool_invocation_evidence(
    monkeypatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    captured: dict[str, Any] = {}

    def _fake_build_turn_execution_record(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "workflow_routing_diagnostics": {},
            "completion_gate": {"decision": "completed"},
        }

    monkeypatch.setattr(
        von_routes, "build_turn_execution_record", _fake_build_turn_execution_record
    )
    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )

    rich_invocation = {
        "tool": "scholarly_paper.verify_representation",
        "status": "ok",
        "effective_payload": {"scholarly_representation_verified": True},
    }

    von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-rich-evidence",
            "interaction_timestamp_utc": "2026-06-07T00:00:00+00:00",
            "response": "Represented.",
            "tool_invocations": [
                {
                    "tool": "scholarly_paper.verify_representation",
                    "status": "ok",
                    "arguments": {},
                }
            ],
            "turn_execution_record_tool_invocations": [rich_invocation],
            "search_evidence": [],
            "turn_execution_diagnostics": {},
            "aux_llm_calls": [],
        },
        prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
        response_text="Represented.",
        session_id="session-rich-evidence",
        namespace="#V#michael_witbrock@sail_lab",
        user_id="#V#michael_witbrock",
        org_id="#V#sail_lab",
        workflow_discovery=None,
        workflow_routing=None,
    )

    assert captured["tool_invocations"] == [rich_invocation]


def test_finalise_llm_debug_info_uses_bounded_evidence_for_arxiv_readback() -> None:
    import src.backend.server.routes.von_routes as von_routes

    workflow_required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "contract_id": "arxiv_paper_representation_readback",
        "required_effects": [
            {
                "effect_id": "arxiv_paper_representation",
                "effect_type": "scholarly_representation",
                "required_tools": ["scholarly_paper.verify_representation"],
                "required_payload_fields": [
                    "paper_concept_id",
                    "file_copy_concept_id",
                ],
                "targets": ["2106.03245"],
                "missing_failure_code": "arxiv_paper_representation_not_executed",
                "wrong_target_failure_code": "arxiv_paper_representation_wrong_target",
            }
        ],
    }
    compact_invocation = {
        "tool": "scholarly_paper.verify_representation",
        "method": "scholarly_paper.verify_representation",
        "status": "ok",
        "arguments": {
            "arxiv_id": "2106.03245",
            "paper_concept_id": "#V#paper_2106_03245",
            "file_copy_concept_id": "#V#file_copy_2106_03245",
        },
    }
    rich_invocation = {
        "tool": "scholarly_paper.verify_representation",
        "method": "scholarly_paper.verify_representation",
        "status": "ok",
        "effective_payload": {
            "arxiv_id": "2106.03245",
            "paper_concept_id": "#V#paper_2106_03245",
            "file_copy_concept_id": "#V#file_copy_2106_03245",
            "scholarly_representation_verified": True,
            "verification_failures": [],
        },
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-arxiv-readback-finalise",
            "interaction_timestamp_utc": "2026-06-07T00:00:00+00:00",
            "response": (
                "The paper has been successfully represented. "
                "Paper concept: #V#paper_2106_03245."
            ),
            "tool_invocations": [compact_invocation],
            "turn_execution_record_tool_invocations": [rich_invocation],
            "search_evidence": [],
            "turn_execution_diagnostics": {},
            "aux_llm_calls": [],
            "selected_workflow_trace": {
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "execution_mode": "custom_workflow",
                "workflow_required_effects_contract": (
                    workflow_required_effects_contract
                ),
                "workflow_required_effects_contract_source": "definition_metadata",
            },
            "completion_report": {
                "schema_version": "conversation_turn_selected_workflow_result.v1",
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "workflow_required_effects_contract": (
                    workflow_required_effects_contract
                ),
                "workflow_required_effects_contract_source": "definition_metadata",
            },
        },
        prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
        response_text=(
            "The paper has been successfully represented. "
            "Paper concept: #V#paper_2106_03245."
        ),
        session_id="session-arxiv-readback-finalise",
        namespace="#V#michael_witbrock@sail_lab",
        user_id="#V#michael_witbrock",
        org_id="#V#sail_lab",
        workflow_discovery=None,
        workflow_routing={
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "verdict": "rag_selected",
            "source": "selector",
        },
    )

    record = result["turn_execution_record"]
    effect_by_id = {
        effect["effect_id"]: effect for effect in record["required_effects"]
    }
    effect = effect_by_id["arxiv_paper_representation"]
    assert effect["status"] == "satisfied"
    assert effect["failure_codes"] == []
    assert "paper_representation_not_verified" not in (
        record["completion_gate"].get("blocking_failure_codes") or []
    )
    assert (
        "arxiv_paper_representation"
        not in record["completion_gate"]["evidence_payload"]["unresolved_effect_ids"]
    )


def test_finalise_llm_debug_info_republishes_canonical_completion_gate(
    monkeypatch,
) -> None:
    import src.backend.server.routes.von_routes as von_routes

    canonical_gate = {
        "decision": "completed",
        "requires_follow_up": False,
        "safe_to_claim_completion": True,
        "blocking_effect_ids": [],
        "blocking_failure_codes": [],
    }
    stale_gate = {
        "decision": "escalation_required",
        "requires_follow_up": True,
        "safe_to_claim_completion": False,
        "blocking_effect_ids": ["effect_required_tool_obligations_1"],
        "blocking_failure_codes": ["required_tool_not_available_on_gateway"],
    }

    def _fake_build_turn_execution_record(**kwargs: Any) -> dict[str, Any]:
        return {
            "workflow_routing_diagnostics": {
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "dispatch": {"required_effects_unresolved_effect_ids": []},
            },
            "completion_gate": dict(canonical_gate),
        }

    monkeypatch.setattr(
        von_routes, "build_turn_execution_record", _fake_build_turn_execution_record
    )
    monkeypatch.setattr(
        von_routes,
        "get_runtime_code_version_info",
        lambda: {"version": "test-version"},
    )

    llm_debug_info = {
        "request_id": "req-canonical-gate",
        "interaction_timestamp_utc": "2026-06-07T00:00:00+00:00",
        "response": "The paper has been represented.",
        "tool_invocations": [],
        "search_evidence": [],
        "turn_execution_diagnostics": {
            "completion_gate": dict(stale_gate),
            "workflow_routing_diagnostics": {
                "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
                "dispatch": {
                    "required_effects_unresolved_effect_ids": [
                        "arxiv_paper_representation_1_2106_03245"
                    ]
                },
            },
        },
        "aux_llm_calls": [],
        "completion_gate_verdict": dict(stale_gate),
    }

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info=llm_debug_info,
        prompt_text="Represent this paper: https://arxiv.org/abs/2106.03245",
        response_text="The paper has been represented.",
        session_id="session-canonical-gate",
        namespace="#V#michael_witbrock@sail_lab",
        user_id="#V#michael_witbrock",
        org_id="#V#sail_lab",
        workflow_discovery={
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow"
        },
        workflow_routing={"workflow_id": "#V#arxiv_paper_representation_workflow"},
    )

    assert result["completion_gate"] == canonical_gate
    assert result["completion_gate_verdict"] == canonical_gate
    assert result["raw_completion_gate_verdict"] == stale_gate
    assert result["turn_execution_diagnostics"]["completion_gate"] == canonical_gate
    assert (
        result["turn_execution_diagnostics"]["workflow_routing_diagnostics"][
            "dispatch"
        ]["required_effects_unresolved_effect_ids"]
        == []
    )


def test_finalise_llm_debug_info_uses_catalogue_for_recovered_required_tool() -> None:
    import src.backend.server.routes.von_routes as von_routes

    result = von_routes._finalise_llm_debug_info(
        llm_debug_info={
            "request_id": "req-recovered-gmail-auth-config",
            "interaction_timestamp_utc": "2026-06-22T03:17:09+00:00",
            "response": (
                "Scopes for profile 'vonwitbrock-gmail': "
                "https://www.googleapis.com/auth/gmail.modify "
                "(source: vontology). Token status: authorised."
            ),
            "tool_invocations": [
                {
                    "tool": "gmail_get_auth_config",
                    "status": "failed",
                    "payload": {"namespace": "#V#user@org"},
                    "error": "Missing required field 'profile_id'",
                },
                {
                    "tool": "gmail_list_profiles",
                    "status": "ok",
                    "payload": {"namespace": "#V#user@org"},
                    "result_preview": {
                        "profiles": [
                            {
                                "profile_id": "vonwitbrock-gmail",
                                "authorised_email": "zhanvonwitbrock@gmail.com",
                            }
                        ]
                    },
                },
                {
                    "tool": "gmail_get_auth_config",
                    "status": "ok",
                    "arguments": {"profile_id": "vonwitbrock-gmail"},
                    "payload": {
                        "success": True,
                        "profile_id": "vonwitbrock-gmail",
                        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
                        "scope_source": "vontology",
                        "token_status": "authorised",
                    },
                },
            ],
            "turn_execution_diagnostics": {},
            "aux_llm_calls": [],
            "selected_workflow_trace": {
                "workflow_id": "#V#tool_calling_workflow",
                "execution_mode": "tool_pipeline",
                "expected_outcome_contract_state": {
                    "required_tools": [
                        "gmail_get_auth_config",
                        "gmail_list_profiles",
                    ]
                },
            },
        },
        prompt_text=(
            "The interface Gmail access check passes. Are you sure? But yet, "
            "try to refresh the token if you have a tool."
        ),
        response_text=(
            "Scopes for profile 'vonwitbrock-gmail': "
            "https://www.googleapis.com/auth/gmail.modify "
            "(source: vontology). Token status: authorised."
        ),
        session_id="session-recovered-gmail-auth-config",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        workflow_routing={"workflow_id": "#V#tool_calling_workflow"},
        method_catalogue={
            "gmail_get_auth_config": {"category": "read"},
            "gmail_list_profiles": {"category": "read"},
        },
    )

    gate = result["completion_gate"]
    assert gate["decision"] == "completed"
    assert gate["safe_to_claim_completion"] is True
    assert gate["requires_follow_up"] is False
    dispatch = result["workflow_routing_diagnostics"]["dispatch"]
    assert dispatch["required_tool_obligation_unsatisfied_count"] == 0
    assert dispatch["required_tool_obligation_blocking_failure_codes"] == []
    obligations = dispatch["required_tool_obligations"]["obligations"]
    auth_obligation = next(
        item for item in obligations if item["tool_name"] == "gmail_get_auth_config"
    )
    assert auth_obligation["operation_metadata_present"] is True
    assert auth_obligation["available_on_gateway"] is True
    assert auth_obligation["successful_count"] == 1
    assert auth_obligation["satisfied"] is True


def test_created_concept_label_extractor_ignores_existing_concept_results() -> None:
    import src.backend.server.routes.von_routes as von_routes

    labels = von_routes._extract_created_concept_labels_from_payload(
        {
            "results": [
                {
                    "success": False,
                    "requested_name": "Tool Calling Workflow",
                    "existing_concept_id": "#V#tool_calling_workflow",
                    "error_code": "already_exists",
                    "duplicate_prevented": True,
                },
                {
                    "success": True,
                    "name": "Scholarly Article",
                    "concept_id": "#V#scholarly_article",
                },
            ],
            "created_concept_ids": [],
        }
    )

    assert labels == []


def test_created_concept_label_extractor_prefers_named_created_ids() -> None:
    import src.backend.server.routes.von_routes as von_routes

    labels = von_routes._extract_created_concept_labels_from_payload(
        {
            "created_concept_ids": ["#V#new_review_workflow"],
            "results": [
                {
                    "success": True,
                    "requested_name": "New Review Workflow",
                    "concept_id": "#V#new_review_workflow",
                }
            ],
        }
    )

    assert labels == ["New Review Workflow (#V#new_review_workflow)"]
