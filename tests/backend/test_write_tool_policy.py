"""Tests for write-tool policy decisions."""

from __future__ import annotations


def _request_evidence(
    tool_name: str,
    *,
    request_state: str = "low_confidence",
    confirmation_state: str = "low_confidence",
    denial_state: str = "low_confidence",
    rationale: str | None = None,
) -> dict[str, dict[str, str | None]]:
    return {
        tool_name: {
            "request_state": request_state,
            "confirmation_state": confirmation_state,
            "denial_state": denial_state,
            "rationale": rationale,
        }
    }


def test_classify_write_tool_risk_covers_policy_classes():
    from src.backend.workflows.write_tool_policy import (
        WRITE_RISK_ADDITIVE_LOW_RISK,
        WRITE_RISK_DESTRUCTIVE,
        WRITE_RISK_EXTERNAL_NON_VONTOLOGY,
        WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE,
        classify_write_tool_risk,
    )

    assert classify_write_tool_risk("add_relationship") == WRITE_RISK_ADDITIVE_LOW_RISK
    assert (
        classify_write_tool_risk("update_concept")
        == WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE
    )
    assert classify_write_tool_risk("delete_concept") == WRITE_RISK_DESTRUCTIVE
    assert (
        classify_write_tool_risk("jira_create_issue")
        == WRITE_RISK_EXTERNAL_NON_VONTOLOGY
    )
    assert classify_write_tool_risk("task_create") == WRITE_RISK_ADDITIVE_LOW_RISK
    assert (
        classify_write_tool_risk("task_update_status")
        == WRITE_RISK_MUTATIVE_NON_DESTRUCTIVE
    )
    assert classify_write_tool_risk("task_delete") == WRITE_RISK_DESTRUCTIVE
    assert (
        classify_write_tool_risk("jira_move_issue")
        == WRITE_RISK_EXTERNAL_NON_VONTOLOGY
    )
    assert (
        classify_write_tool_risk("import_url_file_copy")
        == WRITE_RISK_ADDITIVE_LOW_RISK
    )


def test_bare_arxiv_url_allows_additive_download_by_default():
    from src.backend.workflows.write_tool_policy import (
        REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="https://arxiv.org/abs/2510.06248",
        requested_tools=["download_paper"],
        recent_user_prompts=[],
    )

    assert "download_paper" in decision.allowed_tools
    assert decision.reason == REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK
    tool_decision = decision.decision_for_tool("download_paper")
    assert tool_decision is not None
    assert tool_decision.allowed is True
    assert tool_decision.requires_confirmation is False


def test_explicit_task_create_uses_additive_low_risk_policy():
    from src.backend.workflows.write_tool_policy import (
        REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Please create me a diary entry for today.",
        requested_tools=["task_create"],
        recent_user_prompts=[],
    )

    assert "task_create" in decision.allowed_tools
    assert decision.reason == REASON_DEFAULT_ALLOW_ADDITIVE_LOW_RISK
    tool_decision = decision.decision_for_tool("task_create")
    assert tool_decision is not None
    assert tool_decision.allowed is True
    assert tool_decision.requires_confirmation is False


def test_explicit_denial_blocks_additive_write():
    from src.backend.workflows.write_tool_policy import (
        REASON_EXPLICIT_WRITE_DENIAL_DETECTED,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Do not download or store this arXiv paper: https://arxiv.org/abs/2510.06248",
        requested_tools=["download_paper"],
        request_evidence=_request_evidence(
            "download_paper",
            denial_state="explicit_denial",
            rationale="current prompt explicitly forbids downloading or storing the paper",
        ),
        recent_user_prompts=[],
    )

    assert "download_paper" not in decision.allowed_tools
    assert decision.reason == REASON_EXPLICIT_WRITE_DENIAL_DETECTED
    assert decision.user_denial_detected is True


def test_mutative_non_destructive_write_requires_clear_request():
    from src.backend.workflows.write_tool_policy import (
        REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Tell me about this concept.",
        requested_tools=["update_concept"],
        recent_user_prompts=[],
    )

    assert "update_concept" not in decision.allowed_tools
    assert decision.reason == REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED
    tool_decision = decision.decision_for_tool("update_concept")
    assert tool_decision is not None
    assert tool_decision.confidence_state == "low_confidence"
    assert tool_decision.intervention_kind == "require_explicit_request"
    assert "insufficient_request_evidence" in tool_decision.unresolved_risk_factors


