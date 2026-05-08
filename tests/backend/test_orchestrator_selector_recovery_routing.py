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


def test_selector_unmatched_non_default_candidate_uses_safe_general_tool_fallback(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    excluded_workflow_id = "#V#specialised_vontology_search_workflow"
    executed_workflow_ids: list[str] = []
    recovery_requests: list[Mapping[str, Any]] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "final_response": "Executed via safe general tool workflow.",
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
            recovery_requests.append(dict(_kwargs))
            return SimpleNamespace(
                data={
                    "workflow_gap_final_response_text": (
                        "Recovered through workflow-gap analysis."
                    ),
                    "workflow_gap_final_extra_messages": [],
                    "workflow_gap_final_tool_invocations": [],
                    "workflow_gap_recovery_outcome": ("candidate_retried_successfully"),
                    "workflow_gap_candidate_workflow_id": excluded_workflow_id,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    llm = _CapturingLLM(
        [
            json.dumps(
                {
                    "workflow_id": excluded_workflow_id,
                    "confidence": 0.94,
                    "reasoning": (
                        "The specialised Vontology search workflow is the best fit "
                        "for this request."
                    ),
                }
            )
        ]
    )

    result = orchestrator.run(
        prompt="Find the represented student record and inspect the concept links.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "candidates": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "match_count": 1,
        },
    )

    assert executed_workflow_ids == [
        TOOL_CALLING_WORKFLOW_ID,
        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    ]
    assert result.response_text == "Recovered through workflow-gap analysis."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert (
        "safe general tool workflow"
        in (result.workflow_routing.reasoning or "").lower()
    )

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "default_workflow_fallback_unmatched_candidate"
    )
    assert (
        selector_entry["selection_metadata"]["unmatched_candidate_workflow_id"]
        == excluded_workflow_id
    )

    override_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selector_unmatched_candidate_requires_safe_general_fallback"
    )
    assert override_entry["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert override_entry["requested_candidate_workflow_id"] == excluded_workflow_id

    recovery_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_gap_recovery"
    )
    assert recovery_entry["status"] == "applied"
    assert recovery_entry["candidate_workflow_id"] == excluded_workflow_id
    assert recovery_requests
    assert (
        recovery_requests[0]["data"]["workflow_gap_base_response_text"]
        == "Executed via safe general tool workflow."
    )
    assert recovery_requests[0]["data"]["workflow_gap_selected_execution_mode"] == (
        "tool_pipeline"
    )



def test_selector_unmatched_budget_timeout_recovers_launchable_requested_workflow(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#entity_information_retrieval_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical grounded retrieval workflow for authenticated "
            "self-relative entity questions."
        ),
    )
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        data={
            "final_response": (
                "Recovered through the entity-information retrieval workflow."
            ),
            "tool_messages": [],
            "invocations": [],
        },
    )

    result = orchestrator.run(
        prompt="What research interests of mine are explicitly represented here?",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.98,
                        "reasoning": (
                            "This is an authenticated self-relative entity "
                            "information request."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "requested_query": (
                "What research interests of mine are explicitly represented here?"
            ),
            "query": "What research interests of mine are explicitly represented here?",
            "discovery_query_input": (
                "What research interests of mine are explicitly represented here?"
            ),
            "matches": [],
            "candidates": [],
            "routing_matches": [],
            "match_count": 0,
            "candidate_count": 0,
            "excluded_candidate_ids": [],
            "excluded_candidates": [],
            "budget_exhausted": True,
            "match_absence_reason": "workflow_discovery_budget_exhausted",
            "budget_exhaustion_stage": "workflow_discovery_for_turn",
            "budget_exhaustion_detail": (
                "workflow_discovery_for_turn timed out after 10.000s during "
                "workflow discovery"
            ),
        },
    )

    assert result.response_text == (
        "Recovered through the entity-information retrieval workflow."
    )
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector_override"

    recovery_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == (
            "selector_unmatched_candidate_budget_timeout_recovered_to_requested_workflow"
        )
    )
    assert recovery_entry["selected_workflow_id"] == selected_workflow_id
    assert recovery_entry["requested_candidate_workflow_id"] == selected_workflow_id
    assert recovery_entry["function"] == "_promote_selected_workflow_to_custom_dispatch"

    recovery_prepare_step = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_dispatch_prepare_step"
        and entry.get("step_id") == "selector_unmatched_candidate_recovery"
    )
    assert recovery_prepare_step["workflow_id"] == selected_workflow_id



