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


def test_launchable_custom_workflow_is_not_python_overridden_from_mutative_wording(
    monkeypatch,
):
    """Discovered custom workflows must not be promoted from mutative wording alone."""

    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#launchable_mutative_custom_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(action_id="tool.prepare_custom"),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=(
                "Create concept links in Vontology via a specialised "
                "relationship editing workflow."
            ),
            source="test",
        )
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
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
            data={"response_text": "Executed via specialised workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launchable mutative custom workflow",
                    "description": (
                        "Create concept links in Vontology via a specialised "
                        "relationship editing workflow."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.82,
                    "confidence_score": 0.82,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Launchable mutative custom workflow",
                    "description": (
                        "Create concept links in Vontology via a specialised "
                        "relationship editing workflow."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.82,
                    "confidence_score": 0.82,
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="session-mutative-custom",
        turn_id="turn-mutative-custom",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert captured_execution == {}
    assert not any(
        isinstance(entry, dict)
        and entry.get("type")
        in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )



def test_unrelated_execution_workflow_is_not_python_declined_or_promoted_from_prompt_text(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    unrelated_workflow_id = "#V#sail_phd_student_onboarding_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=unrelated_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=unrelated_workflow_id,
                initial_state="collect_student_info",
                states={
                    "collect_student_info": WorkflowStateSpec(
                        state_id="collect_student_info",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="tool.prepare_student_onboarding"
                            ),
                        ),
                        terminal=True,
                    )
                },
            ),
            purpose=(
                "Onboard new PhD students into SAIL by collecting student "
                "details and setting up onboarding tasks."
            ),
            source="test",
        )
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
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
            final_state="completed",
            error=None,
            data={"response_text": "Tool pipeline executed instead."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt=(
            "OK, taking into account the way papers are represented at the moment, "
            "think about papers under preparation. How should they be represented. "
            "What is common between them and published (or rejected papers) and what "
            "is unique to the under-preparation status. Are any ontological edits "
            "needed. If so, list the new types and relations needed."
        ),
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": unrelated_workflow_id,
                    "name": "Sail Phd Student Onboarding Workflow",
                    "description": (
                        "Onboarding workflow for new PhD students joining the "
                        "SAIL research group."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                }
            ],
            "candidates": [
                {
                    "concept_id": unrelated_workflow_id,
                    "name": "Sail Phd Student Onboarding Workflow",
                    "description": (
                        "Onboarding workflow for new PhD students joining the "
                        "SAIL research group."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 1.0,
                    "confidence_score": 1.0,
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="session-paper-representation-routing",
        turn_id="turn-paper-representation-routing",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert execute_calls == []
    assert not any(
        isinstance(entry, dict)
        and entry.get("type")
        in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )



def test_authoring_workflow_query_is_left_to_selector_without_python_semantic_override(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    authoring_workflow_id = "#V#launchable_authoring_workflow"

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

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
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
            data={"response_text": "Executed via workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    result = orchestrator.run(
        prompt="Is there already a workflow for creating a meeting instance?",
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ],
            "candidates": [
                {
                    "concept_id": authoring_workflow_id,
                    "name": "Workflow creation workflow",
                    "description": (
                        "Create and verify executable workflows from a "
                        "workflow description request."
                    ),
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ],
            "match_count": 1,
        },
        conversation_session_id="session-mutative-authoring-decline",
        turn_id="turn-mutative-authoring-decline",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert execute_calls == []
    assert not any(
        isinstance(entry, dict)
        and entry.get("type")
        in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )



def test_prepare_selector_discovered_matches_preserves_discovery_exclusion_reason(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#search_concept_and_instances_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(),
                        terminal=True,
                    )
                },
            ),
            purpose="Inspect concepts and their instances.",
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Search Concept And Instances Workflow",
                    "description": "Inspect concepts and their instances.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.91,
                    "confidence_score": 0.91,
                    "is_policy_safe": False,
                    "routing_eligible": False,
                    "routing_exclusion_reason": "missing_authoritative_purpose",
                }
            ]
        },
        turn_text="Look up #V#timothy_pistotti and inspect the concept relations.",
    )

    assert included == []
    assert len(excluded) == 1
    assert excluded[0]["routing_eligible"] is False
    assert excluded[0]["routing_exclusion_reason"] == "missing_authoritative_purpose"
    assert excluded[0]["candidate_reason"] == "discovered_workflow_excluded"



def test_prepare_selector_discovered_matches_excludes_authoring_profile_without_explicit_authoring_request(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#launchable_authoring_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
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
            purpose="Create and verify executable workflows from a workflow description request.",
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Workflow creation workflow",
                    "description": "Create and verify executable workflows from a workflow description request.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ]
        },
        turn_text=(
            "The enrichment workflow isn't the right one. We need a new "
            "search workflow eventually, but manually retrieve "
            "#V#timothy_pistotti first and do not run an existing workflow."
        ),
    )

    assert included == []
    assert len(excluded) == 1
    assert excluded[0]["routing_profile"]["role"] == "authoring"
    assert (
        excluded[0]["routing_exclusion_reason"]
        == "authoring_intent_required_by_workflow_profile"
    )
    assert excluded[0]["routing_profile_role"] == "authoring"
    assert (
        excluded[0]["routing_policy_lexical_signals"]["explicit_authoring_request"]
        is False
    )



