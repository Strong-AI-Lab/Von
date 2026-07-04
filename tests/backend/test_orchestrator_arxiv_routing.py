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
    _reset_mock_db,
    _stub_execute_workflow_result,
)


def test_explicit_arxiv_representation_request_selects_representation_workflow_over_generic_tool_calling(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={"response_text": "Executed via arXiv representation workflow."},
    )

    result = orchestrator.run(
        prompt="Download and represent 2603.19312v1 arxiv",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.97,
                        "reasoning": (
                            "The request explicitly asks to download and represent "
                            "an arXiv paper, and the specialised arXiv "
                            "representation workflow is executable."
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
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Executed via arXiv representation workflow."

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    candidate_list_text = str(
        selector_entry.get("candidate_list", {}).get("text") or ""
    )
    assert selected_workflow_id in candidate_list_text
    assert TOOL_CALLING_WORKFLOW_ID in candidate_list_text
    assert "relevance 100%" in candidate_list_text
    assert "confidence 100%" in candidate_list_text
    assert "routing eligible" in candidate_list_text
    assert "executable" in candidate_list_text
    assert selector_entry.get("workflow_id") == selected_workflow_id



def test_bare_arxiv_url_with_authoritative_definition_stays_launchable(
    _reset_mock_db: Any,
    monkeypatch,
):
    bootstrap_canonical_paper_representation_workflows()
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    authoritative_definition = load_workflow_definition_from_vontology(
        selected_workflow_id
    )
    assert authoritative_definition is not None
    assert authoritative_definition.metadata.get("launch_contract") == {
        "schema_version": "launch_contract.v1",
        "preconditions": [
            {
                "type": "context_key_present",
                "key": "prompt",
                "required": True,
            }
        ],
    }
    assert authoritative_definition.metadata.get("launch_contract_source") == (
        "text_relation:#V#has_launch_contract"
    )
    assert authoritative_definition.metadata.get("launch_input_contract", {}).get(
        "required_inputs"
    ) == ["prompt"]

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=authoritative_definition,
            purpose=(
                "Canonical arXiv wrapper workflow that normalises an arXiv source, "
                "fetches authoritative metadata, and delegates to scholarly-paper "
                "representation."
            ),
            source="authoritative_vontology_test",
        )
    )

    executed_workflow_ids: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        executed_workflow_ids.append(workflow_id)
        if workflow_id != selected_workflow_id:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "response_text": "Executed via authoritative arXiv representation workflow."
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2411.04983",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.99,
                        "reasoning": (
                            "A bare arXiv URL should use the specialised arXiv "
                            "representation workflow, and the authoritative launch "
                            "metadata is satisfied by the prompt."
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
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, and delegates to "
                        "scholarly-paper representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, and delegates to "
                        "scholarly-paper representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == (
        "Executed via authoritative arXiv representation workflow."
    )
    assert executed_workflow_ids == [selected_workflow_id]
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_launchability_requires_safe_general_fallback"
        for entry in result.aux_llm_calls
    )