def test_selector_safe_general_fallback_finalises_selection_experience_with_override_truth(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    excluded_workflow_id = "#V#specialised_vontology_search_workflow"
    captured_finalise: dict[str, Any] = {}

    monkeypatch.setattr(
        selection_experience_module,
        "record_selection_experience",
        lambda **_kwargs: SimpleNamespace(experience_id="exp-selector-override"),
    )

    def _capture_finalise(**kwargs: Any):
        captured_finalise.update(kwargs)
        return None

    monkeypatch.setattr(
        selection_experience_module,
        "finalise_selection_experience",
        _capture_finalise,
    )

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "final_response": "Executed via safe general tool workflow.",
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
            return SimpleNamespace(
                data={
                    "workflow_gap_final_response_text": (
                        "Recovered through workflow-gap analysis."
                    ),
                    "workflow_gap_final_extra_messages": [],
                    "workflow_gap_final_tool_invocations": [],
                    "workflow_gap_recovery_outcome": ("candidate_retried_successfully"),
                    "workflow_gap_candidate_workflow_id": excluded_workflow_id,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="Find the represented student record and inspect the concept links.",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": excluded_workflow_id,
                        "confidence": 0.94,
                        "reasoning": (
                            "The specialised Vontology search workflow is the best fit "
                            "for this request."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "candidates": [
                {
                    "concept_id": excluded_workflow_id,
                    "name": "Specialised Vontology Search Workflow",
                    "description": "Inspect represented concepts and relations.",
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.97,
                    "confidence_score": 0.97,
                }
            ],
            "match_count": 1,
        },
        turn_id="turn-selector-override",
    )

    assert result.response_text == "Recovered through workflow-gap analysis."
    assert captured_finalise["experience_id"] == "exp-selector-override"
    outcome_metadata = captured_finalise["outcome_metadata"]
    assert outcome_metadata["selected_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert (
        outcome_metadata["effective_dispatch_workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    )
    assert outcome_metadata["selector_override_applied"] is True
    assert outcome_metadata["workflow_gap_recovery_applied"] is True
    assert outcome_metadata["selector_override"]["reason"] == (
        "selector_unmatched_candidate_requires_safe_general_fallback"
    )
    assert outcome_metadata["selector_override"]["requested_candidate_workflow_id"] == (
        excluded_workflow_id
    )



def test_single_specialised_retrieval_candidate_recovers_inside_selector_boundary(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Retrieve the represented concept facts for the authenticated current "
            "user and answer from those facts."
        ),
    )
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        data={"response_text": "Retrieved current-user concept details."},
        passthrough_unmatched=True,
    )

    result = orchestrator.run(
        prompt="Tell me about myself.",
        context=[],
        llm_client=_CapturingLLM(
            [
                (
                    "I'm not sure which workflow you would like me to select.\n\n"
                    "Are you looking to search for information or manage tasks?"
                ),
                "Retrieved current-user concept details.",
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": (
                        "Retrieve represented facts about a specific concept or "
                        "instance from the Vontology."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "retrieval"},
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": (
                        "Retrieve represented facts about a specific concept or "
                        "instance from the Vontology."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "retrieval"},
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Retrieved current-user concept details."
    assert (
        "only eligible specialised candidate already present"
        in (result.workflow_routing.reasoning or "").lower()
    )

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == selected_workflow_id
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "single_specialised_candidate_recovery_from_selector_fallback"
    )
    assert selector_entry["selection_metadata"]["selection_resolution_prior"] == (
        "default_workflow_fallback"
    )
    assert selector_entry["selection_metadata"]["recovered_candidate_workflow_id"] == (
        selected_workflow_id
    )
    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector_override" not in aux_types



def test_generic_tool_fallback_records_disqualifying_reason_for_specialised_candidate(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"

    result = orchestrator.run(
        prompt="Download and represent 2603.19312v1 arxiv",
        context=[],
        llm_client=_CapturingLLM(
            [TOOL_CALLING_WORKFLOW_ID, "Fallback via generic tool pipeline."]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Canonical arXiv representation workflow.",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": False,
                    "executability_reason": "launch_input_contract_unsatisfied",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Canonical arXiv representation workflow.",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": False,
                    "executability_reason": "launch_input_contract_unsatisfied",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    reasoning = str(selector_entry.get("reasoning") or "")
    assert "#V#arxiv_paper_representation_workflow" in reasoning
    assert "launch input contract unsatisfied" in reasoning



def test_discovery_miss_invokes_gap_recovery_after_plain_fallback(monkeypatch):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    recovery_calls: list[tuple[str, Mapping[str, Any]]] = []

    def _fake_execute_workflow(workflow_id: str, **kwargs):
        recovery_calls.append((workflow_id, dict(kwargs)))
        return SimpleNamespace(
            data={
                "workflow_gap_final_response_text": "Recovered through workflow-gap analysis.",
                "workflow_gap_final_extra_messages": [
                    {"role": "tool", "content": "gap recovery tool output"}
                ],
                "workflow_gap_final_tool_invocations": [
                    {"tool": "workflow_gap.execute_candidate"}
                ],
                "workflow_gap_recovery_outcome": "candidate_retried_successfully",
                "workflow_gap_candidate_workflow_id": "#V#candidate_recovery_workflow",
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    llm = _CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Fallback response."])
    result = orchestrator.run(
        prompt="Handle this missing workflow.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={"matches": [], "candidates": []},
    )

    assert recovery_calls
    workflow_id, payload = recovery_calls[0]
    assert workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    assert payload["data"]["workflow_gap_base_response_text"] == "Fallback response."
    assert result.response_text == "Recovered through workflow-gap analysis."
    assert result.extra_messages == (
        {"role": "tool", "content": "gap recovery tool output"},
    )
    assert result.tool_invocations == ({"tool": "workflow_gap.execute_candidate"},)
    recovery_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_gap_recovery"
        ),
        None,
    )
    assert recovery_entry is not None
    assert recovery_entry.get("status") == "applied"
    assert (
        recovery_entry.get("candidate_workflow_id") == "#V#candidate_recovery_workflow"
    )



def test_discovered_custom_workflow_failure_falls_through_to_tool_pipeline_before_gap_recovery(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#misaligned_specialised_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Misaligned specialised workflow for gap-recovery routing tests.",
    )

    executed_workflow_ids: list[str] = []
    tool_pipeline_payload: dict[str, Any] = {}

    def _fake_execute_workflow(workflow_id: str, **kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={"response_text": "Selected specialised workflow failed."},
                final_state="#V#workflow_step_misaligned_specialised_workflow_failed",
                completed=True,
                error=None,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": "Recovered through the general tool workflow.",
                    "tool_messages": [
                        {
                            "role": "tool",
                            "content": "general tool workflow inspected existing context",
                        }
                    ],
                    "invocations": [{"tool": "find_relations_with_argument"}],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt="Represent these people in the Vontology if they are not already represented.",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Misaligned specialised workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Misaligned specialised workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert executed_workflow_ids == [
        selected_workflow_id,
        TOOL_CALLING_WORKFLOW_ID,
    ]
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "#V#workflow_step_misaligned_specialised_workflow_failed",
        "user_visible_failure_text": "Selected specialised workflow failed.",
    }
    assert result.response_text == "Recovered through the general tool workflow."
    assert result.extra_messages == (
        {
            "role": "tool",
            "content": "general tool workflow inspected existing context",
        },
    )
    assert result.tool_invocations == ({"tool": "find_relations_with_argument"},)
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
    assert terminal_boundary.get("completed") is False
    assert terminal_boundary.get("final_state") == (
        "#V#workflow_step_misaligned_specialised_workflow_failed"
    )
    assert terminal_boundary.get("reason") == "failed_terminal_state"
    assert terminal_boundary.get("detail") == ("Selected specialised workflow failed.")
    assert terminal_boundary.get("continued_to_tool_pipeline") is True
    assert (
        terminal_boundary.get("fallback_tool_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    )
    handoff_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_recovery_handoff"
        ),
        None,
    )
    assert handoff_entry is not None
    assert handoff_entry.get("to_execution_mode") == "tool_pipeline"
    assert handoff_entry.get("reason") == "failed_custom_workflow_before_tool_progress"
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_gap_recovery"
        for entry in result.aux_llm_calls
    )



def test_entity_representation_failure_family_replays_with_truthful_gap_recovery_after_tool_pipeline_attempt(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that represents scholarly papers, "
            "not conversational person lists."
        ),
    )

    executed_workflow_ids: list[str] = []
    recovery_calls: list[tuple[str, Mapping[str, Any]]] = []
    tool_pipeline_payload: dict[str, Any] = {}

    def _fake_execute_workflow(workflow_id: str, **kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={
                    "response_text": (
                        "ArXiv paper representation could not represent those people."
                    ),
                    "error": "arxiv_identifier_missing",
                },
                final_state="#V#workflow_step_arxiv_paper_representation_workflow_failed",
                completed=True,
                error=None,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": (
                        "The general tool workflow still could not complete the turn."
                    ),
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 1,
                },
                final_state="#V#tool_calling_workflow_failed",
                completed=True,
                error=None,
            )
        if workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID:
            recovery_calls.append((workflow_id, dict(kwargs)))
            return SimpleNamespace(
                data={
                    "workflow_gap_final_response_text": (
                        "Recovered by escalating into entity-representation workflow authoring."
                    ),
                    "workflow_gap_final_extra_messages": [
                        {
                            "role": "tool",
                            "content": "workflow-gap recovery prepared entity workflow follow-up",
                        }
                    ],
                    "workflow_gap_final_tool_invocations": [
                        {"tool": "workflow_gap.execute_candidate"}
                    ],
                    "workflow_gap_recovery_outcome": "candidate_retried_successfully",
                    "workflow_gap_candidate_workflow_id": (
                        "#V#entity_representation_workflow"
                    ),
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt=(
            "OK, now for each of those students, if they aren't already represented "
            "in the Vontology, please represent them."
        ),
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert executed_workflow_ids == [
        selected_workflow_id,
        TOOL_CALLING_WORKFLOW_ID,
        WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    ]
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "#V#workflow_step_arxiv_paper_representation_workflow_failed",
        "operational_error": "arxiv_identifier_missing",
        "user_visible_failure_text": (
            "ArXiv paper representation could not represent those people."
        ),
    }
    assert recovery_calls
    workflow_id, payload = recovery_calls[0]
    assert workflow_id == WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID
    assert payload["data"]["workflow_gap_trigger_reason"] == (
        "tool_pipeline_failed_after_custom_workflow_failure"
    )
    assert payload["data"]["workflow_gap_selected_workflow_final_state"] == (
        "#V#tool_calling_workflow_failed"
    )
    assert payload["data"]["workflow_gap_selected_execution_mode"] == "tool_pipeline"
    assert payload["data"]["workflow_gap_prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": False,
        "tool_progress_detected": False,
        "final_state": "#V#workflow_step_arxiv_paper_representation_workflow_failed",
        "operational_error": "arxiv_identifier_missing",
        "user_visible_failure_text": (
            "ArXiv paper representation could not represent those people."
        ),
    }
    assert result.response_text == (
        "Recovered by escalating into entity-representation workflow authoring."
    )
    assert result.extra_messages == (
        {
            "role": "tool",
            "content": "workflow-gap recovery prepared entity workflow follow-up",
        },
    )
    terminal_boundary = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_boundary"
            and entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("completed") is False
    assert terminal_boundary.get("reason") == "failed_terminal_state"
    assert terminal_boundary.get("detail") == "arxiv_identifier_missing"
    assert terminal_boundary.get("continued_to_tool_pipeline") is True



def test_custom_workflow_failure_prefers_explicit_action_error_over_metadata_summary(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Canonical arXiv wrapper workflow for explicit failure-detail tests.",
    )

    actionable_error = (
        "Unexpected error: Failed to store PDF in blob store: Blob store "
        "initialisation failed: OpenStack Swift backend requires 'openstacksdk' "
        "in the active runtime environment."
    )
    masked_summary = (
        "The ability to predict future outcomes given control actions is "
        "fundamental for physical reasoning."
    )

    def _fake_execute_workflow(workflow_id: str, **_kwargs: Any):
        if workflow_id != selected_workflow_id:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "summary": masked_summary,
                "last_action_error": actionable_error,
                "workflow_step_result_envelopes": [
                    {
                        "schema_version": "workflow_step_result_envelope.v1",
                        "workflow_id": selected_workflow_id,
                        "state_id": (
                            "#V#workflow_step_arxiv_paper_representation_workflow_"
                            "download_or_finalise"
                        ),
                        "action_id": "download_paper",
                        "action_status": "failed",
                        "action_outcome": "failure",
                        "diagnostics": {"error": actionable_error},
                        "output_payload": {},
                    }
                ],
            },
            final_state="#V#workflow_step_arxiv_paper_representation_workflow_failed",
            completed=True,
            error=None,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2411.04983",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_gap_recovery_enabled=False,
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert result.response_text == actionable_error
    terminal_boundary = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_boundary"
            and entry.get("boundary") == "workflow_terminal"
            and entry.get("selected_execution_mode") == "custom_workflow"
        ),
        None,
    )
    assert terminal_boundary is not None
    assert terminal_boundary.get("status") == "failed"
    assert terminal_boundary.get("detail") == actionable_error
    assert terminal_boundary.get("detail") != masked_summary