def test_prepare_selector_discovered_matches_allows_authoring_profile_for_explicit_authoring_request(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#launchable_authoring_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
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
            purpose="Create and verify executable workflows from a workflow description request.",
            source="test",
        )
    )

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Workflow creation workflow",
                    "description": "Create and verify executable workflows from a workflow description request.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.93,
                    "confidence_score": 0.93,
                }
            ]
        },
        turn_text="Create a new workflow to inspect PhD supervision relations.",
    )

    assert excluded == []
    assert len(included) == 1
    assert included[0]["routing_profile"]["role"] == "authoring"
    assert included[0]["routing_eligible"] is True
    assert included[0]["routing_profile_role"] == "authoring"
    assert (
        included[0]["routing_policy_lexical_signals"]["explicit_authoring_request"]
        is True
    )



def test_prepare_selector_discovered_matches_excludes_maintenance_profile_without_explicit_workflow_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
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

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": "Run the canonical arXiv ingestion workflow as a test.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.77,
                    "confidence_score": 0.84,
                }
            ]
        },
        turn_text="https://arxiv.org/abs/2411.04983",
    )

    assert included == []
    assert len(excluded) == 1
    assert excluded[0]["routing_profile"]["role"] == "testing"
    assert excluded[0]["routing_profile"]["explicit_workflow_context_required"] is True
    assert (
        excluded[0]["routing_exclusion_reason"]
        == "explicit_workflow_context_required_by_workflow_profile"
    )
    assert excluded[0]["routing_profile_role"] == "maintenance"
    assert (
        excluded[0]["routing_policy_lexical_signals"]["workflow_query_intent"] is False
    )



def test_prepare_selector_discovered_matches_allows_maintenance_profile_for_explicit_workflow_context(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=workflow_id,
            definition=WorkflowDefinition(
                workflow_id=workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
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

    included, excluded = orchestrator._prepare_selector_discovered_matches(
        {
            "candidates": [
                {
                    "concept_id": workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": "Run the canonical arXiv ingestion workflow as a test.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ]
        },
        turn_text=(
            "Run the arXiv paper ingestion testing workflow on "
            "https://arxiv.org/abs/2411.04983 and verify the workflow result."
        ),
    )

    assert excluded == []
    assert len(included) == 1
    assert included[0]["routing_profile"]["role"] == "testing"
    assert included[0]["routing_profile"]["explicit_workflow_context_required"] is True
    assert included[0]["routing_eligible"] is True
    assert (
        included[0]["routing_policy_lexical_signals"]["workflow_query_intent"] is True
    )



def test_explicit_workflow_prompt_is_not_python_reinterpreted_into_custom_dispatch(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#arxiv_paper_ingestion_testing_workflow"

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"download_paper": {"category": "read"}},
    )

    orchestrator._workflow_registry.register_or_replace(
        WorkflowRegistration(
            workflow_id=selected_workflow_id,
            definition=WorkflowDefinition(
                workflow_id=selected_workflow_id,
                initial_state="complete",
                states={
                    "complete": WorkflowStateSpec(
                        state_id="complete",
                        actions=(
                            WorkflowActionInvocation(
                                action_id="testing.prepare_arxiv_paper_ingestion_fixture"
                            ),
                        ),
                        terminal=True,
                    )
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
            data={"response_text": "Executed via arXiv testing workflow."},
        )

    monkeypatch.setattr(orchestrator._workflow_executor, "run", _run_workflow)

    prompt = (
        "Run the arXiv paper ingestion testing workflow on "
        "https://arxiv.org/abs/2603.21702. Use the workflow itself to verify "
        "title, authors, abstract, publication date, provenance, and cleanup."
    )
    result = orchestrator.run(
        prompt=prompt,
        context=[],
        llm_client=_CapturingLLM([CHAT_ASSISTANT_WORKFLOW_ID, "Plain response only."]),
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against one "
                        "live arXiv paper and verify title, authors, abstract, "
                        "publication date, provenance, and cleanup."
                    ),
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Arxiv Paper Ingestion Testing Workflow",
                    "description": (
                        "Execute the canonical arXiv ingestion workflow against one "
                        "live arXiv paper and verify title, authors, abstract, "
                        "publication date, provenance, and cleanup."
                    ),
                    "confidence_score": 1.0,
                    "relevance_score": 1.0,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "match_count": 1,
        },
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert captured_execution == {}
    assert not any(
        isinstance(entry, dict)
        and entry.get("type")
        in {"workflow_selector_override", "custom_workflow_override_policy"}
        for entry in result.aux_llm_calls
    )


