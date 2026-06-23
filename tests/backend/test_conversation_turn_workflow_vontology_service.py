from __future__ import annotations

import json
from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.conversation_turn_workflow_vontology_service import (
    _ensure_conversation_turn_prompt_support,
    bootstrap_canonical_conversation_turn_workflows,
)
from src.backend.services.synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
)
from src.backend.services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_WORKFLOW_ID,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.services.gmail_tool_evidence_contract_vontology_service import (
    bootstrap_gmail_tool_evidence_contract,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.services.workflow_policy_graph_service import resolve_policy_from_graph
from src.backend.workflows import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.mcp_tool_bridge import (
    workflow_action_result_from_mcp_payload,
)
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)

EXPECTED_OUTCOME_PROMPT_CONCEPT_ID = (
    "#V#prompt_turn_execution_expected_outcome_inference"
)
CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID = "#V#turn_prompt_context_adjudication_prompt"
SELECTOR_PROMPT_CONCEPT_ID = "#V#chat_turn_classifier_prompt"
NARRATION_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_narrate_completion_report"
RECOVERY_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_recovery_decision"
MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID = "#V#missing_tool_call_retry_prompt"
TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID = "#V#tool_call_repair_prompt"
POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_postcondition_critic"
GENERAL_MAIL_REVIEW_WORKFLOW_ID = "#V#general_mail_review_workflow"
GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID = "#V#gmail_message_detail_fetch_workflow"


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    authority_service.clear_workflow_type_resolution_cache()

    from src.backend.db.mongo_client import get_db

    db = get_db()
    if db is not None:
        for collection_name in ("concepts", "text_relations", "text_values"):
            try:
                db.drop_collection(collection_name)
            except Exception:
                pass

    yield
    authority_service.clear_workflow_type_resolution_cache()


