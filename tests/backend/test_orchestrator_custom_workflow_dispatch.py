"""Focused workflow selector routing tests split from the shared orchestrator harness."""

from __future__ import annotations

from tests.backend.test_orchestrator_workflow_selector_routing import *  # noqa: F401,F403
from tests.backend.test_orchestrator_workflow_selector_routing import (
    _CapturingLLM,
    _ModelCandidate,
    _build_orchestrator,
    _build_structured_turn_contract_payload,
    _dispatch_surface,
    _register_terminal_custom_workflow,
    _stub_execute_workflow_result,
)


def test_non_standard_workflow_routes_via_execute_workflow(monkeypatch):
    """Workflows in the registry but not in the standard set should use execute_workflow."""

    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    execute_calls: list[dict[str, Any]] = []

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        execute_calls.append(
            {
                "workflow_id": workflow_id,
                "data": dict(kwargs.get("data") or {}),
            }
        )
        return SimpleNamespace(
            completed=True,
            final_state="completed",
            data={"response_text": "Todo refresh complete."},
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    # The selector returns #V#todo_refresh_workflow, which IS in the registry.
    # We pass it via workflow_discovery_result so the selector treats it as a
    # valid discovered workflow ID instead of falling back to plain_response.
    llm = _CapturingLLM(
        [
            TODO_REFRESH_WORKFLOW_ID.lower(),  # selector verdict
            # No additional LLM responses needed — todo_refresh check_cache
            # handler will short-circuit to completed because there's no
            # user namespace tasks.
        ]
    )

    discovery_result = {
        "matches": [
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo Refresh Workflow",
                "description": "Refreshes user's todo list from Jira.",
            }
        ],
        "match_count": 1,
    }

    result = orchestrator.run(
        prompt="Refresh my todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    # The workflow executed via execute_workflow and produced a result.
    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types
    assert len(execute_calls) == 1
    assert execute_calls[0]["workflow_id"] == TODO_REFRESH_WORKFLOW_ID
    handoff_data = execute_calls[0]["data"]
    assert isinstance(handoff_data.get("workflow_discovery_result"), dict)
    assert isinstance(handoff_data.get("workflow_routing"), dict)
    assert handoff_data.get("selected_workflow_id") == TODO_REFRESH_WORKFLOW_ID

    # Should have a workflow_execution entry (non-standard workflow dispatch).
    execution_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is not None
    assert execution_entry["workflow_id"] == TODO_REFRESH_WORKFLOW_ID
    assert execution_entry["completed"] is True



def test_non_standard_workflow_emits_custom_workflow_execution_summary(monkeypatch):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#custom_summary_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="done",
                states={"done": WorkflowStateSpec(state_id="done", terminal=True)},
            ),
            purpose="Workflow execution summary regression test.",
            source="test",
        )
    )

    def _run_workflow(_workflow_def: Any, **_kwargs: Any):
        return SimpleNamespace(
            completed=True,
            final_state="done",
            error=None,
            data={
                "response_text": "Workflow created.",
                "created_workflow_ids": ["#V#wf_new"],
                "alignment": {"updated_type_ids": ["#V#durable_workflow"]},
                "workflow_step_result_envelopes": [
                    {
                        "state_id": "prepare",
                        "action_id": "tool.prepare",
                        "action_outcome": "success",
                    },
                    {
                        "state_id": "persist",
                        "action_id": "tool.persist",
                        "action_outcome": "success",
                    },
                ],
                "workflow_terminal_effect_events": [
                    {
                        "state_id": "done",
                        "symbol": "#V#workflow_effect_custom_summary_done_terminal",
                        "alias": "workflow_effect_custom_summary_done_terminal",
                        "applied": True,
                    }
                ],
                "workflow_control_flow_events": [
                    {"status": "entered_state", "state_id": "prepare"},
                    {"status": "entered_state", "state_id": "persist"},
                ],
            },
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    llm = _CapturingLLM([selected_workflow_id.lower()])
    result = orchestrator.run(
        prompt="Create the workflow definition.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [{"concept_id": selected_workflow_id}],
            "match_count": 1,
        },
    )

    execution_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is not None
    assert execution_entry["workflow_id"] == selected_workflow_id
    assert execution_entry["completed"] is True

    execution_summary = execution_entry.get("execution_summary")
    assert isinstance(execution_summary, dict)
    assert execution_summary["workflow_id"] == selected_workflow_id
    assert execution_summary["step_result_envelope_count"] == 2
    assert execution_summary["action_completed_count"] == 2
    assert execution_summary["action_success_count"] == 2
    assert execution_summary["terminal_effect_count"] == 1
    assert execution_summary["durable_side_effect_count"] == 2
    assert execution_summary["durable_side_effects"] == [
        {
            "mutation_kind": "created",
            "artefact_type": "workflow",
            "source_key": "created_workflow_ids",
            "source_path": "created_workflow_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#wf_new"],
        },
        {
            "mutation_kind": "updated",
            "artefact_type": "type",
            "source_key": "updated_type_ids",
            "source_path": "alignment.updated_type_ids",
            "artefact_count": 1,
            "artefact_ids": ["#V#durable_workflow"],
        },
    ]