def test_bare_arxiv_url_excludes_testing_workflow_before_selector_and_routes_to_representation_workflow(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
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
                        "explicit_workflow_context_required": True,
                        "prefer_existing_capability": False,
                    }
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

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={"response_text": "Executed via arXiv representation workflow."},
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2411.04983",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 1.0,
                        "reasoning": (
                            "The request is a bare arXiv URL, so the canonical "
                            "execution workflow for arXiv representation is the "
                            "best executable route."
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
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
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
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
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
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-bare-arxiv-routing-regression",
        turn_id="turn-bare-arxiv-routing-regression",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Executed via arXiv representation workflow."

    selector_prompt_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    candidate_list_text = str(
        selector_prompt_entry.get("candidate_list", {}).get("text") or ""
    )
    assert selected_workflow_id in candidate_list_text
    assert testing_workflow_id not in candidate_list_text

    excluded_candidates = selector_prompt_entry.get("discovery_excluded_candidates")
    assert isinstance(excluded_candidates, list)
    excluded_testing = next(
        item
        for item in excluded_candidates
        if isinstance(item, dict) and item.get("concept_id") == testing_workflow_id
    )
    assert excluded_testing.get("routing_profile_role") == "maintenance"
    assert (
        excluded_testing.get("routing_exclusion_reason")
        == "explicit_workflow_context_required_by_workflow_profile"
    )
    assert (
        excluded_testing.get("routing_policy_flags", {}).get(
            "explicit_workflow_context_required"
        )
        is True
    )



def test_bare_arxiv_url_selector_default_recovers_to_single_discovered_execution_workflow(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_representation_workflow"
    testing_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose=(
            "Canonical arXiv wrapper workflow that normalises an arXiv source, "
            "fetches authoritative metadata, and delegates to scholarly-paper "
            "representation."
        ),
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
                        "explicit_workflow_context_required": True,
                        "prefer_existing_capability": False,
                    }
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

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        final_state="complete",
        data={"response_text": "Executed via recovered arXiv representation workflow."},
    )

    result = orchestrator.run(
        prompt="https://arxiv.org/abs/2501.00663",
        context=[],
        llm_client=_CapturingLLM(
            [
                (
                    "I'm not sure which workflow you'd like me to select from "
                    "the candidates provided. If you want me to download or "
                    "represent the paper, please say so explicitly."
                )
            ]
        ),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "execution"},
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
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": (
                        "Canonical arXiv wrapper workflow that normalises an arXiv "
                        "source, fetches authoritative metadata, acquires or "
                        "finalises the paper artefact, delegates to the scholarly-"
                        "paper workflow, and fails closed on incomplete "
                        "representation."
                    ),
                    "match_source": "capability_index",
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "routing_profile": {"role": "execution"},
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
                    "match_source": "capability_index",
                    "confidence_score": 0.84,
                    "relevance_score": 0.77,
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                },
            ],
            "match_count": 2,
        },
        conversation_session_id="session-bare-arxiv-selector-default-recovery",
        turn_id="turn-bare-arxiv-selector-default-recovery",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert (
        result.response_text == "Executed via recovered arXiv representation workflow."
    )
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
    candidate_entries = selector_entry.get("candidate_entries") or []
    candidate_order = [
        str(item.get("concept_id"))
        for item in candidate_entries
        if isinstance(item, dict) and isinstance(item.get("concept_id"), str)
    ]
    assert candidate_order.index(selected_workflow_id) < candidate_order.index(
        CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert selector_entry["selection_metadata"]["recovered_candidate_workflow_id"] == (
        selected_workflow_id
    )
    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector_override" not in aux_types



def test_custom_workflow_dispatch_resolves_deictic_arxiv_target_from_discovery_context(
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
                                "target_context_key": "arxiv_id",
                                "source_expression": "inputs.arxiv_id",
                            },
                            {
                                "target_context_key": "arxiv_id",
                                "source_expression": (
                                    "inputs.workflow_discovery_result.discovery_query_input"
                                ),
                                "extractor": "arxiv_id",
                            },
                        ],
                    },
                    "launch_input_contract_source": "test_contract",
                },
            ),
            purpose="Deictic arXiv workflow dispatch test.",
            source="test",
        )
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        if getattr(_workflow_def, "workflow_id", None) == selected_workflow_id:
            captured_data.clear()
            captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={"response_text": "Prepared from grounded discovery context."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="represent the first one",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-deictic-arxiv",
        workflow_discovery_result={
            "discovery_query_input": (
                "represent the first one\n\n"
                "Success target: Represent the first arXiv paper from the "
                "immediately preceding list, i.e. arXiv:2604.04604."
            ),
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a grounded target.",
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
                    "description": "Represent an arXiv paper from a grounded target.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == "Prepared from grounded discovery context."
    assert captured_data["selected_workflow_id"] == selected_workflow_id
    assert captured_data["arxiv_id"] == "2604.04604"
    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == ["arxiv_id", "prompt"]



def test_authoritative_arxiv_workflow_dispatch_resolves_deictic_grounded_target(
    _reset_mock_db: Any,
    monkeypatch,
):
    bootstrap_canonical_paper_representation_workflows()
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    authoritative_definition = load_workflow_definition_from_vontology(
        selected_workflow_id
    )
    assert authoritative_definition is not None
    launch_contract = authoritative_definition.metadata.get("launch_input_contract")
    assert isinstance(launch_contract, dict)
    arxiv_launch_sources = {
        mapping.get("source_expression")
        for mapping in launch_contract.get("input_mappings") or []
        if mapping.get("target_context_key") == "arxiv_id"
    }
    assert "inputs.workflow_discovery_result.discovery_query_input" in (
        arxiv_launch_sources
    )

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=authoritative_definition,
            purpose="Authoritative deictic arXiv workflow dispatch test.",
            source="authoritative_vontology_test",
        )
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        if getattr(_workflow_def, "workflow_id", None) == selected_workflow_id:
            captured_data.clear()
            captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={
                "response_text": (
                    "Prepared from authoritative grounded discovery context."
                )
            },
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="represent the first one",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-authoritative-deictic-arxiv",
        workflow_discovery_result={
            "discovery_query_input": (
                "represent the first one\n\n"
                "Turn-intent routing guidance:\n"
                "- Success target: Represent the first arXiv paper from the "
                "immediately preceding Zhan-email arXiv list, i.e. arXiv:2604.04604."
            ),
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a grounded target.",
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
                    "description": "Represent an arXiv paper from a grounded target.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == (
        "Prepared from authoritative grounded discovery context."
    )
    assert captured_data["selected_workflow_id"] == selected_workflow_id
    assert captured_data["arxiv_id"] == "2604.04604"
    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("contract_source") == (
        "text_relation:#V#hasWorkflowLaunchInputContractJson"
    )
    assert launch_resolution.get("status") == "resolved"
    assert launch_resolution.get("resolved_inputs") == [
        "arxiv_id",
        "arxiv_ids",
        "prompt",
    ]
    assert any(
        mapping.get("target_context_key") == "arxiv_id"
        and mapping.get("source_expression")
        == "inputs.workflow_discovery_result.discovery_query_input"
        and mapping.get("resolved") is True
        for mapping in launch_resolution.get("mappings") or []
    )


def test_authoritative_arxiv_workflow_dispatch_ignores_stale_ambient_target_when_prompt_names_new_target(
    _reset_mock_db: Any,
    monkeypatch,
):
    bootstrap_canonical_paper_representation_workflows()
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID

    authoritative_definition = load_workflow_definition_from_vontology(
        selected_workflow_id
    )
    assert authoritative_definition is not None
    launch_contract = authoritative_definition.metadata.get("launch_input_contract")
    assert isinstance(launch_contract, dict)
    assert "arxiv_id" in launch_contract.get("excluded_ambient_input_keys", [])
    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=authoritative_definition,
            purpose="Authoritative arXiv stale-context handoff regression test.",
            source="authoritative_vontology_test",
        )
    )

    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: {
            "session_id": "session-stale-arxiv-context",
            "selected_workflow_id": selected_workflow_id,
            "completion_gate_decision": "follow_up_required",
            "requires_follow_up": True,
            "safe_to_claim_completion": False,
            "has_unresolved_required_effects": True,
            "unresolved_required_effects": [
                {
                    "effect_id": "arxiv_paper_representation",
                    "effect_type": "scholarly_representation",
                    "status": "not_satisfied",
                    "targets": ["2406.15341"],
                }
            ],
            "required_effects_contract": {
                "schema_version": "workflow_required_effects_contract.v1",
                "required_effects": [
                    {
                        "effect_id": "arxiv_paper_representation",
                        "targets": ["2406.15341"],
                    }
                ],
            },
        },
    )

    captured_data: dict[str, Any] = {}

    def _run_workflow(_workflow_def: Any, *, data: Mapping[str, Any], **_kwargs: Any):
        if getattr(_workflow_def, "workflow_id", None) == selected_workflow_id:
            captured_data.clear()
            captured_data.update(dict(data))
        return SimpleNamespace(
            completed=True,
            final_state="normalise_arxiv_source",
            error=None,
            data={"response_text": "Prepared from current prompt target."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Is this paper represented? https://arxiv.org/pdf/2603.22519v2",
        context=[],
        llm_client=_CapturingLLM([selected_workflow_id]),
        model=None,
        user_namespace="#V#user",
        conversation_session_id="session-stale-arxiv-context",
        workflow_discovery_result={
            "discovery_query_input": (
                "Is this paper represented? https://arxiv.org/pdf/2603.22519v2"
            ),
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Representation Workflow",
                    "description": "Represent an arXiv paper from a current prompt URL.",
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
                    "description": "Represent an arXiv paper from a current prompt URL.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                }
            ],
            "match_count": 1,
        },
    )

    assert result.response_text == "Prepared from current prompt target."
    assert captured_data["selected_workflow_id"] == selected_workflow_id
    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert captured_data["arxiv_id"] == "2603.22519", {
        "excluded_ambient_inputs_applied": (
            launch_resolution.get("excluded_ambient_inputs_applied")
            if isinstance(launch_resolution, dict)
            else None
        ),
        "arxiv_mappings": [
            mapping
            for mapping in (
                launch_resolution.get("mappings", [])
                if isinstance(launch_resolution, dict)
                else []
            )
            if isinstance(mapping, dict)
            and mapping.get("target_context_key") in {"arxiv_id", "arxiv_ids"}
        ],
    }
    assert captured_data["arxiv_ids"] == ["2603.22519"]
    assert captured_data.get("source_uri") != "https://arxiv.org/abs/2406.15341"
    launch_resolution = captured_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("status") == "resolved"
    assert "arxiv_id" in launch_resolution.get("excluded_ambient_inputs_applied", [])
    assert any(
        mapping.get("target_context_key") == "arxiv_id"
        and mapping.get("source_expression")
        == "inputs.workflow_discovery_result.discovery_query_input"
        and mapping.get("resolved") is True
        for mapping in launch_resolution.get("mappings") or []
    )