def test_conversation_turn_prompt_support_seeds_content_from_repo_asset(
    _reset_mock_db: Any,
) -> None:
    report = _ensure_conversation_turn_prompt_support()

    assert report.get("success") is True
    assert report.get("seeded_prompt_count") == 9

    expected_outcome_rows = get_texts_for_concept(
        EXPECTED_OUTCOME_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    expected_outcome_text = next(
        (
            (row or {}).get("text")
            for row in expected_outcome_rows
            if (row or {}).get("text")
        ),
        "",
    )
    assert isinstance(expected_outcome_text, str)
    assert "expected-success inference policy" in expected_outcome_text
    framing_rows = get_texts_for_concept(
        SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    framing_text = next(
        ((row or {}).get("text") for row in framing_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(framing_text, str)
    assert "synthesiser_context_framing_template.v1" in framing_text
    assert "ownership, authorship, identity, provenance, attribution" in (
        expected_outcome_text
    )
    assert "Do not treat storage presence, cache presence" in expected_outcome_text
    assert "Prefer omission or explicit uncertainty over speculative recall" in (
        expected_outcome_text
    )
    assert "`selector_guidance`" in expected_outcome_text
    assert "`answering_guidance`" in expected_outcome_text
    assert "`required_tools`" in expected_outcome_text
    assert "exact internal tool IDs" in expected_outcome_text
    assert "prefer ontology-native predicate narrowing" in expected_outcome_text
    assert "`get_predicate_incidence` and then `find_relations_with_argument`" in (
        expected_outcome_text
    )
    assert "extent of an implied or explicit predicate" in expected_outcome_text
    assert "Resolved-entity predicate extent example" in expected_outcome_text
    assert (
        '"required_tools":["get_predicate_incidence","find_relations_with_argument"]'
        in expected_outcome_text
    )
    assert "predicate-filtered extent" in expected_outcome_text
    assert "predicates, relation schema, usage, incidence" in expected_outcome_text
    assert "represented class/type" in expected_outcome_text
    assert "`target_type_ids`" in expected_outcome_text
    assert "`get_predicate_incidence` with `instance_of`" in expected_outcome_text
    assert "simple represented artefact" in expected_outcome_text
    assert "`create_concepts`, `upsert_singleton_text_relation`" in (
        expected_outcome_text
    )
    assert "represented labels, categories, tags, role markers" in (
        expected_outcome_text
    )
    assert "#V#represented_artefact_creation_workflow" in expected_outcome_text
    assert "grounded `parent_id`" in expected_outcome_text
    assert "with `#V#thing` as the parent, is not sufficient" in (expected_outcome_text)
    assert "Listing existing external-system labels" in expected_outcome_text
    assert "write-only required tool list is not sufficient" in (expected_outcome_text)
    assert "Do not invent tools such as `diary_create`" in expected_outcome_text
    assert "Use `task_create` only when the user asks for a task" in (
        expected_outcome_text
    )
    assert "external-system side effect" in expected_outcome_text
    assert "gmail_send_message" in expected_outcome_text
    assert "allow_send=true" in expected_outcome_text
    assert '"required_tools":["gmail_send_message"]' in expected_outcome_text
    assert "read-only external-system retrieval" in expected_outcome_text
    assert "#V#general_mail_review_workflow" in expected_outcome_text
    assert "ordinary mailbox review" in expected_outcome_text
    assert "Do not use the mail-review workflow for Gmail auth" in (
        expected_outcome_text
    )
    assert (
        '"required_tools":["gmail_list_profiles","gmail_list_messages","gmail_get_message"]'
        in expected_outcome_text
    )
    assert "read-only Jira retrieval" in expected_outcome_text
    assert "latest, most recent, newest" in expected_outcome_text
    assert "jira_get_issue" in expected_outcome_text
    assert "jira_search" in expected_outcome_text
    assert "Do not use Jira import/reconciliation workflows" in expected_outcome_text
    assert "#V#jira_task_full_reconciliation_workflow" in expected_outcome_text
    assert "task_import_jira_issues" in expected_outcome_text
    assert "Jira direct issue-key lookup example" in expected_outcome_text
    assert '"required_tools":["jira_get_issue"]' in expected_outcome_text
    assert "Preserve the exact issue key" in expected_outcome_text
    assert "Jira recency/list lookup example" in expected_outcome_text
    assert '"required_tools":["jira_search"]' in expected_outcome_text
    assert "recency basis" in expected_outcome_text
    assert "must contain a concrete predicate before `ORDER BY`" in (
        expected_outcome_text
    )
    assert "never use a bare `ORDER BY updated DESC`" in expected_outcome_text
    assert "issuetype = Task ORDER BY created DESC" in expected_outcome_text
    assert "do not let later stages invent example aliases" in expected_outcome_text
    assert "bypass_profile_query_prefix=true" in expected_outcome_text
    assert "when list rows do not already include sender, subject" in (
        expected_outcome_text
    )
    assert "Read-only recent Gmail listing example" in expected_outcome_text
    assert (
        "Gmail auth, OAuth, scope, auth-config, and token-status checks"
        in expected_outcome_text
    )
    assert "read-only authentication diagnostics" in expected_outcome_text
    assert "gmail_get_auth_config" in expected_outcome_text
    assert "confirmation doubt about a Gmail/interface access check" in (
        expected_outcome_text
    )
    assert '"required_tools":["gmail_list_profiles","gmail_get_auth_config"]' in (
        expected_outcome_text
    )
    assert "no manual token-refresh tool exists" in expected_outcome_text
    assert "never invent tools such as `gmail_refresh_token`" in expected_outcome_text
    assert "Gmail auth/token status example" in expected_outcome_text
    assert (
        "Gmail access-check doubt with refresh-if-available example"
        in expected_outcome_text
    )
    assert "Do not answer solely from the absence of gmail_refresh_token" in (
        expected_outcome_text
    )
    assert "deictic or count-based" in expected_outcome_text
    assert "stable target handles" in expected_outcome_text
    assert "Do not replace concrete handles such as `2605.03042`" in (
        expected_outcome_text
    )
    assert "workflow launch, verification, read-back, or recovery" in (
        expected_outcome_text
    )
    assert "turn_context_handoff_decision" in expected_outcome_text
    assert "`no_prior_context`" in expected_outcome_text
    assert "must contain only exact tool IDs" in expected_outcome_text
    assert "Never invent capability-shaped tool names" in expected_outcome_text

    context_adjudication_rows = get_texts_for_concept(
        CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    context_adjudication_text = next(
        (
            (row or {}).get("text")
            for row in context_adjudication_rows
            if (row or {}).get("text")
        ),
        "",
    )
    assert isinstance(context_adjudication_text, str)
    assert "prompt-dependent prior-context adjudication policy" in (
        context_adjudication_text
    )
    assert "turn_context_handoff_messages" in context_adjudication_text
    assert "`no_prior_context`" in context_adjudication_text
    assert "Do not answer the user" in context_adjudication_text

    missing_tool_retry_rows = get_texts_for_concept(
        MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    missing_tool_retry_text = next(
        (
            (row or {}).get("text")
            for row in missing_tool_retry_rows
            if (row or {}).get("text")
        ),
        "",
    )
    assert isinstance(missing_tool_retry_text, str)
    assert "Choose exact tool names from the available tool list" in (
        missing_tool_retry_text
    )
    assert "`create_concepts` for the represented instance" in (missing_tool_retry_text)
    assert "`upsert_singleton_text_relation` for supplied content" in (
        missing_tool_retry_text
    )
    assert "Do NOT emit `create_concepts` without `parent_id`" in (
        missing_tool_retry_text
    )
    assert "Do NOT use `#V#thing` as the parent" in missing_tool_retry_text
    assert "Do NOT invent domain-specific tool names such as `diary_create`" in (
        missing_tool_retry_text
    )
    assert "external-system side effect" in missing_tool_retry_text
    assert "`gmail_send_message`" in missing_tool_retry_text
    assert "Gmail list/read/label tools" in missing_tool_retry_text
    assert "read-only Gmail tools" in missing_tool_retry_text
    assert "`gmail_list_profiles`" in missing_tool_retry_text
    assert "`gmail_list_messages`" in missing_tool_retry_text
    assert "Gmail auth, token, OAuth-scope, or auth-config checks" in (
        missing_tool_retry_text
    )
    assert "Never invent placeholder aliases such as `user_profile_123`" in (
        missing_tool_retry_text
    )
    assert "max_results" in missing_tool_retry_text
    assert "Do not emit unsupported payload fields such as `count` or `fields`" in (
        missing_tool_retry_text
    )
    assert "represented labels, categories, tags, workflow markers" in (
        missing_tool_retry_text
    )
    assert "Do not substitute external label-listing tools" in (missing_tool_retry_text)

    tool_repair_rows = get_texts_for_concept(
        TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    tool_repair_text = next(
        (
            (row or {}).get("text")
            for row in tool_repair_rows
            if (row or {}).get("text")
        ),
        "",
    )
    assert isinstance(tool_repair_text, str)
    assert "Gmail profile-scoped requests" in tool_repair_text
    assert "gmail_get_auth_config" in tool_repair_text
    assert "Never emit placeholders such as `user_profile_123`" in tool_repair_text

    selector_rows = get_texts_for_concept(
        SELECTOR_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    selector_text = next(
        ((row or {}).get("text") for row in selector_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(selector_text, str)
    assert "full turn context as LLM context messages" in selector_text
    assert "adjudicated prior-context handoff" in selector_text
    assert "turn_context_handoff_decision" in selector_text
    assert "`no_prior_context`" in selector_text
    assert "Do not assume the current request is standalone" in selector_text
    assert "Canonical valid output examples" in selector_text
    assert "Invalid outputs. Never do any of these" in selector_text
    assert "optional `workflow_inputs`" in selector_text
    assert "selected-workflow launch parameters" in selector_text
    assert "Treat `required_tools` as exact-symbol evidence only" in selector_text
    assert "do not select a semantically similar workflow" in selector_text
    assert "Treat auth, OAuth, scope, auth-config, credential" in selector_text
    assert "do not select it for Gmail auth/token diagnostics" in selector_text
    assert "authenticated self-relative entity-information question" in selector_text
    assert "#V#entity_information_retrieval_workflow" in selector_text
    assert '"workflow_id":"#V#tool_calling_workflow"' in selector_text
    assert '"tool_name":"vontology_concept_search"' in selector_text

    prompt_rows = get_texts_for_concept(
        NARRATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    prompt_text = next(
        ((row or {}).get("text") for row in prompt_rows if (row or {}).get("text")),
        "",
    )

    assert isinstance(prompt_text, str)
    assert "Selected Workflow User Response" in prompt_text
    assert "Completion Report` is supporting evidence only" in prompt_text
    assert "Turn Expected Outcome Summary" in prompt_text
    assert "Grounding Requirement" in prompt_text
    assert "Do not narrate workflow bookkeeping as the response" in prompt_text

    recovery_rows = get_texts_for_concept(
        RECOVERY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    recovery_text = next(
        ((row or {}).get("text") for row in recovery_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(recovery_text, str)
    assert "recovery-decision policy" in recovery_text
    assert "next best bounded automated step" in recovery_text
    assert "`turn_next_action`" in recovery_text
    assert "`action_type`" in recovery_text
    assert "completion_gate_repeat_eligible" in recovery_text
    assert "completion_gate_loop_stop_reason" in recovery_text
    assert "completion_gate_escalation_signal" in recovery_text
    assert "Turn Expected Outcome Summary" in recovery_text
    assert "unresolved mechanically extractable targets remain" in recovery_text
    assert "Gmail/mail tool blockers" in recovery_text
    assert "gmail_list_profiles" in recovery_text
    assert "do not answer as if Gmail auth or mailbox state was verified" in (
        recovery_text
    )
    assert (
        '`"retry_execution"`, `"execute_tool_batch"`, '
        '`"respond_with_answer"`, or `"respond_with_follow_up"`'
    ) in recovery_text

    critic_rows = get_texts_for_concept(
        POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    critic_text = next(
        ((row or {}).get("text") for row in critic_rows if (row or {}).get("text")),
        "",
    )
    assert isinstance(critic_text, str)
    assert "postcondition critic for one completed Von turn" in critic_text
    assert "required_evidence_answer_consistency_blocker" in critic_text
    assert "effect_prompt_required_evidence_answer_consistency" in critic_text
    assert "Focus on grounded answer consistency" in critic_text
    assert "final-answer synthesis telemetry" in critic_text
    assert "final_answer_synthesis.tool_evidence_projection" in critic_text
    assert "operational status/ledger summary" in critic_text


def test_bootstrap_materialises_conversation_turn_workflow_family_and_prompt_links(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_conversation_turn_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert report.get("success") is True
    assert counts.get("errors") == 0
    assert counts.get("workflows_published") == 9

    model_policy = resolve_policy_from_graph("#V#default_workflow_model_policy")
    assert model_policy is not None
    assert model_policy.get("completeness") == "graph_complete"
    mail_render_stage = model_policy["stages"]["mail_review_response_rendering"]
    assert mail_render_stage["primary"] == "active_llm"
    assert mail_render_stage["fallback"] == ["ollama:granite3.3:2b"]
    context_adjudication_stage = model_policy["stages"]["context_adjudication"]
    assert context_adjudication_stage["primary"] == "active_llm"
    assert context_adjudication_stage["fallback"] == ["ollama:granite3.3:2b"]
    tool_call_stage = model_policy["stages"]["tool_call"]
    assert tool_call_stage["primary"] == "active_llm"
    assert tool_call_stage["fallback"] == ["ollama:granite3.3:2b"]
    tool_recovery_stage = model_policy["stages"]["tool_recovery"]
    assert tool_recovery_stage["primary"] == "active_llm"
    assert tool_recovery_stage["fallback"] == ["ollama:granite3.3:2b"]

    chat_definition = load_workflow_definition_from_vontology(
        CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert chat_definition is not None
    chat_respond_step_id = authority_service._step_concept_id(
        workflow_id=CHAT_ASSISTANT_WORKFLOW_ID,
        state_id="respond",
    )
    chat_respond_action = chat_definition.states[chat_respond_step_id].actions[0]
    assert chat_respond_action.action_id == "tool_calling.respond"

    context_adjudication_definition = load_workflow_definition_from_vontology(
        TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID
    )
    assert context_adjudication_definition is not None
    context_decision_step_id = authority_service._step_concept_id(
        workflow_id=TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
        state_id="context_adjudication_decision",
    )
    context_decision_action = context_adjudication_definition.states[
        context_decision_step_id
    ].actions[0]
    assert context_decision_action.action_id == "llm.action"
    assert (context_decision_action.llm_policy or {}).get("policy_stage") == (
        "context_adjudication"
    )
    context_decision_prompt_contract = context_decision_action.prompt_contract
    assert isinstance(context_decision_prompt_contract, dict)
    assert context_decision_prompt_contract.get("resolved_prompt_concept_id") == (
        CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID
    )
    context_decision_policy = context_decision_action.validation_policy or {}
    assert context_decision_policy.get("output_format") == "json_value"
    context_decision_defaults = context_decision_policy.get("json_field_defaults") or {}
    assert "mode" not in context_decision_defaults
    assert "summary" not in context_decision_defaults
    assert context_decision_defaults == {
        "routing_evidence_scope": "current_request_only",
        "expected_outcome_scope": "current_request_only",
        "answer_scope": "current_request_only",
        "turn_context_handoff_messages": [],
        "lineage": [],
        "omitted_context_reasons": [],
        "risks": [],
        "confidence": 0.5,
    }
    context_decision_required = (
        context_decision_policy.get("required_json_fields") or []
    )
    assert all(
        field in context_decision_required for field in context_decision_defaults
    )

    tool_calling_definition = load_workflow_definition_from_vontology(
        TOOL_CALLING_WORKFLOW_ID
    )
    assert tool_calling_definition is not None
    tool_calling_repair_step_id = authority_service._step_concept_id(
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        state_id="repair",
    )
    tool_calling_repair_action = tool_calling_definition.states[
        tool_calling_repair_step_id
    ].actions[0]
    assert tool_calling_repair_action.action_id == "tool_calling.repair"
    assert tool_calling_repair_action.execution_mode == "deterministic"
    assert tool_calling_repair_action.prompt_contract is None
    tool_calling_synth_prep_step_id = authority_service._step_concept_id(
        workflow_id=TOOL_CALLING_WORKFLOW_ID,
        state_id="synthesiser_context_prep",
    )
    tool_calling_synth_prep_action = tool_calling_definition.states[
        tool_calling_synth_prep_step_id
    ].actions[0]
    assert tool_calling_synth_prep_action.action_id == "synthesiser_context_prep"
    assert tool_calling_synth_prep_action.execution_mode == "deterministic"
    assert tool_calling_synth_prep_action.prompt_contract is not None
    assert (
        tool_calling_synth_prep_action.prompt_contract["resolved_prompt_concept_id"]
        == SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID
    )
    framing_prompt_rows = get_texts_for_concept(
        SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=1,
    )
    assert framing_prompt_rows
    assert "synthesiser_context_framing_template.v1" in str(
        framing_prompt_rows[0].get("text")
    )
    required_effects_contract = tool_calling_definition.metadata.get(
        "required_effects_contract"
    )
    assert required_effects_contract is None
    contract_rows = get_texts_for_concept(
        TOOL_CALLING_WORKFLOW_ID,
        predicate="#V#hasWorkflowRequiredEffectsContractJson",
        limit=5,
    )
    assert not any((row or {}).get("text") for row in contract_rows)

    mail_review_definition = load_workflow_definition_from_vontology(
        GENERAL_MAIL_REVIEW_WORKFLOW_ID
    )
    assert mail_review_definition is not None
    mail_review_capture_user_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="capture_authenticated_mail_user",
    )
    assert mail_review_definition.initial_state == mail_review_capture_user_step_id
    mail_review_capture_user_action = mail_review_definition.states[
        mail_review_capture_user_step_id
    ].actions[0]
    assert mail_review_capture_user_action.action_id == (
        "workflow_control.context_template"
    )
    assert {
        authority_service._step_concept_id(
            workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
            state_id="lookup_default_mail_profile",
        )
    }.issubset(
        {
            transition.to_state
            for transition in mail_review_definition.states[
                mail_review_capture_user_step_id
            ].transitions
        }
    )
    assert "mail_review_authenticated_user_concept_id" in mail_review_definition.states[
        mail_review_capture_user_step_id
    ].metadata.get("writes_context_keys", [])
    mail_review_default_lookup_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="lookup_default_mail_profile",
    )
    mail_review_default_lookup_action = mail_review_definition.states[
        mail_review_default_lookup_step_id
    ].actions[0]
    assert mail_review_default_lookup_action.action_id == "workflow_mcp.invoke_tool"
    assert mail_review_default_lookup_action.execution_mode == "deterministic"
    assert mail_review_default_lookup_action.inputs["tool_name"] == (
        "find_relations_with_argument"
    )
    assert mail_review_default_lookup_action.inputs["tool_arguments"] == {
        "concept_id": {"$context_key": "mail_review_authenticated_user_concept_id"},
        "argument_index": "subject",
        "predicate_filter": ["#V#has_default_mail_profile"],
        "relation_kind": "any",
        "include_concept_preview": True,
        "include_text_snippets": False,
        "limit": 5,
    }
    assert any(
        mapping.get("tool_output_field") == "result"
        and mapping.get("context_key") == "default_mail_profile_relation_lookup_result"
        for mapping in mail_review_definition.states[
            mail_review_default_lookup_step_id
        ].metadata.get("tool_output_context_mappings", [])
    )
    mail_review_apply_default_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="apply_default_mail_profile_from_relations",
    )
    mail_review_apply_default_action = mail_review_definition.states[
        mail_review_apply_default_step_id
    ].actions[0]
    assert mail_review_apply_default_action.action_id == "workflow_control.context_set"
    default_assignments = mail_review_apply_default_action.inputs["assignments"]
    assert {
        (
            assignment.get("key"),
            assignment.get("value_from_context"),
            assignment.get("skip_if_unresolved"),
        )
        for assignment in default_assignments
    } == {
        (
            "gmail_profile",
            "default_mail_profile_relation_lookup_result.hits.0.target_value",
            True,
        ),
        (
            "mail_profile_resource_concept_id",
            "default_mail_profile_relation_lookup_result.hits.0.target_value",
            True,
        ),
    }
    apply_default_branch_targets = {
        transition.to_state
        for transition in mail_review_definition.states[
            mail_review_apply_default_step_id
        ].transitions
    }
    assert {
        authority_service._step_concept_id(
            workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
            state_id="extract_mail_review_request_parameters",
        ),
        authority_service._step_concept_id(
            workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
            state_id="lookup_represented_mail_profiles",
        ),
    }.issubset(apply_default_branch_targets)
    mail_review_lookup_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="lookup_represented_mail_profiles",
    )
    mail_review_lookup_action = mail_review_definition.states[
        mail_review_lookup_step_id
    ].actions[0]
    assert mail_review_lookup_action.action_id == "workflow_mcp.invoke_tool"
    assert mail_review_lookup_action.execution_mode == "deterministic"
    assert mail_review_lookup_action.inputs["tool_name"] == (
        "find_relations_with_argument"
    )
    assert mail_review_lookup_action.inputs["tool_arguments"] == {
        "concept_id": {"$context_key": "mail_review_authenticated_user_concept_id"},
        "argument_index": "subject",
        "predicate_filter": [
            "#V#has_authorised_mail_profile",
            "#V#has_default_mail_profile",
        ],
        "relation_kind": "any",
        "include_concept_preview": True,
        "include_text_snippets": False,
        "limit": 20,
    }
    assert "mail_profile_relation_lookup_result" in mail_review_definition.states[
        mail_review_lookup_step_id
    ].metadata.get("writes_context_keys", [])
    assert any(
        mapping.get("tool_output_field") == "result"
        and mapping.get("context_key") == "mail_profile_relation_lookup_result"
        for mapping in mail_review_definition.states[
            mail_review_lookup_step_id
        ].metadata.get("tool_output_context_mappings", [])
    )
    mail_review_resolver_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="resolve_mail_profile",
    )
    mail_review_resolver_action = mail_review_definition.states[
        mail_review_resolver_step_id
    ].actions[0]
    assert mail_review_resolver_action.action_id == "mail_review.resolve_profile"
    assert mail_review_resolver_action.execution_mode == "llm"
    assert mail_review_resolver_action.llm_policy is not None
    assert mail_review_resolver_action.llm_policy.get("prompt_text")
    assert mail_review_resolver_action.llm_policy.get("required_tools") == []
    assert (
        "#V#has_authorised_mail_profile"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "environment defaults"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "runtime_profile_alias"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "mail_profile_relation_lookup_result"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "mail_profile_resource_concept_id"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "predicate_filter"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "no predicate_concept_id"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "vonwitbrock_gmail is not a valid runtime alias"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert any(
        field.get("context_key") == "mail_profile_relation_lookup_result"
        for field in mail_review_resolver_action.llm_policy.get("context_fields", [])
        if isinstance(field, dict)
    )
    resolver_branch_targets = {
        transition.to_state
        for transition in mail_review_definition.states[
            mail_review_resolver_step_id
        ].transitions
    }
    assert {
        authority_service._step_concept_id(
            workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
            state_id="extract_mail_review_request_parameters",
        ),
        authority_service._step_concept_id(
            workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
            state_id="profile_choice_needed",
        ),
    }.issubset(resolver_branch_targets)
    assert "gmail_profile" in mail_review_definition.states[
        mail_review_resolver_step_id
    ].metadata.get("writes_context_keys", [])
    assert any(
        mapping.get("tool_output_field")
        == "validated_json.mail_profile_resource_concept_id"
        and mapping.get("context_key") == "gmail_profile"
        for mapping in mail_review_definition.states[
            mail_review_resolver_step_id
        ].metadata.get("tool_output_context_mappings", [])
    )
    resolver_validation_policy = mail_review_resolver_action.validation_policy
    assert isinstance(resolver_validation_policy, dict)
    resolver_required_fields = set(
        resolver_validation_policy.get("required_json_fields") or []
    )
    assert {
        "mail_profile_resolution_status",
        "mail_profile_candidates",
        "mail_profile_resolution_reason",
    }.issubset(resolver_required_fields)
    assert "mail_review_profile_id" not in resolver_required_fields
    assert "mail_profile_resource_concept_id" not in resolver_required_fields
    profile_choice_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="profile_choice_needed",
    )
    profile_choice_action = mail_review_definition.states[
        profile_choice_step_id
    ].actions[0]
    assert profile_choice_action.action_id == "workflow_control.context_template"
    mail_review_prompt_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="prepare_mail_review_tool_prompt",
    )
    mail_review_prompt_action = mail_review_definition.states[
        mail_review_prompt_step_id
    ].actions[0]
    assert mail_review_prompt_action.action_id == "workflow_control.context_template"
    prompt_assignments = mail_review_prompt_action.inputs.get("assignments")
    assert isinstance(prompt_assignments, list)
    assert any(
        isinstance(item, dict)
        and item.get("key") == "mail_review_tool_prompt"
        and "gmail_list_messages" in str(item.get("template") or "")
        and "explicit numeric message count" in str(item.get("template") or "")
        and "exact message_id value" in str(item.get("template") or "")
        and "bypass_profile_query_prefix" in str(item.get("template") or "")
        for item in prompt_assignments
    )
    mail_review_extract_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="extract_mail_review_request_parameters",
    )
    mail_review_extract_action = mail_review_definition.states[
        mail_review_extract_step_id
    ].actions[0]
    assert mail_review_extract_action.action_id == (
        "mail_review.extract_request_parameters"
    )
    assert mail_review_extract_action.execution_mode == "llm"
    assert mail_review_extract_action.llm_policy is not None
    assert mail_review_extract_action.llm_policy.get("tool_mode") == "none"
    assert mail_review_extract_action.llm_policy.get("required_tools") == []
    assert "last 3" in mail_review_extract_action.llm_policy["response_contract_text"]
    assert (
        "last four" in mail_review_extract_action.llm_policy["response_contract_text"]
    )
    assert (
        "one through twenty-five"
        in mail_review_extract_action.llm_policy["response_contract_text"]
    )
    extract_required_fields = (mail_review_extract_action.validation_policy or {}).get(
        "required_json_fields"
    )
    assert "mail_query" not in extract_required_fields
    assert set(extract_required_fields) == {
        "requested_message_count",
        "label_ids",
        "output_fields",
        "extraction_reason",
    }
    assert "mail_review_effective_limit" in mail_review_definition.states[
        mail_review_extract_step_id
    ].metadata.get("writes_context_keys", [])
    assert "mail_review_output_fields" in mail_review_definition.states[
        mail_review_extract_step_id
    ].metadata.get("writes_context_keys", [])
    assert any(
        mapping.get("tool_output_field") == "validated_json.requested_message_count"
        and mapping.get("context_key") == "mail_review_effective_limit"
        for mapping in mail_review_definition.states[
            mail_review_extract_step_id
        ].metadata.get("tool_output_context_mappings", [])
    )
    mail_review_list_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="list_recent_mail_messages",
    )
    mail_review_list_action = mail_review_definition.states[
        mail_review_list_step_id
    ].actions[0]
    assert mail_review_list_action.action_id == "gmail_list_messages"
    assert mail_review_list_action.inputs["profile"]["$context_key"] == (
        "gmail_profile"
    )
    assert mail_review_list_action.inputs["max_results"]["$context_key"] == (
        "mail_review_effective_limit"
    )
    assert mail_review_list_action.inputs["bypass_profile_query_prefix"] is True
    assert any(
        mapping.get("tool_output_field") == "result.messages"
        and mapping.get("context_key") == "mail_review_listed_messages"
        for mapping in mail_review_definition.states[
            mail_review_list_step_id
        ].metadata.get("tool_output_context_mappings", [])
    )
    mail_review_record_invocation_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="record_mail_list_invocation",
    )
    record_invocation_action = mail_review_definition.states[
        mail_review_record_invocation_step_id
    ].actions[0]
    record_assignments = record_invocation_action.inputs.get("assignments")
    requirement_assignment = next(
        (
            item
            for item in record_assignments
            if isinstance(item, dict)
            and item.get("key") == "mail_review_item_output_requirement"
        ),
        None,
    )
    assert isinstance(requirement_assignment, dict)
    requirement_value = requirement_assignment.get("value")
    assert isinstance(requirement_value, dict)
    assert requirement_value["schema_version"] == "workflow_item_output_requirement.v1"
    assert requirement_value["required_fields"]["$context_key"] == (
        "mail_review_output_fields"
    )
    assert "gmail_message_detail" in requirement_value["excluded_fields"]
    mail_review_for_each_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="fetch_mail_message_details",
    )
    mail_review_for_each_action = mail_review_definition.states[
        mail_review_for_each_step_id
    ].actions[0]
    assert mail_review_for_each_action.action_id == "workflow_control.for_each"
    assert mail_review_for_each_action.inputs["workflow_id"] == (
        GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID
    )
    assert mail_review_for_each_action.inputs["items_context_key"] == (
        "mail_review_listed_messages"
    )
    assert mail_review_for_each_action.inputs["item_context_key"] == (
        "current_mail_message"
    )
    assert mail_review_for_each_action.inputs["max_items"]["$context_key"] == (
        "mail_review_effective_limit"
    )
    assert mail_review_for_each_action.inputs["success_policy"] == "allow_partial"
    assert (
        mail_review_for_each_action.inputs[
            "include_tool_invocations_in_iteration_results"
        ]
        is False
    )
    detail_mappings = mail_review_definition.states[
        mail_review_for_each_step_id
    ].metadata.get("tool_output_context_mappings", [])
    assert any(
        mapping.get("tool_output_field") == "successful_results"
        and mapping.get("context_key") == "mail_message_detail_results"
        for mapping in detail_mappings
        if isinstance(mapping, dict)
    )
    assert any(
        mapping.get("tool_output_field") == "iteration_results"
        and mapping.get("context_key") == "mail_message_detail_iteration_results"
        for mapping in detail_mappings
        if isinstance(mapping, dict)
    )
    assert any(
        mapping.get("tool_output_field") == "iteration_errors"
        and mapping.get("context_key") == "mail_message_detail_iteration_errors"
        for mapping in detail_mappings
        if isinstance(mapping, dict)
    )
    assert any(
        mapping.get("tool_output_field") == "invocations"
        and mapping.get("context_key") == "invocations"
        for mapping in detail_mappings
        if isinstance(mapping, dict)
    )
    mail_review_render_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="render_mail_review_response",
    )
    mail_review_render_action = mail_review_definition.states[
        mail_review_render_step_id
    ].actions[0]
    assert mail_review_render_action.action_id == "mail_review.render_grounded_response"
    assert mail_review_render_action.llm_policy is not None
    assert mail_review_render_action.llm_policy.get("tool_mode") == "none"
    render_validation_policy = mail_review_render_action.validation_policy or {}
    assert render_validation_policy.get("required_json_fields") == ["response_text"]
    assert "json_field_defaults" not in render_validation_policy
    render_contract = mail_review_render_action.llm_policy["response_contract_text"]
    assert (
        'Return exactly one JSON object of the form {"response_text":"..."}'
        in render_contract
    )
    assert "put the numbered list inside that one string" in render_contract
    assert (
        "Do not include message_id unless the user explicitly asks" in render_contract
    )
    assert "projected to the caller's required fields" in render_contract
    assert "do not claim that details cannot be displayed" in render_contract
    assert "mail_review_item_output_requirement" in {
        field.get("context_key")
        for field in mail_review_render_action.llm_policy.get("context_fields", [])
        if isinstance(field, dict)
    }
    assert "mail_message_detail_results" in {
        field.get("context_key")
        for field in mail_review_render_action.llm_policy.get("context_fields", [])
        if isinstance(field, dict)
    }
    assert "mail_message_detail_iteration_errors" in {
        field.get("context_key")
        for field in mail_review_render_action.llm_policy.get("context_fields", [])
        if isinstance(field, dict)
    }
    mail_review_return_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="return_mail_review_response",
    )
    mail_review_return_action = mail_review_definition.states[
        mail_review_return_step_id
    ].actions[0]
    assert mail_review_return_action.action_id == "workflow_control.context_set"
    return_assignments = mail_review_return_action.inputs.get("assignments")
    assert isinstance(return_assignments, list)
    assert any(
        item.get("key") == "control_signal" and item.get("value") == "return"
        for item in return_assignments
        if isinstance(item, dict)
    )
    assert any(
        item.get("key") == "return_payload"
        and isinstance(item.get("value"), dict)
        and item["value"].get("final_response", {}).get("$context_key")
        == "final_response"
        and item["value"].get("invocations", {}).get("$context_key") == "invocations"
        and item["value"]
        .get("mail_message_detail_iteration_errors", {})
        .get("$context_key")
        == "mail_message_detail_iteration_errors"
        for item in return_assignments
        if isinstance(item, dict)
    )
    assert not any(
        "delegate_to_tool_pipeline" in state_id
        for state_id in mail_review_definition.states
    )
    detail_fetch_definition = load_workflow_definition_from_vontology(
        GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID
    )
    assert detail_fetch_definition is not None
    detail_extract_step_id = authority_service._step_concept_id(
        workflow_id=GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
        state_id="extract_current_message_id",
    )
    detail_extract_action = detail_fetch_definition.states[
        detail_extract_step_id
    ].actions[0]
    assert detail_extract_action.action_id == "workflow_control.context_set"
    assert detail_extract_action.inputs["assignments"][0][
        "value_from_context_options"
    ] == ["current_mail_message.message_id", "current_mail_message.id"]
    detail_fetch_step_id = authority_service._step_concept_id(
        workflow_id=GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
        state_id="fetch_message_detail",
    )
    detail_project_step_id = authority_service._step_concept_id(
        workflow_id=GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
        state_id="project_message_detail_output",
    )
    detail_fetch_action = detail_fetch_definition.states[detail_fetch_step_id].actions[
        0
    ]
    assert detail_fetch_action.action_id == "gmail_get_message"
    assert detail_fetch_action.inputs["profile"]["$context_key"] == "gmail_profile"
    assert detail_fetch_action.inputs["message_id"]["$context_key"] == (
        "current_message_id"
    )
    detail_project_action = detail_fetch_definition.states[
        detail_project_step_id
    ].actions[0]
    assert detail_project_action.action_id == "workflow_control.context_project"
    assert detail_project_action.inputs["requirement"]["$context_key"] == (
        "mail_review_item_output_requirement"
    )
    assert detail_project_action.inputs["field_sources"]["subject"]["$context_key"] == (
        "message_detail_subject"
    )
    assert "gmail_message_detail" in detail_project_action.inputs["excluded_fields"]
    detail_return_step_id = authority_service._step_concept_id(
        workflow_id=GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
        state_id="return_message_detail",
    )
    assert any(
        transition.to_state == detail_project_step_id
        for transition in detail_fetch_definition.states[
            detail_fetch_step_id
        ].transitions
    )
    assert any(
        transition.to_state == detail_return_step_id
        for transition in detail_fetch_definition.states[
            detail_project_step_id
        ].transitions
    )
    detail_fetch_mappings = detail_fetch_definition.states[
        detail_fetch_step_id
    ].metadata.get("tool_output_context_mappings", [])
    assert {
        ("result", "gmail_message_detail"),
        ("result.message_id", "message_detail_message_id"),
        ("result.sender", "message_detail_sender"),
        ("result.sender", "message_detail_from"),
        ("result.subject", "message_detail_subject"),
        ("result.date", "message_detail_date"),
        ("result.snippet", "message_detail_snippet"),
    }.issubset(
        {
            (mapping.get("tool_output_field"), mapping.get("context_key"))
            for mapping in detail_fetch_mappings
            if isinstance(mapping, dict)
        }
    )
    assert {
        "gmail_message_detail",
        "message_detail_message_id",
        "message_detail_sender",
        "message_detail_from",
        "message_detail_subject",
        "message_detail_date",
        "message_detail_snippet",
    }.issubset(
        set(
            detail_fetch_definition.states[detail_fetch_step_id].metadata.get(
                "writes_context_keys", []
            )
        )
    )
    detail_return_action = detail_fetch_definition.states[
        detail_return_step_id
    ].actions[0]
    assert detail_return_action.action_id == "workflow_control.context_set"
    detail_return_assignments = detail_return_action.inputs.get("assignments")
    assert isinstance(detail_return_assignments, list)
    assert any(
        item.get("key") == "control_signal" and item.get("value") == "return"
        for item in detail_return_assignments
        if isinstance(item, dict)
    )
    detail_return_payload = next(
        (
            item.get("value")
            for item in detail_return_assignments
            if isinstance(item, dict) and item.get("key") == "return_payload"
        ),
        {},
    )
    assert isinstance(detail_return_payload, dict)
    assert detail_return_payload.get("$context_key") == (
        "message_detail_projected_payload"
    )
    launch_contract = mail_review_definition.metadata.get("launch_input_contract")
    assert isinstance(launch_contract, dict)
    assert launch_contract.get("required_inputs") == ["prompt"]
    mail_mapping_targets = {
        mapping.get("target_context_key")
        for mapping in launch_contract.get("input_mappings", [])
        if isinstance(mapping, dict)
    }
    assert {
        "prompt",
        "mail_review_profile_id",
        "mail_review_limit",
        "mail_review_query",
        "mail_review_label_filter",
        "mail_review_output_shape",
        "mail_review_output_fields",
    }.issubset(mail_mapping_targets)
    mail_routing_profile = mail_review_definition.metadata.get("routing_profile")
    assert isinstance(mail_routing_profile, dict)
    assert mail_routing_profile.get("ordinary_mail_review") is True
    assert mail_routing_profile.get("excludes_auth_or_token_refresh") is True
    mail_exemplar_rows = get_texts_for_concept(
        GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        predicate="#V#hasWorkflowDiscoveryExemplarsJson",
        limit=5,
    )
    raw_mail_exemplar_text: object = next(
        (
            (row or {}).get("text")
            for row in mail_exemplar_rows
            if (row or {}).get("text")
        ),
        "",
    )
    mail_exemplar_text = (
        raw_mail_exemplar_text if isinstance(raw_mail_exemplar_text, str) else ""
    )
    assert "gmail review" in mail_exemplar_text
    assert "ordinary mailbox listing" in mail_exemplar_text
    assert "Do not choose this for Gmail authentication" in mail_exemplar_text
    assert "token-status" in mail_exemplar_text
    assert "Do not choose arXiv" in mail_exemplar_text

    assert "mail_profile_id" in mail_exemplar_text

    turn_definition = load_workflow_definition_from_vontology(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert turn_definition is not None
    prelude_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="workflow_experience_context_prelude",
    )
    prelude_action = turn_definition.states[prelude_step_id].actions[0]
    assert prelude_action.action_id == "workflow_invoke_subworkflow"
    assert (
        prelude_action.subworkflow_id == WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID
    )
    assert any(
        transition.to_state
        == authority_service._step_concept_id(
            workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
            state_id="context_adjudication",
        )
        for transition in turn_definition.states[prelude_step_id].transitions
    )
    context_adjudication_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="context_adjudication",
    )
    context_adjudication = turn_definition.states[context_adjudication_step_id]
    context_adjudication_action = context_adjudication.actions[0]
    assert context_adjudication_action.action_id == "workflow_invoke_subworkflow"
    assert context_adjudication_action.subworkflow_id == (
        TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID
    )
    assert context_adjudication_action.inputs.get("inherit_parent_context") is True
    context_adjudication_mappings = (
        context_adjudication.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        mapping.get("context_key") == "turn_context_handoff_decision"
        and mapping.get("tool_output_field") == "result.turn_context_handoff_decision"
        for mapping in context_adjudication_mappings
        if isinstance(mapping, dict)
    )
    prelude_definition = load_workflow_definition_from_vontology(
        WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID
    )
    assert prelude_definition is not None
    read_steps = {
        "read_success_guidance": (
            "#V#hasWorkflowSuccessfulRunGuidanceText",
            "workflow_success_guidance_history",
        ),
        "read_failure_guidance": (
            "#V#hasWorkflowFailureAvoidanceGuidanceText",
            "workflow_failure_avoidance_history",
        ),
        "read_exploration_guidance": (
            "#V#hasWorkflowNextRunExplorationGuidanceText",
            "workflow_low_imposition_exploration_history",
        ),
    }
    for state_id, (predicate_id, context_key) in read_steps.items():
        step_id = authority_service._step_concept_id(
            workflow_id=WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
            state_id=state_id,
        )
        action = prelude_definition.states[step_id].actions[0]
        assert action.action_id == "get_text_relations"
        assert action.inputs["predicate"] == predicate_id
        assert action.inputs["limit"] == 5
        assert action.inputs["sort_recent_first"] is True
        mappings = (
            prelude_definition.states[step_id].metadata.get(
                "tool_output_context_mappings"
            )
            or []
        )
        assert any(
            mapping.get("tool_output_field") == "result.relations"
            and mapping.get("context_key") == context_key
            for mapping in mappings
            if isinstance(mapping, dict)
        )
    expected_outcome_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="expected_outcome_inference",
    )
    expected_outcome_action = turn_definition.states[expected_outcome_step_id].actions[
        0
    ]
    assert expected_outcome_action.action_id == "llm.action"
    expected_outcome_policy = expected_outcome_action.validation_policy or {}
    assert expected_outcome_policy.get("output_format") == "json_value"
    assert "expected_outcome_summary" in (
        expected_outcome_policy.get("json_field_defaults") or {}
    )
    assert "expected_outcome_summary" in (
        expected_outcome_policy.get("required_json_fields") or []
    )
    assert "required_tools" in (
        expected_outcome_policy.get("json_field_defaults") or {}
    )
    assert "required_tools" in (
        expected_outcome_policy.get("required_json_fields") or []
    )
    assert "target_concept_ids" in (
        expected_outcome_policy.get("json_field_defaults") or {}
    )
    assert "target_type_ids" in (
        expected_outcome_policy.get("json_field_defaults") or {}
    )
    assert "target_concept_ids" in (
        expected_outcome_policy.get("required_json_fields") or []
    )
    assert "target_type_ids" in (
        expected_outcome_policy.get("required_json_fields") or []
    )
    expected_outcome_prompt_contract = expected_outcome_action.prompt_contract
    assert isinstance(expected_outcome_prompt_contract, dict)
    assert expected_outcome_prompt_contract.get("resolved_prompt_concept_id") == (
        EXPECTED_OUTCOME_PROMPT_CONCEPT_ID
    )
    expected_outcome_context_fields = (expected_outcome_action.llm_policy or {}).get(
        "context_fields"
    )
    assert isinstance(expected_outcome_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_context_handoff_decision"
        for field in expected_outcome_context_fields
    )
    expected_outcome_metadata = turn_definition.states[
        expected_outcome_step_id
    ].metadata
    expected_outcome_mappings = (
        expected_outcome_metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_expected_required_tools"
        and mapping.get("tool_output_field") == "validated_json.required_tools"
        for mapping in expected_outcome_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_expected_target_concept_ids"
        and mapping.get("tool_output_field") == "validated_json.target_concept_ids"
        for mapping in expected_outcome_mappings
    )
    assert any(
        isinstance(mapping, dict)
        and mapping.get("context_key") == "turn_expected_target_type_ids"
        and mapping.get("tool_output_field") == "validated_json.target_type_ids"
        for mapping in expected_outcome_mappings
    )
    assert "turn_expected_required_tools" in (
        expected_outcome_metadata.get("writes_context_keys") or []
    )
    assert "turn_expected_target_concept_ids" in (
        expected_outcome_metadata.get("writes_context_keys") or []
    )
    assert "turn_expected_target_type_ids" in (
        expected_outcome_metadata.get("writes_context_keys") or []
    )

    selector_preparation_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="selector_preparation",
    )
    selector_preparation = turn_definition.states[selector_preparation_step_id]
    selector_preparation_action = selector_preparation.actions[0]
    assert (
        selector_preparation_action.action_id
        == "turn_execution.prepare_selector_context"
    )

    selector_decision_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="selector_decision",
    )
    selector_decision = turn_definition.states[selector_decision_step_id]
    selector_decision_action = selector_decision.actions[0]
    assert selector_decision_action.action_id == "llm.action"
    selector_decision_policy = selector_decision_action.llm_policy or {}
    assert selector_decision_policy.get("prompt_text_context_key") == (
        "selector_call_prompt_text"
    )
    assert selector_decision_policy.get("context_messages_context_key") == (
        "selector_context_messages"
    )
    assert selector_decision_policy.get("context_lineage_context_key") == (
        "selector_context_lineage"
    )
    selector_context_fields = selector_decision_policy.get("context_fields")
    assert isinstance(selector_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_context_handoff_decision"
        for field in selector_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_context_handoff_routing_evidence_scope"
        for field in selector_context_fields
    )
    routing_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="routing",
    )
    selector_preparation_targets = {
        transition.to_state for transition in selector_preparation.transitions
    }
    assert selector_decision_step_id in selector_preparation_targets
    assert routing_step_id in selector_preparation_targets

    critic_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="critic",
    )
    critic_action = turn_definition.states[critic_step_id].actions[0]
    assert critic_action.action_id == "workflow_invoke_subworkflow"
    assert critic_action.subworkflow_id == KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID

    kb_critic_definition = load_workflow_definition_from_vontology(
        KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID
    )
    assert kb_critic_definition is not None
    evaluate_authoritative_prompt_step_id = authority_service._step_concept_id(
        workflow_id=KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
        state_id="evaluate_authoritative_prompt",
    )
    evaluate_authoritative_prompt = kb_critic_definition.states[
        evaluate_authoritative_prompt_step_id
    ]
    evaluate_authoritative_prompt_action = evaluate_authoritative_prompt.actions[0]
    assert evaluate_authoritative_prompt_action.action_id == "llm.action"
    assert evaluate_authoritative_prompt_action.validation_policy == {
        "output_format": "json_value"
    }
    evaluate_authoritative_prompt_contract = (
        evaluate_authoritative_prompt_action.prompt_contract
    )
    assert isinstance(evaluate_authoritative_prompt_contract, dict)
    assert evaluate_authoritative_prompt_contract.get("resolved_prompt_concept_id") == (
        POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID
    )

    completion_gate_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="completion_gate",
    )
    completion_gate = turn_definition.states[completion_gate_step_id]
    completion_gate_action = completion_gate.actions[0]
    assert completion_gate_action.action_id == "turn_execution.completion_gate"

    recovery_decision_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="recovery_decision",
    )
    recovery_decision = turn_definition.states[recovery_decision_step_id]
    recovery_action = recovery_decision.actions[0]
    assert recovery_action.action_id == "llm.action"
    assert recovery_action.validation_policy == {"output_format": "json_value"}
    recovery_prompt_contract = recovery_action.prompt_contract
    assert isinstance(recovery_prompt_contract, dict)
    assert recovery_prompt_contract.get("resolved_prompt_concept_id") == (
        RECOVERY_PROMPT_CONCEPT_ID
    )
    recovery_context_fields = (recovery_action.llm_policy or {}).get("context_fields")
    assert isinstance(recovery_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_expected_outcome_summary"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "workflow_discovery_result"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "completion_gate_repeat_eligible"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict) and field.get("context_key") == "required_effects"
        for field in recovery_context_fields
    )
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "turn_recovery_last_reasoning"
        for field in recovery_context_fields
    )

    completed_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="completed",
    )
    execution_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="execution",
    )
    recovery_follow_up_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="apply_recovery_follow_up",
    )
    recovery_tool_batch_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="apply_recovery_tool_batch",
    )
    recovery_answer_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="apply_recovery_answer",
    )
    transition_targets = {
        transition.to_state for transition in completion_gate.transitions
    }
    selector_targets = {
        transition.to_state
        for transition in turn_definition.states[expected_outcome_step_id].transitions
    }
    assert selector_preparation_step_id in selector_targets
    assert execution_step_id not in transition_targets
    assert recovery_decision_step_id in transition_targets
    assert completed_step_id in transition_targets
    recovery_transition_targets = {
        transition.to_state for transition in recovery_decision.transitions
    }
    assert recovery_tool_batch_step_id in recovery_transition_targets
    assert recovery_answer_step_id in recovery_transition_targets
    assert recovery_follow_up_step_id in recovery_transition_targets
    recovery_tool_batch = turn_definition.states[recovery_tool_batch_step_id]
    recovery_tool_batch_action = recovery_tool_batch.actions[0]
    assert recovery_tool_batch_action.action_id == "turn_execution.execute_tool_batch"
    recovery_tool_batch_targets = {
        transition.to_state for transition in recovery_tool_batch.transitions
    }
    assert critic_step_id in recovery_tool_batch_targets
    recovery_mappings = (
        recovery_decision.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        mapping.get("context_key") == "turn_next_action"
        and mapping.get("tool_output_field") == "validated_json.turn_next_action"
        for mapping in recovery_mappings
        if isinstance(mapping, dict)
    )
    assert any(
        mapping.get("context_key") == "turn_next_action_type"
        and mapping.get("tool_output_field")
        == "validated_json.turn_next_action.action_type"
        for mapping in recovery_mappings
        if isinstance(mapping, dict)
    )
    assert any(
        mapping.get("context_key") == "turn_next_action_tool_calls"
        and mapping.get("tool_output_field")
        == "validated_json.turn_next_action.tool_calls"
        for mapping in recovery_mappings
        if isinstance(mapping, dict)
    )

    narration_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="narration",
    )
    narration_state = turn_definition.states[narration_step_id]
    narration_action = narration_state.actions[0]
    prompt_contract = narration_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert (
        prompt_contract.get("resolved_prompt_concept_id") == NARRATION_PROMPT_CONCEPT_ID
    )
    narration_context_fields = (narration_action.llm_policy or {}).get("context_fields")
    assert isinstance(narration_context_fields, list)
    assert any(
        isinstance(field, dict)
        and field.get("context_key") == "selected_workflow_user_response"
        for field in narration_context_fields
    )
    narration_mappings = (
        narration_state.metadata.get("tool_output_context_mappings") or []
    )
    assert any(
        mapping.get("tool_output_field") == "final_response"
        and mapping.get("context_key") == "response_text"
        for mapping in narration_mappings
        if isinstance(mapping, dict)
    )