def test_explicit_prompt_allows_mutative_non_destructive_write():
    from src.backend.workflows.write_tool_policy import (
        REASON_EXPLICIT_NON_DESTRUCTIVE_MUTATION_REQUEST,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Tell me about this concept.",
        requested_tools=["update_concept"],
        request_evidence=_request_evidence(
            "update_concept",
            request_state="explicit_request",
            rationale="current prompt explicitly requests a recoverable update",
        ),
        recent_user_prompts=[],
    )

    assert "update_concept" in decision.allowed_tools
    assert decision.reason == REASON_EXPLICIT_NON_DESTRUCTIVE_MUTATION_REQUEST


def test_recent_prompt_alone_no_longer_allows_mutative_non_destructive_write():
    from src.backend.workflows.write_tool_policy import (
        REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Yes, do it.",
        requested_tools=["update_concept"],
        recent_user_prompts=["Update the Vontology concept description now."],
    )

    assert "update_concept" not in decision.allowed_tools
    assert decision.reason == REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED


def test_recent_request_evidence_allows_mutative_non_destructive_write():
    from src.backend.workflows.write_tool_policy import (
        REASON_RECENT_NON_DESTRUCTIVE_MUTATION_REQUEST,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Yes, do it.",
        requested_tools=["update_concept"],
        request_evidence=_request_evidence(
            "update_concept",
            request_state="recent_request_context",
            rationale="current prompt is a continuation of the preserved update request",
        ),
        recent_user_prompts=["Update the Vontology concept description now."],
    )

    assert "update_concept" in decision.allowed_tools
    assert decision.reason == REASON_RECENT_NON_DESTRUCTIVE_MUTATION_REQUEST


def test_destructive_write_requires_confirmation():
    from src.backend.workflows.write_tool_policy import (
        REASON_DESTRUCTIVE_CONFIRMATION_REQUIRED,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Delete concept #V#paper_on_arxiv_2510_06248.",
        requested_tools=["delete_concept"],
        recent_user_prompts=[],
    )

    assert "delete_concept" not in decision.allowed_tools
    assert decision.reason == REASON_DESTRUCTIVE_CONFIRMATION_REQUIRED
    tool_decision = decision.decision_for_tool("delete_concept")
    assert tool_decision is not None
    assert tool_decision.requires_confirmation is True


def test_recent_confirmation_allows_destructive_write():
    from src.backend.workflows.write_tool_policy import (
        REASON_RECENT_DESTRUCTIVE_CONFIRMATION,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Yes, do it.",
        requested_tools=["delete_concept"],
        request_evidence=_request_evidence(
            "delete_concept",
            confirmation_state="recent_confirmation_context",
            rationale="current prompt confirms the recent destructive request",
        ),
        recent_user_prompts=["Delete concept #V#paper_on_arxiv_2510_06248."],
    )

    assert "delete_concept" in decision.allowed_tools
    assert decision.reason == REASON_RECENT_DESTRUCTIVE_CONFIRMATION


def test_external_write_requires_explicit_request():
    from src.backend.workflows.write_tool_policy import (
        REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Summarise the Jira issue state.",
        requested_tools=["jira_update_issue"],
        recent_user_prompts=[],
    )

    assert "jira_update_issue" not in decision.allowed_tools
    assert decision.reason == REASON_EXTERNAL_WRITE_REQUIRES_EXPLICIT_REQUEST


def test_global_mutation_authority_caps_external_write():
    from src.backend.workflows.write_tool_policy import (
        MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE,
        REASON_INSUFFICIENT_MUTATION_AUTHORITY,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Update the Jira issue status now.",
        requested_tools=["jira_update_issue"],
        recent_user_prompts=[],
        global_mutation_authority=(
            MUTATION_AUTHORITY_LEVEL_MUTATIVE_VONTOLOGY_NON_DESTRUCTIVE
        ),
    )

    assert "jira_update_issue" not in decision.allowed_tools
    assert decision.reason == REASON_INSUFFICIENT_MUTATION_AUTHORITY
    tool_decision = decision.decision_for_tool("jira_update_issue")
    assert tool_decision is not None
    assert tool_decision.authority_block_source == "global"


def test_workflow_mutation_authority_can_cap_mutations_to_additive_only():
    from src.backend.workflows.write_tool_policy import (
        REASON_INSUFFICIENT_MUTATION_AUTHORITY,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Update the Vontology concept description now.",
        requested_tools=["update_concept"],
        recent_user_prompts=[],
        workflow_mutation_authority={
            "schema_version": "workflow_step_mutation_authority.v1",
            "maximum_level": "additive_vontology",
        },
    )

    assert "update_concept" not in decision.allowed_tools
    assert decision.reason == REASON_INSUFFICIENT_MUTATION_AUTHORITY
    tool_decision = decision.decision_for_tool("update_concept")
    assert tool_decision is not None
    assert tool_decision.authority_block_source == "workflow"


