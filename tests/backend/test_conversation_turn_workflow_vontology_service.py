from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import concept_service
from src.backend.services.conversation_turn_workflow_vontology_service import (
    _ensure_conversation_turn_prompt_support,
    bootstrap_canonical_conversation_turn_workflows,
)
from src.backend.services.episode_evaluation_workflow_contracts import (
    EPISODE_EVALUATION_PROMPT_CONCEPT_ID,
    EPISODE_EVALUATION_WORKFLOW_ID,
)
from src.backend.services.episode_evaluation_workflow_vontology_service import (
    bootstrap_canonical_episode_evaluation_workflow,
)
from src.backend.services.text_value_service import get_texts_for_concept
from src.backend.workflows import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
    workflow_concept_authority_service as authority_service,
)
from src.backend.workflows.vontology_loader import (
    load_workflow_definition_from_vontology,
)

EXPECTED_OUTCOME_PROMPT_CONCEPT_ID = (
    "#V#prompt_turn_execution_expected_outcome_inference"
)
SELECTOR_PROMPT_CONCEPT_ID = "#V#chat_turn_classifier_prompt"
NARRATION_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_narrate_completion_report"
RECOVERY_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_recovery_decision"
MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID = "#V#missing_tool_call_retry_prompt"
POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_postcondition_critic"
GENERAL_MAIL_REVIEW_WORKFLOW_ID = "#V#general_mail_review_workflow"


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
    assert report.get("seeded_prompt_count") == 7

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
    assert "represented labels, categories, tags, workflow markers" in (
        missing_tool_retry_text
    )
    assert "Do not substitute external label-listing tools" in (missing_tool_retry_text)

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
    assert "Do not assume the current request is standalone" in selector_text
    assert "Canonical valid output examples" in selector_text
    assert "Invalid outputs. Never do any of these" in selector_text
    assert "optional `workflow_inputs`" in selector_text
    assert "selected-workflow launch parameters" in selector_text
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


def test_bootstrap_materialises_conversation_turn_workflow_family_and_prompt_links(
    _reset_mock_db: Any,
) -> None:
    report = bootstrap_canonical_conversation_turn_workflows()

    publication = report.get("publication") or {}
    counts = publication.get("counts") or {}
    assert report.get("success") is True
    assert counts.get("errors") == 0
    assert counts.get("workflows_published") == 7

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
    assert "find_relations_with_argument" in mail_review_resolver_action.llm_policy.get(
        "required_tools", []
    )
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
        "predicate_filter"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "do not use predicate_concept_id"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
    )
    assert (
        "must not start with #V#"
        in mail_review_resolver_action.llm_policy["response_contract_text"]
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
            state_id="prepare_mail_review_tool_prompt",
        ),
        authority_service._step_concept_id(
            workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
            state_id="profile_choice_needed",
        ),
    }.issubset(resolver_branch_targets)
    assert "gmail_profile" in mail_review_definition.states[
        mail_review_resolver_step_id
    ].metadata.get("writes_context_keys", [])
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
        and "bypass_profile_query_prefix" in str(item.get("template") or "")
        for item in prompt_assignments
    )
    mail_review_delegate_step_id = authority_service._step_concept_id(
        workflow_id=GENERAL_MAIL_REVIEW_WORKFLOW_ID,
        state_id="delegate_to_tool_pipeline",
    )
    mail_review_delegate_action = mail_review_definition.states[
        mail_review_delegate_step_id
    ].actions[0]
    assert mail_review_delegate_action.action_id == "workflow_invoke_subworkflow"
    assert mail_review_delegate_action.subworkflow_id == TOOL_CALLING_WORKFLOW_ID
    assert mail_review_delegate_action.inputs["inherit_parent_context"] is True
    assert mail_review_delegate_action.inputs["prompt"] == {
        "$context_key": "mail_review_tool_prompt",
        "$mapping_concept_id": "#V#workflow_mapping_general_mail_review_delegate_tool_prompt_to_prompt",
        "$required": True,
    }
    assert mail_review_delegate_action.inputs["prompt_for_requirements"] == {
        "$context_key": "mail_review_tool_prompt",
        "$mapping_concept_id": "#V#workflow_mapping_general_mail_review_delegate_tool_prompt_to_prompt_for_requirements",
        "$required": True,
    }
    assert mail_review_delegate_action.inputs["llm_allowed_tools"] == [
        "gmail_list_messages",
        "gmail_get_message",
    ]
    assert mail_review_delegate_action.inputs["gmail_profile"] == {
        "$context_key": "gmail_profile",
        "$mapping_concept_id": "#V#workflow_mapping_general_mail_review_delegate_gmail_profile_to_gmail_profile",
        "$required": True,
    }
    assert mail_review_delegate_action.inputs["mail_review_profile_id"] == {
        "$context_key": "mail_review_profile_id",
        "$mapping_concept_id": "#V#workflow_mapping_general_mail_review_delegate_mail_review_profile_id_to_mail_review_profile_id",
        "$required": False,
    }
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
    expected_outcome_prompt_contract = expected_outcome_action.prompt_contract
    assert isinstance(expected_outcome_prompt_contract, dict)
    assert expected_outcome_prompt_contract.get("resolved_prompt_concept_id") == (
        EXPECTED_OUTCOME_PROMPT_CONCEPT_ID
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
    assert "turn_expected_required_tools" in (
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