def test_gmail_message_detail_fetch_workflow_returns_compact_declared_payload(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_conversation_turn_workflows()
    detail_fetch_definition = load_workflow_definition_from_vontology(
        GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID
    )
    assert detail_fetch_definition is not None

    registry = ActionRegistry()
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: load_workflow_definition_from_vontology(
            workflow_id
        ),
    )

    def fake_gmail_get_message(request: Any) -> WorkflowActionResult:
        assert request.inputs["profile"] == "default-profile"
        assert request.inputs["message_id"] == "msg-1"
        return WorkflowActionResult(
            status="success",
            outputs={
                "result": {
                    "message_id": "msg-1",
                    "sender": "Sender <sender@example.test>",
                    "from": "Sender <sender@example.test>",
                    "subject": "Subject line",
                    "date": "Sat, 25 Apr 2026 09:00:00 +0000",
                    "snippet": "Short preview",
                }
            },
        )

    registry.register(
        ActionSpec(action_id="gmail_get_message", handler=fake_gmail_get_message)
    )
    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        detail_fetch_definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "gmail_profile": "default-profile",
            "current_mail_message": {"message_id": "msg-1"},
            "mail_review_item_output_requirement": {
                "schema_version": "workflow_item_output_requirement.v1",
                "purpose": "mail_list_rendering",
                "required_fields": ["message_id", "subject", "sender"],
                "excluded_fields": ["gmail_message_detail", "payload"],
            },
        },
    )

    assert result.completed is True
    assert result.final_state.endswith("_return_message_detail")
    assert result.result_envelope is not None
    payload = result.result_envelope.get("declared_output_payload")
    assert payload == {
        "message_id": "msg-1",
        "subject": "Subject line",
        "sender": "Sender <sender@example.test>",
    }