def test_custom_workflow_run_applies_launch_contract_without_workflow_specific_glue(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#launch_contract_custom_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare",
                states={
                    "prepare": WorkflowStateSpec(
                        state_id="prepare",
                        actions=(WorkflowActionInvocation(action_id="tool.prepare"),),
                        terminal=True,
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["invitation_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "invitation_text",
                                "source_expression": "inputs.prompt",
                                "extractor": "first_quoted_text",
                                "required": True,
                            },
                            {
                                "target_context_key": "candidate_workflow_ids",
                                "source_expression": "inputs.workflow_discovery_result.matches",
                                "extractor": "workflow_id_list",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Custom launch-contract workflow for selector dispatch tests.",
            source="test",
        )
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="prepare",
            error=None,
            data={"response_text": "Prepared."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    prompt = (
        "Run a meeting-invitation test on this invitation text:\n\n"
        '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
        'for a project planning meeting about the Q2 roadmap."'
    )
    discovery_result = {
        "matches": [
            {
                "concept_id": selected_workflow_id,
                "name": "Launch contract custom workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
            {
                "concept_id": "#V#synthetic_workflow_regression_suite_workflow",
                "name": "Synthetic workflow regression suite workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            },
        ],
        "candidates": [
            {
                "concept_id": selected_workflow_id,
                "name": "Launch contract custom workflow",
                "is_executable": True,
                "executability_reason": "executable_now",
            }
        ],
        "match_count": 2,
    }

    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result=discovery_result,
        conversation_session_id="chat-launch",
        turn_id="turn-launch",
    )

    assert result.response_text == "Prepared."
    assert captured_data["invitation_text"] == (
        "Kia ora team, please join us on Tuesday at 2:00pm in Room 4 for a "
        "project planning meeting about the Q2 roadmap."
    )
    assert captured_data["candidate_workflow_ids"] == [
        selected_workflow_id,
        "#V#synthetic_workflow_regression_suite_workflow",
    ]
    assert captured_data["selected_workflow_id"] == selected_workflow_id

    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == [
        "candidate_workflow_ids",
        "invitation_text",
    ]



def test_custom_workflow_launchability_promotes_launchable_replacement_candidate(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#non_launchable_custom_workflow"
    launchable_workflow_id = "#V#launchable_custom_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["target_workflow_ids"]},
                    )
                },
            ),
            purpose="Non-launchable custom workflow for selector override tests.",
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=launchable_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=launchable_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["invitation_text"]},
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["invitation_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "invitation_text",
                                "source_expression": "inputs.prompt",
                                "extractor": "first_quoted_text",
                                "required": True,
                            },
                            {
                                "target_context_key": "candidate_workflow_ids",
                                "source_expression": "inputs.workflow_discovery_result.matches",
                                "extractor": "workflow_id_list",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Launchable custom workflow for selector override tests.",
            source="test",
        )
    )

    captured_execution: dict[str, Any] = {}

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        captured_execution["workflow_id"] = workflow_def.workflow_id
        captured_execution["data"] = dict(data)
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={
                "response_text": "Handled via safe tool pipeline fallback.",
                "final_response": "Handled via safe tool pipeline fallback.",
            },
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt=(
            "Run a meeting-invitation test on this invitation text:\n\n"
            '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
            'for a project planning meeting about the Q2 roadmap."'
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
                {
                    "concept_id": launchable_workflow_id,
                    "name": "Launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
                {
                    "concept_id": launchable_workflow_id,
                    "name": "Launchable custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="chat-launch-override",
        turn_id="turn-launch-override",
    )

    assert result.response_text == "Handled via safe tool pipeline fallback."
    assert captured_execution["workflow_id"] == launchable_workflow_id

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == launchable_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_not_launchable_from_turn_inputs"
        for entry in result.aux_llm_calls
    )

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert [entry.get("boundary") for entry in dispatch_boundaries[-3:]] == [
        "execution_mode_selected",
        "workflow_handoff",
        "workflow_terminal",
    ]
    assert dispatch_boundaries[-3].get("selected_workflow_id") == launchable_workflow_id
    assert dispatch_boundaries[-2].get("selected_workflow_id") == launchable_workflow_id
    assert dispatch_boundaries[-1].get("selected_workflow_id") == launchable_workflow_id



def test_custom_workflow_launchability_override_failure_preserves_selector_decision_but_surfaces_truthful_failure(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_inputs",
                states={
                    "normalise_inputs": WorkflowStateSpec(
                        state_id="normalise_inputs",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["file_copy_concept_id"]},
                    )
                },
            ),
            purpose="Selected workflow whose launchability override path will fail.",
            source="test",
        )
    )

    def _unexpected_execute_workflow(*_args: Any, **_kwargs: Any):
        raise AssertionError(
            "execute_workflow should not run after launchability gate failure"
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _unexpected_execute_workflow)

    def _raise_override_failure(**_kwargs: Any) -> Any:
        raise AttributeError("override policy exploded")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.choose_custom_workflow_override_candidate",
        _raise_override_failure,
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2402.18144",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="chat-launchability-override-failure",
        turn_id="turn-launchability-override-failure",
    )

    assert (
        result.response_text
        == "I attempted to use tools but the tool-pipeline handoff failed before "
        "tool execution could begin. Please try again or report this issue."
    )
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert result.extra_messages == ()
    assert result.tool_invocations == ()

    failure_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selector_unmatched_candidate_requires_safe_general_fallback"
        ),
        None,
    )
    assert failure_entry is not None
    assert failure_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert failure_entry.get("requested_candidate_workflow_id") == selected_workflow_id
    assert failure_entry.get("prior_selected_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID



def test_custom_workflow_override_prefers_semantically_fit_execution_candidate(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#non_launchable_custom_workflow"
    authoring_workflow_id = "#V#launchable_authoring_workflow"
    execution_workflow_id = "#V#meeting_invitation_testing_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["target_workflow_ids"]},
                    )
                },
            ),
            purpose="Generic non-launchable custom workflow for selector override tests.",
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=authoring_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=authoring_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="workflow_authoring.identify_need"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "authoring",
                        "authoring_intent_required": True,
                        "prefer_existing_capability": True,
                    }
                },
            ),
            purpose=(
                "Create and verify executable workflows from a workflow "
                "description request."
            ),
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=execution_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=execution_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["invitation_text"]},
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["invitation_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "invitation_text",
                                "source_expression": "inputs.prompt",
                                "extractor": "first_quoted_text",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Run meeting invitation testing against an invitation specimen.",
            source="test",
        )
    )

    captured_execution: dict[str, Any] = {}

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        captured_execution["workflow_id"] = workflow_def.workflow_id
        captured_execution["data"] = dict(data)
        return SimpleNamespace(
            completed=True,
            final_state="prepare_spec",
            error=None,
            data={"response_text": "Prepared via meeting invitation workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt=(
            "Run a meeting invitation test on this invitation text:\n\n"
            '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4 '
            'for a project planning meeting about the Q2 roadmap."'
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "description": "Generic workflow with missing launch inputs.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.41,
                    "confidence_score": 0.41,
                },
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.58,
                    "confidence_score": 0.58,
                },
                {
                    "concept_id": execution_workflow_id,
                    "name": "Meeting invitation testing workflow",
                    "description": "Run meeting invitation testing against an invitation specimen.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.87,
                    "confidence_score": 0.87,
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Non-launchable custom workflow",
                    "description": "Generic workflow with missing launch inputs.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.41,
                    "confidence_score": 0.41,
                },
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.58,
                    "confidence_score": 0.58,
                },
                {
                    "concept_id": execution_workflow_id,
                    "name": "Meeting invitation testing workflow",
                    "description": "Run meeting invitation testing against an invitation specimen.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.87,
                    "confidence_score": 0.87,
                },
            ],
            "match_count": 3,
        },
        conversation_session_id="chat-launch-semantic-override",
        turn_id="turn-launch-semantic-override",
    )

    assert result.response_text == "Prepared via meeting invitation workflow."
    assert captured_execution["workflow_id"] == execution_workflow_id
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == execution_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None



