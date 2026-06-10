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


def test_same_session_follow_up_does_not_rehydrate_python_write_intent_memory(
    monkeypatch,
):
    """Same-session follow-up turns should remain selector-owned."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    first = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-same",
    )
    assert first.workflow_routing is not None
    assert first.workflow_routing.verdict == "rag_selected"
    assert first.workflow_routing.source == "selector"

    second = orchestrator.run(
        prompt="Yes, do it.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-same",
    )
    assert second.workflow_routing is not None
    assert second.workflow_routing.verdict == "rag_selected"
    assert second.workflow_routing.source == "selector"
    gate_entry = next(
        (
            entry
            for entry in second.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("continuation_context_reused") is False
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "write_intent_session_memory"
        for entry in second.aux_llm_calls
    )


def test_cross_session_follow_up_has_no_python_write_intent_reuse(monkeypatch):
    """Cross-session follow-up turns should also remain selector-owned."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    first = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-a",
    )
    assert first.workflow_routing is not None
    assert first.workflow_routing.verdict == "rag_selected"
    assert first.workflow_routing.source == "selector"

    second = orchestrator.run(
        prompt="Yes, do it.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-b",
    )
    assert second.workflow_routing is not None
    assert second.workflow_routing.verdict == "rag_selected"
    assert second.workflow_routing.source == "selector"

    aux_types = [
        entry.get("type") for entry in second.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector_override" not in aux_types
    gate_entry = next(
        (
            entry
            for entry in second.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("reason") == "workflow_llm_owns_mutation_routing"
    assert gate_entry.get("continuation_context_reused") is False
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "write_intent_session_memory"
        for entry in second.aux_llm_calls
    )


def test_confirm_structure_follow_up_does_not_rehydrate_python_write_intent_memory(
    monkeypatch,
):
    """Short confirmation prompts should not trigger Python write-intent reuse."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    first = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-structure",
    )
    assert first.workflow_routing is not None
    assert first.workflow_routing.verdict == "rag_selected"
    assert first.workflow_routing.source == "selector"

    second = orchestrator.run(
        prompt="Confirm structure.",
        context=[],
        llm_client=_CapturingLLM(
            [
                CHAT_ASSISTANT_WORKFLOW_ID,
                "Plain response only.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1328-structure",
    )
    assert second.workflow_routing is not None
    assert second.workflow_routing.verdict == "rag_selected"
    assert second.workflow_routing.source == "selector"
    gate_entry = next(
        (
            entry
            for entry in second.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("continuation_context_reused") is False
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "write_intent_session_memory"
        for entry in second.aux_llm_calls
    )


def test_tool_planner_receives_authoritative_workflow_continuation_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1380",
            "selected_workflow_id": "#V#scholarly_paper_representation_workflow",
            "completion_gate_decision": "escalation_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_paper_representation_1",
                    "effect_type": "scholarly_representation",
                    "description": "Represent the corresponding scholarly paper.",
                    "required_tools": ["interpret_file_copy"],
                    "targets": ["#V#uploaded_file_copy_abc123"],
                }
            ],
            "required_effects_contract": {
                "schema_version": "required_effects_contract.v1",
                "intent_class": "representation",
                "domain_profile_id": "paper",
                "artefact_context": {
                    "file_copy_ids": ["#V#uploaded_file_copy_abc123"],
                    "urls": [],
                },
            },
        },
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
            "I'll continue the representation work.",
            "Follow-through response.",
            "Final response after tool workflow.",
        ]
    )

    result = orchestrator.run(
        prompt="Please proceed.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1380",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"

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
    continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in continuation_context
    assert "#V#scholarly_paper_representation_workflow" in continuation_context

    planner_context = llm.calls[1]["context"] or []
    planner_prompt_context = "\n".join(
        str(message.get("content") or "")
        for message in planner_context
        if isinstance(message, dict)
    )
    assert llm.calls[1]["prompt"] == "Please proceed."
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in planner_prompt_context
    assert "#V#scholarly_paper_representation_workflow" in planner_prompt_context
    assert "#V#uploaded_file_copy_abc123" in planner_prompt_context

    continuation_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_continuation_context"
        ),
        None,
    )
    assert continuation_entry is not None
    assert continuation_entry.get("applied") is True
    assert continuation_entry.get("reason") == "workflow_state_authoritative"


def test_custom_workflow_dispatch_projects_launch_inputs_from_applied_continuation_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_arxiv_source",
                states={
                    "normalise_arxiv_source": WorkflowStateSpec(
                        state_id="normalise_arxiv_source",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="arxiv.normalise_source"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            },
                            {
                                "target_context_key": "source_uri",
                                "source_expression": "inputs.source_uri",
                            },
                            {
                                "target_context_key": "arxiv_id",
                                "source_expression": "inputs.arxiv_id",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Continuation-aware arXiv workflow dispatch test.",
            source="test",
        )
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1858",
            "selected_workflow_id": selected_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "status": "not_executed",
                    "description": (
                        "Obtain the selected workflow result needed for the "
                        "user-facing answer."
                    ),
                }
            ],
            "required_effects_contract": {
                "schema_version": "required_effects_contract.v1",
                "intent_class": "representation",
                "artefact_context": {
                    "urls": ["https://arxiv.org/abs/2501.00663"],
                },
            },
        },
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={"response_text": "Prepared from continuation context."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Download and represent the paper",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1858",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a prior session artefact.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a prior session artefact.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == "Prepared from continuation context."
    assert captured_data["selected_workflow_id"] == selected_workflow_id
    assert captured_data["source_uri"] == "https://arxiv.org/abs/2501.00663"
    assert captured_data["source_uris"] == ["https://arxiv.org/abs/2501.00663"]
    assert captured_data["arxiv_id"] == "2501.00663"
    assert captured_data["arxiv_ids"] == ["2501.00663"]
    assert captured_data["workflow_continuation_launch_inputs"] == {
        "source_uris": ["https://arxiv.org/abs/2501.00663"],
        "source_uri": "https://arxiv.org/abs/2501.00663",
        "arxiv_ids": ["2501.00663"],
        "arxiv_id": "2501.00663",
    }

    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == [
        "arxiv_id",
        "prompt",
        "source_uri",
    ]


def test_custom_workflow_dispatch_preserves_plural_launch_inputs_from_continuation_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="normalise_arxiv_source",
                states={
                    "normalise_arxiv_source": WorkflowStateSpec(
                        state_id="normalise_arxiv_source",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="arxiv.normalise_source"
                            ),
                        ),
                        terminal=True,
                    )
                },
                metadata={
                    "launch_input_contract": {
                        "schema_version": "workflow_launch_input_contract.v1",
                        "required_inputs": ["prompt"],
                        "input_mappings": [
                            {
                                "target_context_key": "prompt",
                                "source_expression": "inputs.prompt",
                                "required": True,
                            }
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Continuation-aware plural arXiv workflow dispatch test.",
            source="test",
        )
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1874-multi",
            "selected_workflow_id": selected_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "status": "not_executed",
                    "targets": [
                        "https://arxiv.org/abs/2501.00663",
                        "https://arxiv.org/abs/2501.00664",
                    ],
                }
            ],
            "required_effects_contract": {
                "schema_version": "required_effects_contract.v1",
                "intent_class": "representation",
                "artefact_context": {
                    "urls": [
                        "https://arxiv.org/abs/2501.00663",
                        "https://arxiv.org/abs/2501.00664",
                    ],
                },
            },
        },
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={"response_text": "Prepared from plural continuation context."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Download and represent the papers",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1874-multi",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent arXiv papers from prior session artefacts.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent arXiv papers from prior session artefacts.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == "Prepared from plural continuation context."
    assert "source_uri" not in captured_data
    assert "arxiv_id" not in captured_data
    assert captured_data["source_uris"] == [
        "https://arxiv.org/abs/2501.00663",
        "https://arxiv.org/abs/2501.00664",
    ]
    assert captured_data["arxiv_ids"] == ["2501.00663", "2501.00664"]
    assert captured_data["workflow_continuation_launch_inputs"] == {
        "source_uris": [
            "https://arxiv.org/abs/2501.00663",
            "https://arxiv.org/abs/2501.00664",
        ],
        "arxiv_ids": ["2501.00663", "2501.00664"],
    }


def test_selector_routes_failure_follow_up_with_episode_aware_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    diagnostic_workflow_id = "#V#missing_tool_call_workflow"

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1793",
            "active_workflow_episode_id": "wfep_1793",
            "active_workflow_source": "conversation_turn",
            "selected_workflow_id": diagnostic_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "completion_gate_decision_reason": "Diagnostic evidence retrieval failed.",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "workflow_required_effects_contract": {
                "contract_id": "conversation_diagnostics_required_evidence",
            },
            "resolved_contract_identifiers": {
                "workflow_required_effects_contract_id": (
                    "conversation_diagnostics_required_evidence"
                ),
                "selected_execution_mode": "tool_pipeline",
                "dispatch_workflow_id": "#V#tool_calling_workflow",
            },
            "unresolved_required_effects": [
                {
                    "effect_id": "conversation_history",
                    "effect_type": "diagnostic_evidence",
                    "status": "not_satisfied",
                    "status_reason": "Conversation evidence retrieval failed.",
                    "required_tools": ["chat_history_get_debug_entry"],
                }
            ],
        },
    )
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=diagnostic_workflow_id,
        purpose="Diagnostic follow-up workflow.",
    )

    class _SelectorContextSensitiveLLM:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        def generate(
            self,
            prompt: str,
            context: Optional[Sequence[Mapping[str, Any]]] = None,
            model=None,
        ):
            self.calls.append(
                {"prompt": prompt, "context": list(context or []), "model": model}
            )
            if isinstance(prompt, str) and prompt.startswith("Select workflow"):
                selector_prompt_text = "\n".join(
                    str(item.get("content") or "")
                    for item in (context or [])
                    if isinstance(item, Mapping)
                )
                if (
                    "ACTIVE WORKFLOW CONTINUATION CONTEXT" in selector_prompt_text
                    and "conversation_diagnostics_required_evidence"
                    in selector_prompt_text
                    and "Active workflow source: conversation_turn"
                    in selector_prompt_text
                ):
                    return diagnostic_workflow_id
                return CHAT_ASSISTANT_WORKFLOW_ID
            return "Diagnostic follow-up response."

    llm = _SelectorContextSensitiveLLM()

    result = orchestrator.run(
        prompt="Explain the failure from the telemetry",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1793",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": diagnostic_workflow_id,
                    "name": "Missing Tool Call Workflow",
                    "description": "Recover when tool emission failed.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "is_policy_safe": True,
                    "relevance_score": 0.95,
                    "confidence_score": 0.95,
                    "candidate_source": "workflow_discovery",
                    "candidate_reason": "discovered_workflow_candidate",
                }
            ]
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == diagnostic_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
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
    continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in continuation_context
    assert "conversation_diagnostics_required_evidence" in continuation_context
    assert "Active workflow source: conversation_turn" in continuation_context


def test_divergent_selection_keeps_continuation_context_but_not_launch_inputs(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1687",
            "selected_workflow_id": "#V#enrichment_workflow",
            "completion_gate_decision": "escalation_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_concept_verification_1",
                    "effect_type": "concept_verification",
                    "description": "Verify the concept relations.",
                    "required_tools": ["fetch_concept"],
                    "targets": ["#V#timothy_pistotti"],
                }
            ],
        },
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
            "I will inspect the concept directly instead.",
            "Follow-through response.",
            "Final response after tool workflow.",
        ]
    )

    result = orchestrator.run(
        prompt=(
            "The enrichment workflow isn't the right one. Manually retrieve "
            "#V#timothy_pistotti and inspect the concept. Do not run an "
            "existing workflow."
        ),
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1687",
    )

    # JVNAUTOSCI-2500: divergence is no longer a Python regex decision. The
    # continuation context still applies on workflow state and is presented to
    # the represented selector/planner stages, which own the divergence
    # judgement (here the selector chose the tool workflow). A divergent
    # selection must not inherit stale continuation launch inputs.
    assert result.workflow_routing is not None
    planner_context = llm.calls[1]["context"] or []
    planner_prompt_context = "\n".join(
        str(message.get("content") or "")
        for message in planner_context
        if isinstance(message, dict)
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in planner_prompt_context
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
    selector_continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" in selector_continuation_context

    continuation_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_continuation_context"
        ),
        None,
    )
    assert continuation_entry is not None
    assert continuation_entry.get("applied") is True
    assert continuation_entry.get("reason") == "workflow_state_authoritative"

    # The selector diverged to the tool workflow, so the continuation
    # context's concept targets must not be projected into its inputs.
    from src.backend.services.workflow_continuation_service import (
        project_launch_inputs_from_continuation_context,
    )

    divergent_projection = project_launch_inputs_from_continuation_context(
        {
            "selected_workflow_id": "#V#enrichment_workflow",
            "unresolved_required_effects": [
                {
                    "effect_type": "concept_verification",
                    "status": "not_executed",
                    "targets": ["#V#timothy_pistotti"],
                }
            ],
        },
        selected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
    )
    assert divergent_projection == {}


def test_tool_planner_skips_continuation_when_selected_workflow_is_not_executable(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-1718",
            "selected_workflow_id": "#V#arxiv_paper_representation_workflow",
            "selected_workflow_is_executable": False,
            "selected_workflow_executability_reason": "draft_not_published",
            "selected_workflow_executability_detail": (
                "workflow_not_published:phase=draft"
            ),
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "effect_workflow_execution_1",
                    "effect_type": "workflow_execution",
                    "description": (
                        "Obtain the selected workflow result needed for the user-facing answer."
                    ),
                }
            ],
        },
    )

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,
            "I will continue by inspecting the available workflow candidates.",
            "Follow-through response.",
            "Final response after tool workflow.",
        ]
    )

    result = orchestrator.run(
        prompt="Represent this paper https://arxiv.org/abs/2603.01896",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-1718",
    )

    assert result.workflow_routing is not None
    planner_context = llm.calls[1]["context"] or []
    planner_prompt_context = "\n".join(
        str(message.get("content") or "")
        for message in planner_context
        if isinstance(message, dict)
    )
    assert "ACTIVE WORKFLOW CONTINUATION CONTEXT" not in planner_prompt_context
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
    selector_continuation_context = (
        selector_prompt_entry.get("continuation_context", {}).get("text") or ""
    )
    assert "No active workflow continuation context." in selector_continuation_context

    continuation_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_continuation_context"
        ),
        None,
    )
    assert continuation_entry is not None
    assert continuation_entry.get("applied") is False
    assert continuation_entry.get("reason") == "selected_workflow_not_executable"
    assert (
        continuation_entry.get("context", {}).get(
            "selected_workflow_executability_reason"
        )
        == "draft_not_published"
    )


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing info on tool-calling path.
# ---------------------------------------------------------------------------