def test_gmail_message_detail_fetch_workflow_uses_projected_mcp_payload(
    _reset_mock_db: Any,
) -> None:
    bootstrap_gmail_tool_evidence_contract()
    bootstrap_canonical_conversation_turn_workflows()
    detail_fetch_definition = load_workflow_definition_from_vontology(
        GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID
    )
    assert detail_fetch_definition is not None

    registry = ActionRegistry()
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: load_workflow_definition_from_vontology(
            workflow_id
        ),
    )

    large_marker = "RAW_BODY_SHOULD_NOT_REACH_WORKFLOW_CONTEXT"

    def fake_mcp_fallback(request: Any) -> WorkflowActionResult:
        assert request.action_id == "gmail_get_message"
        assert request.inputs["profile"] == "default-profile"
        assert request.inputs["message_id"] == "msg-1"
        return workflow_action_result_from_mcp_payload(
            tool_name=request.action_id,
            payload={
                "message_id": "msg-1",
                "threadId": "thread-1",
                "labelIds": ["INBOX"],
                "sender": "Sender <sender@example.test>",
                "from": "Sender <sender@example.test>",
                "subject": "Subject line",
                "date": "Sat, 25 Apr 2026 09:00:00 +0000",
                "snippet": "Short preview",
                "payload": {
                    "headers": [{"name": "Subject", "value": "Subject line"}],
                    "parts": [{"body": large_marker * 64}],
                },
            },
            duration_ms=1.0,
        )

    registry.set_fallback_handler(fake_mcp_fallback)
    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        detail_fetch_definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "gmail_profile": "default-profile",
            "current_mail_message": {"message_id": "msg-1"},
            "mail_review_item_output_requirement": {
                "schema_version": "workflow_item_output_requirement.v1",
                "purpose": "mail_list_rendering",
                "required_fields": ["message_id", "subject", "sender"],
                "excluded_fields": ["gmail_message_detail", "payload"],
            },
        },
    )

    assert result.completed is True
    assert result.result_envelope is not None
    payload = result.result_envelope.get("declared_output_payload")
    assert isinstance(payload, dict)
    assert payload == {
        "message_id": "msg-1",
        "subject": "Subject line",
        "sender": "Sender <sender@example.test>",
    }
    assert "gmail_message_detail" not in payload
    assert "payload" not in payload

    detail_step = next(
        envelope
        for envelope in result.data.get("workflow_step_result_envelopes", [])
        if isinstance(envelope, dict)
        and envelope.get("action_id") == "gmail_get_message"
    )
    detail_outputs = detail_step["output_payload"]
    assert detail_outputs["mcp_result_projection_applied"] is True
    assert detail_outputs["mcp_raw_result_omitted_from_workflow_context"] is True
    assert detail_outputs["result"]["_llm_view"] == "tool_evidence_projection.v1"
    assert "payload" not in detail_outputs["result"]
    assert "gmail_message_detail" not in json.dumps(
        payload, sort_keys=True, default=str
    )
    assert large_marker not in json.dumps(result.data, sort_keys=True, default=str)