def test_process_sensitive_additive_write_requires_clear_request():
    from src.backend.workflows.write_tool_policy import (
        REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="List the current workflow bindings only.",
        requested_tools=["workflow_bind_event"],
        requested_tool_payloads={
            "workflow_bind_event": {
                "event_type": "task.created",
                "workflow_id": "#V#some_workflow",
            }
        },
        recent_user_prompts=[],
        runtime_profile={
            "profile_concept_id": "#V#minimal_imposition_runtime_profile_write_policy_v1",
            "decision_policy": {
                "process_sensitive_additive_requires_clear_request": True,
            },
            "tool_feature_overrides": {
                "workflow_bind_event": {
                    "blast_radius": "high",
                    "process_sensitive": True,
                    "requires_clear_request": True,
                }
            },
        },
    )

    assert "workflow_bind_event" not in decision.allowed_tools
    assert decision.reason == REASON_MUTATIVE_NON_DESTRUCTIVE_REQUEST_REQUIRED
    tool_decision = decision.decision_for_tool("workflow_bind_event")
    assert tool_decision is not None
    assert tool_decision.scenario_id == "high_fan_out_change"
    assert tool_decision.risk_features is not None
    assert tool_decision.risk_features["blast_radius"] == "high"


def test_build_mutation_guardrail_events_includes_authority_context():
    from src.backend.workflows.write_tool_policy import (
        build_mutation_guardrail_events,
        compute_allowed_write_tools,
    )

    decision = compute_allowed_write_tools(
        prompt="Delete concept #V#paper_on_arxiv_2510_06248.",
        requested_tools=["delete_concept"],
        recent_user_prompts=[],
    )

    events = build_mutation_guardrail_events(
        policy_decision=decision,
        guardrail_surface="execution",
        stage="tool_execute",
        workflow_id="#V#tool_calling_workflow",
        workflow_step_id="respond",
        action_id="write_policy.decide",
        conversation_session_id="sess-1",
        turn_id="turn-1",
    )

    assert len(events) == 1
    event = events[0]
    assert event["guardrail_surface"] == "execution"
    assert event["decision"] == "approval_required"
    assert event["workflow_id"] == "#V#tool_calling_workflow"
    assert event["workflow_step_id"] == "respond"
    assert event["conversation_session_id"] == "sess-1"


def test_workflow_execution_policy_blocks_write_not_allowlisted_in_theory_scope():
    from src.backend.workflows.write_tool_policy import (
        REASON_WORKFLOW_EXECUTION_POLICY_BLOCKED,
        WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
        compute_workflow_execution_write_policy,
    )

    decision = compute_workflow_execution_write_policy(
        requested_tools=["workflow_create_schedule"],
        workflow_mutation_authority={
            "schema_version": "workflow_step_mutation_authority.v1",
            "maximum_level": "external_system_guarded",
        },
        execution_side_effect_policy={
            "schema_version": WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
            "mode": "theory_bounded",
            "testing_theory_id": "#V#theory_capability_test",
        },
    )

    assert "workflow_create_schedule" not in decision.allowed_tools
    assert decision.reason == REASON_WORKFLOW_EXECUTION_POLICY_BLOCKED
    tool_decision = decision.decision_for_tool("workflow_create_schedule")
    assert tool_decision is not None
    assert tool_decision.authority_block_source == "execution_scope"


def test_workflow_execution_policy_allows_allowlisted_write_in_theory_scope():
    from src.backend.workflows.write_tool_policy import (
        REASON_WORKFLOW_EXECUTION_POLICY_ALLOWLIST,
        WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
        compute_workflow_execution_write_policy,
    )

    decision = compute_workflow_execution_write_policy(
        requested_tools=["add_relationship"],
        workflow_mutation_authority={
            "schema_version": "workflow_step_mutation_authority.v1",
            "maximum_level": "additive_vontology",
        },
        execution_side_effect_policy={
            "schema_version": WORKFLOW_EXECUTION_SIDE_EFFECT_POLICY_SCHEMA_VERSION,
            "mode": "theory_bounded",
            "testing_theory_id": "#V#theory_capability_test",
            "allowed_write_tools": ["add_relationship"],
        },
    )

    assert "add_relationship" in decision.allowed_tools
    assert decision.reason == REASON_WORKFLOW_EXECUTION_POLICY_ALLOWLIST