def test_custom_workflow_result_preserves_messages_and_invocations(monkeypatch):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#custom_gap_analysis_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Custom gap-analysis workflow for selector dispatch tests.",
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={
            "response_text": "Custom workflow response.",
            "extra_messages": [{"role": "tool", "content": "custom output"}],
            "tool_invocations": [{"tool": "search_concepts"}],
        },
    )

    result = orchestrator.run(
        prompt="Use the discovered workflow.",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
        },
    )

    assert result.response_text == "Custom workflow response."
    assert result.extra_messages == ({"role": "tool", "content": "custom output"},)
    assert result.tool_invocations == ({"tool": "search_concepts"},)

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert dispatch_boundaries[-1].get("boundary") == "workflow_terminal"
    assert dispatch_boundaries[-1].get("selected_execution_mode") == "custom_workflow"
    assert dispatch_boundaries[-1].get("dispatch_workflow_id") == selected_workflow_id



def test_custom_workflow_structured_result_renders_verdict_evidence_and_promotion(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#custom_gap_analysis_workflow"
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Structured-result workflow for selector dispatch tests.",
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={
            "response_text": json.dumps(
                [
                    {
                        "label": "meeting_type_classification",
                        "verdict": "pass",
                        "expected_outcome": "project_meeting",
                        "observed_outcome": "project_meeting",
                    }
                ]
            ),
            "run_id": "#V#run_meeting_test",
            "verdict": "pass",
            "verdict_summary": {
                "reason": "all_recorded_observations_passed",
            },
            "promotion_recommendation": {
                "recommended": True,
                "requires_promotion_gate": True,
                "reason": "explicit_promotion_gate_required",
            },
            "candidate_meeting_type": "project_meeting",
            "candidate_safe_downstream_action": "draft_calendar_entry",
            "meeting_candidate_observations": [
                {
                    "label": "meeting_type_classification",
                    "verdict": "pass",
                    "expected_outcome": "project_meeting",
                    "observed_outcome": "project_meeting",
                },
                {
                    "label": "structured_meeting_fields",
                    "verdict": "pass",
                    "expected_outcome": "title,time,participants",
                    "observed_outcome": "all expected fields present",
                },
            ],
        },
    )

    result = orchestrator.run(
        prompt="Use the discovered workflow.",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Custom gap workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
    )

    assert "Workflow verdict: pass." in result.response_text
    assert "Experiment run: #V#run_meeting_test." in result.response_text
    assert (
        "Promotion recommendation: recommended; promotion gate required; "
        "explicit_promotion_gate_required."
    ) in result.response_text
    assert "Candidate meeting type: project_meeting." in result.response_text
    assert (
        "Candidate safe downstream action: draft_calendar_entry."
    ) in result.response_text
    assert "Evidence:" in result.response_text
    assert (
        "- meeting_type_classification: pass (expected: project_meeting; "
        "observed: project_meeting)"
    ) in result.response_text
    assert (
        "- structured_meeting_fields: pass (expected: title,time,participants; "
        "observed: all expected fields present)"
    ) in result.response_text

    execution_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is not None
    result_snapshot = execution_entry.get("result_snapshot")
    assert isinstance(result_snapshot, dict)
    assert result_snapshot.get("run_id") == "#V#run_meeting_test"
    assert result_snapshot.get("verdict") == "pass"
    assert isinstance(result_snapshot.get("verdict_summary"), dict)
    assert isinstance(result_snapshot.get("promotion_recommendation"), dict)
    assert isinstance(result_snapshot.get("observations"), list)