def test_launchability_replacement_declines_testing_workflow_for_conceptual_prompt(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#scholarly_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_inputs",
                states={
                    "normalise_inputs": WorkflowStateSpec(
                        state_id="normalise_inputs",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["file_copy_concept_id"]},
                    )
                },
            ),
            purpose=(
                "Canonical durable workflow for representing scholarly papers "
                "from file-copy artefacts, metadata, and verification requirements."
            ),
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "prefer_existing_capability": False,
                    },
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt_text",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    execute_calls: list[dict[str, Any]] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        execute_calls.append({"workflow_id": workflow_id})
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "final_response": "Fallback via generic tool pipeline.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    prompt = (
        "OK, thinking about the way papers are represented at the moment, think "
        "about papers under preparation. How should they be represented. What is "
        "common between them and published (or rejected papers) and what is "
        "unique to the under-preparation status. Are any ontological edits needed"
    )
    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.914,
                    "confidence_score": 0.984,
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.914,
                    "confidence_score": 0.984,
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-paper-representation-safe-fallback",
        turn_id="turn-paper-representation-safe-fallback",
    )

    assert execute_calls
    assert execute_calls[0]["workflow_id"] == TOOL_CALLING_WORKFLOW_ID

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert result.response_text == "Fallback via generic tool pipeline."

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None

    selector_prompt_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_prompt"
        ),
        None,
    )
    assert selector_prompt_entry is not None
    excluded_candidates = selector_prompt_entry.get("discovery_excluded_candidates")
    assert isinstance(excluded_candidates, list)
    excluded_testing = next(
        (
            item
            for item in excluded_candidates
            if isinstance(item, dict) and item.get("concept_id") == testing_workflow_id
        ),
        None,
    )
    assert isinstance(excluded_testing, dict)
    assert excluded_testing.get("routing_profile_role") == "maintenance"
    assert (
        excluded_testing.get("routing_exclusion_reason")
        == "explicit_workflow_context_required_by_workflow_profile"
    )

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selector_unmatched_candidate_requires_safe_general_fallback"
        ),
        None,
    )
    assert override_entry is not None
    assert (
        override_entry.get("prior_selected_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert override_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert override_entry.get("requested_candidate_workflow_id") == selected_workflow_id
    assert "launch_viability_probe" not in (override_entry or {})

    override_reasons = {
        entry.get("reason")
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
    }
    assert (
        "selector_unmatched_candidate_requires_safe_general_fallback"
        in override_reasons
    )



def test_launchability_replacement_allows_testing_workflow_for_explicit_testing_prompt(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "policy_active": False,
            "guidance_mode": "none",
            "candidate_scores": [],
            "ranked_candidate_ids": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#scholarly_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_inputs",
                states={
                    "normalise_inputs": WorkflowStateSpec(
                        state_id="normalise_inputs",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                        metadata={"reads_context_keys": ["file_copy_concept_id"]},
                    )
                },
            ),
            purpose=(
                "Canonical durable workflow for representing scholarly papers "
                "from file-copy artefacts, metadata, and verification requirements."
            ),
            source="test",
        )
    )
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=testing_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=testing_workflow_id,
                initial_state="prepare_fixture",
                states={
                    "prepare_fixture": WorkflowStateSpec(
                        state_id="prepare_fixture",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "routing_profile": {
                        "role": "testing",
                        "authoring_intent_required": False,
                        "prefer_existing_capability": False,
                    },
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt_text"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt_text",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose=(
                "Execute the canonical arXiv ingestion workflow against one live "
                "arXiv paper, verify represented scholarly metadata and provenance, "
                "and clean up transient artefacts afterwards."
            ),
            source="test",
        )
    )

    execute_calls: list[dict[str, Any]] = []

    def _run_workflow(
        workflow_def: WorkflowDefinition,
        *,
        data: Mapping[str, Any],
        **_kwargs: Any,
    ):
        execute_calls.append(
            {"workflow_id": workflow_def.workflow_id, "data": dict(data)}
        )
        return SimpleNamespace(
            completed=True,
            final_state="complete",
            error=None,
            data={"response_text": "Executed via arXiv testing workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    prompt = (
        "Run the arXiv paper ingestion testing workflow on "
        "https://arxiv.org/abs/2603.21702 and verify title, authors, abstract, "
        "publication date, provenance, and cleanup."
    )
    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.71,
                    "confidence_score": 0.71,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Scholarly Paper Representation Workflow",
                    "description": (
                        "Canonical durable workflow for representing scholarly "
                        "papers from file-copy artefacts, metadata, and "
                        "verification requirements."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.71,
                    "confidence_score": 0.71,
                },
                {
                    "concept_id": testing_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against "
                        "one live arXiv paper, verify represented scholarly "
                        "metadata and provenance, and clean up transient "
                        "artefacts afterwards."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-explicit-arxiv-testing",
        turn_id="turn-explicit-arxiv-testing",
    )

    assert execute_calls
    assert execute_calls[0]["workflow_id"] == testing_workflow_id

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == testing_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    override_policy_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "custom_workflow_override_policy"
        ),
        None,
    )
    assert override_policy_entry is None

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selected_custom_workflow_not_launchable_from_turn_inputs"
        ),
        None,
    )
    assert override_entry is None