def test_mail_review_detail_loop_collects_compact_child_results(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_conversation_turn_workflows()

    registry = ActionRegistry()
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: load_workflow_definition_from_vontology(
            workflow_id
        ),
    )

    large_marker = "RAW_DETAIL_BODY_SHOULD_NOT_REACH_PARENT_LOOP"

    def fake_mcp_fallback(request: Any) -> WorkflowActionResult:
        assert request.action_id == "gmail_get_message"
        message_id = request.inputs["message_id"]
        return workflow_action_result_from_mcp_payload(
            tool_name=request.action_id,
            payload={
                "message_id": message_id,
                "threadId": f"thread-{message_id}",
                "labelIds": ["INBOX"],
                "sender": f"Sender {message_id} <sender-{message_id}@example.test>",
                "from": f"Sender {message_id} <sender-{message_id}@example.test>",
                "subject": f"Subject {message_id}",
                "date": "Sat, 25 Apr 2026 09:00:00 +0000",
                "snippet": f"Preview {message_id}",
                "payload": {
                    "headers": [{"name": "Subject", "value": f"Subject {message_id}"}],
                    "parts": [{"body": large_marker * 64}],
                },
            },
            duration_ms=1.0,
        )

    registry.set_fallback_handler(fake_mcp_fallback)
    context: dict[str, Any] = {
        "gmail_profile": "default-profile",
        "mail_review_item_output_requirement": {
            "schema_version": "workflow_item_output_requirement.v1",
            "purpose": "mail_list_rendering",
            "required_fields": ["message_id", "subject", "sender"],
            "excluded_fields": ["gmail_message_detail", "payload", "body"],
        },
    }
    result = registry.execute(
        "workflow_control.for_each",
        inputs={
            "workflow_id": GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
            "items": [{"message_id": "msg-1"}, {"message_id": "msg-2"}],
            "item_context_key": "current_mail_message",
            "item_limit": 2,
            "success_policy": "all_must_succeed",
            "max_transitions": 8,
            "include_tool_invocations_in_iteration_results": False,
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("for_each_success_count") == 2
    assert result.outputs.get("for_each_error_count") == 0
    assert result.outputs.get("iteration_errors") == []
    assert result.outputs.get("successful_results") == [
        {
            "message_id": "msg-1",
            "subject": "Subject msg-1",
            "sender": "Sender msg-1 <sender-msg-1@example.test>",
        },
        {
            "message_id": "msg-2",
            "subject": "Subject msg-2",
            "sender": "Sender msg-2 <sender-msg-2@example.test>",
        },
    ]

    parent_visible_payload = {
        "iteration_results": result.outputs.get("iteration_results"),
        "successful_results": result.outputs.get("successful_results"),
    }
    serialised_parent_payload = json.dumps(
        parent_visible_payload,
        sort_keys=True,
        default=str,
    )
    assert large_marker not in serialised_parent_payload
    for iteration in result.outputs.get("iteration_results", []):
        assert isinstance(iteration, dict)
        assert "gmail_message_detail" not in (iteration.get("result") or {})
        assert "payload" not in (iteration.get("result") or {})
        envelope = iteration.get("result_envelope")
        assert isinstance(envelope, dict)
        declared_payload = envelope.get("declared_output_payload")
        assert "gmail_message_detail" not in (declared_payload or {})
        assert "payload" not in (declared_payload or {})


def test_turn_and_episode_prompt_authority_resolve_on_live_surface(
    _reset_mock_db: Any,
) -> None:
    turn_report = bootstrap_canonical_conversation_turn_workflows()
    episode_report = bootstrap_canonical_episode_evaluation_workflow()

    assert turn_report.get("success") is True
    assert episode_report.get("success") is True

    narration_rows = get_texts_for_concept(
        NARRATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    recovery_rows = get_texts_for_concept(
        RECOVERY_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    episode_rows = get_texts_for_concept(
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    selector_rows = get_texts_for_concept(
        SELECTOR_PROMPT_CONCEPT_ID,
        predicate="hasContent",
        limit=5,
    )
    assert any((row or {}).get("text") for row in selector_rows)
    assert any((row or {}).get("text") for row in narration_rows)
    assert any((row or {}).get("text") for row in recovery_rows)
    assert any((row or {}).get("text") for row in episode_rows)

    episode_definition = load_workflow_definition_from_vontology(
        EPISODE_EVALUATION_WORKFLOW_ID
    )
    assert episode_definition is not None
    evaluate_step_id = authority_service._step_concept_id(
        workflow_id=EPISODE_EVALUATION_WORKFLOW_ID,
        state_id="evaluate_episode",
    )
    evaluate_action = episode_definition.states[evaluate_step_id].actions[0]
    prompt_contract = evaluate_action.prompt_contract
    assert isinstance(prompt_contract, dict)
    assert prompt_contract.get("resolved_prompt_concept_id") == (
        EPISODE_EVALUATION_PROMPT_CONCEPT_ID
    )


def test_bootstrap_repairs_bundle_snapshot_drift_for_conversation_turn_workflow_family(
    _reset_mock_db: Any,
) -> None:
    bootstrap_canonical_conversation_turn_workflows()

    definition = load_workflow_definition_from_vontology(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert definition is not None

    routing_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="routing",
    )
    prelude_step_id = authority_service._step_concept_id(
        workflow_id=CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        state_id="workflow_experience_context_prelude",
    )
    assert definition.initial_state == prelude_step_id

    workflow_concept = concept_service.get_concept_by_concept_id(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert workflow_concept is not None
    relationships = dict(workflow_concept.get("relationships") or {})
    for alias in (
        "#V#hasInitialStep",
        "hasInitialStep",
        "#V#has_initial_step",
        "has_initial_step",
    ):
        relationships.pop(alias, None)
    relationships["#V#hasInitialStep"] = [routing_step_id]
    concept_service.update_concept(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
        {"relationships": relationships},
    )

    drifted_definition = load_workflow_definition_from_vontology(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert drifted_definition is not None
    assert drifted_definition.initial_state == routing_step_id

    repair_report = bootstrap_canonical_conversation_turn_workflows(
        force_republish=True,
    )
    publication = repair_report.get("publication") or {}
    assert publication.get("materialisation_status") == "repaired_from_repo_seed"
    assert publication.get("skipped") is not True
    assert publication.get("drift_detected") is True
    assert publication.get("drift_workflow_ids") == []
    assert publication.get("issue_codes") == []
    assert publication.get("bundle_snapshot_drift_detected") is True
    assert CONVERSATION_TURN_EXECUTION_WORKFLOW_ID in (
        publication.get("bundle_snapshot_drift_workflow_ids") or []
    )
    assert "definition_mismatch" in (
        publication.get("bundle_snapshot_issue_codes") or []
    )
    assert CONVERSATION_TURN_EXECUTION_WORKFLOW_ID in (
        publication.get("published_workflow_ids") or []
    )

    repaired_definition = load_workflow_definition_from_vontology(
        CONVERSATION_TURN_EXECUTION_WORKFLOW_ID
    )
    assert repaired_definition is not None
    assert repaired_definition.initial_state == prelude_step_id