def test_custom_workflow_first_step_failure_projects_terminal_locality_before_tool_pipeline_fallback(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#launch_contract_failure_custom_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="prepare_spec",
                states={
                    "prepare_spec": WorkflowStateSpec(
                        state_id="prepare_spec",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_spec"),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose="Custom workflow failure locality projection test.",
            source="test",
        )
    )

    tool_pipeline_payload: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                completed=False,
                final_state="prepare_spec",
                error="workflow_launch_input_resolution_failed:invitation_text",
                data={
                    "response_text": (
                        f"Workflow {selected_workflow_id} could not start because "
                        "required launch inputs were unresolved: invitation_text."
                    ),
                    "workflow_launch_input_resolution": {
                        "status": "failed",
                        "unresolved_required_inputs": ["invitation_text"],
                        "failing_state_id": "prepare_spec",
                        "failing_action_id": "tool.prepare_spec",
                    },
                },
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                completed=True,
                final_state="complete",
                error=None,
                data={
                    "final_response": "Recovered through the general tool workflow.",
                    "tool_messages": [],
                    "invocations": [{"tool": "extract_url"}],
                    "iteration_count": 1,
                },
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    progress_events: list[dict[str, Any]] = []
    orchestrator.set_progress_callback(lambda info: progress_events.append(dict(info)))

    result = orchestrator.run(
        prompt=(
            "Run the meeting invitation testing workflow on this invitation:\n\n"
            '"Kia ora team, please join us on Tuesday at 2:00pm in Room 4."'
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launch contract failure custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launch contract failure custom workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="chat-launch-failure",
        turn_id="turn-launch-failure",
    )

    assert result.response_text == "Recovered through the general tool workflow."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "prepare_spec",
        "operational_error": "workflow_launch_input_resolution_failed:invitation_text",
        "user_visible_failure_text": (
            f"Workflow {selected_workflow_id} could not start because required "
            "launch inputs were unresolved: invitation_text."
        ),
    }

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    terminal_boundary = next(
        (
            entry
            for entry in dispatch_boundaries
            if entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("selected_execution_mode") == "custom_workflow"
    assert terminal_boundary.get("selected_workflow_id") == selected_workflow_id
    assert terminal_boundary.get("dispatch_workflow_id") == selected_workflow_id
    assert terminal_boundary.get("final_state") == "prepare_spec"
    assert terminal_boundary.get("reason") == "workflow_launch_input_resolution_failed"
    assert terminal_boundary.get("workflow_launch_input_resolution_status") == "failed"
    assert terminal_boundary.get("unresolved_required_inputs") == ["invitation_text"]
    assert terminal_boundary.get("failing_state_id") == "prepare_spec"
    assert terminal_boundary.get("failing_action_id") == "tool.prepare_spec"
    assert terminal_boundary.get("continued_to_tool_pipeline") is True

    terminal_progress = next(
        (
            entry
            for entry in reversed(progress_events)
            if entry.get("phase_label") == "Workflow terminal state"
        ),
        None,
    )
    assert terminal_progress is not None
    assert terminal_progress.get("selected_workflow_id") == selected_workflow_id
    assert terminal_progress.get("selected_workflow_name") == (
        "Launch contract failure custom workflow"
    )
    assert terminal_progress.get("workflow_selector_verdict") == "rag_selected"
    assert terminal_progress.get("workflow_selector_source") == "selector"
    assert isinstance(terminal_progress.get("workflow_selection_rationale"), str)
    assert terminal_progress.get("workflow_selection_rationale")



def test_custom_tool_pipeline_workflow_dispatches_without_id_special_casing(
    monkeypatch,
):
    """Custom discovered workflows that satisfy the tool pipeline contract should
    execute through the tool workflow path without a workflow-ID allowlist.
    """

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    custom_workflow_id = "#V#custom_tool_pipeline_workflow"

    tool_workflow_def = orchestrator._workflow_registry.get(TOOL_CALLING_WORKFLOW_ID)
    assert tool_workflow_def is not None
    orchestrator._workflow_registry.register_if_absent(
        WorkflowRegistration(
            workflow_id=custom_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=custom_workflow_id,
                initial_state=tool_workflow_def.initial_state,
                states=tool_workflow_def.states,
                termination_states=tool_workflow_def.termination_states,
                purpose="Custom tool pipeline workflow for dispatch parity tests.",
            ),
            purpose="Custom tool pipeline workflow for dispatch parity tests.",
            source="test",
        )
    )

    class _WorkflowResult:
        def __init__(self):
            self.data = {
                "final_response": "Custom tool pipeline response.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            }
            self.final_state = "completed"
            self.completed = True

    execute_calls: list[dict[str, Any]] = []

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        call = {
            "workflow_id": workflow_id,
            "episode_stage": kwargs.get("data", {}).get("workflow_episode_stage"),
        }
        execute_calls.append(call)
        if call["episode_stage"] != "tool_calling":
            raise AssertionError(
                "Custom tool-pipeline workflow should dispatch via tool_calling stage."
            )
        return _WorkflowResult()

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM([custom_workflow_id.lower()])
    discovery_result = {
        "matches": [
            {
                "concept_id": custom_workflow_id,
                "name": "Custom Tool Pipeline Workflow",
                "description": "Test workflow mirroring tool-calling actions.",
            }
        ],
        "match_count": 1,
    }

    result = orchestrator.run(
        prompt="Use the custom tool pipeline",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    assert result.response_text == "Custom tool pipeline response."
    assert len(execute_calls) == 1
    assert execute_calls[0]["workflow_id"] == custom_workflow_id
    assert execute_calls[0]["episode_stage"] == "tool_calling"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: selector disabled → no classifier call.
# ---------------------------------------------------------------------------


