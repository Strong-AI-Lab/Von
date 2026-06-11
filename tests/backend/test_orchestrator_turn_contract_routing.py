"""Focused workflow selector routing tests split from the shared orchestrator harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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

_DISPATCH_POLICY_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "turn_contract_dispatch_policy.json"
)


@pytest.fixture(autouse=True)
def _represented_dispatch_policy(monkeypatch):
    """Serve the canonical dispatch-policy fixture as the represented authority.

    The live authority is the #V#turn_contract_dispatch_policy concept
    (JVNAUTOSCI-2365); these real-path tests resolve the same authored rules
    from the checked-in fixture instead of a live KB.
    """

    payload = json.loads(_DISPATCH_POLICY_FIXTURE_PATH.read_text(encoding="utf-8"))

    def _fixture_rows(concept_id, predicate=None, limit=1):
        if (
            concept_id == "#V#turn_contract_dispatch_policy"
            and predicate == "hasContent"
        ):
            return [{"text": json.dumps(payload)}]
        return []

    monkeypatch.setattr(
        "src.backend.services.turn_contract_dispatch_policy_service."
        "get_texts_for_concept",
        _fixture_rows,
    )


def test_multi_surface_turn_contract_overrides_selected_custom_workflow_to_tool_pipeline(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"
    structured_contract = _build_structured_turn_contract_payload(
        summary=(
            "A concise research briefing comprising represented papers, recent "
            "relevant arXiv work, and linked Jira tasks."
        ),
        grounding_requirement=(
            "Papers must be grounded via authorship or ownership relationships."
        ),
        selector_guidance="Use KB/concept retrieval, arXiv search, and Jira retrieval.",
        required_tools=(
            "search_knowledge_base",
            "search_concepts",
            "search_arxiv",
            "jira_search",
        ),
    )

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Inspect represented concepts and relations for retrieval questions.",
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "search_knowledge_base": {"category": "read"},
            "search_concepts": {"category": "read"},
            "search_arxiv": {"category": "read"},
            "jira_search": {"category": "read"},
        },
    )
    dispatch_metadata = {
        "search_knowledge_base": _dispatch_surface("knowledge_base"),
        "search_concepts": _dispatch_surface("knowledge_base"),
        "search_arxiv": _dispatch_surface("arxiv", external_surface=True),
        "jira_search": _dispatch_surface("jira", external_surface=True),
    }
    monkeypatch.setattr(
        orchestrator_module,
        "get_tool_dispatch_surface_metadata",
        lambda tool_name: dispatch_metadata.get(str(tool_name).strip().lower()),
    )

    execute_calls: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "final_response": "Research briefing via tool pipeline.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt=(
            "Prepare a short research briefing for me: my represented papers, "
            "relevant recent arXiv work, and any linked Jira tasks."
        ),
        context=[
            {
                "role": "system",
                "content": (
                    "Expected answer contract for this turn:\n"
                    "- Success target: Contradictory prose should not control dispatch.\n"
                    "- Required tools: task_list\n"
                    "- Selector guidance: This support string is intentionally wrong."
                ),
            }
        ],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.96,
                        "reasoning": (
                            "The specialised concept-search workflow is best for "
                            "grounded represented retrieval."
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
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve represented concepts and relations.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve represented concepts and relations.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "match_count": 1,
            **structured_contract,
        },
    )

    assert execute_calls == [TOOL_CALLING_WORKFLOW_ID]
    assert result.response_text == "Research briefing via tool pipeline."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"

    preflight_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "prompt_tool_requirements_preflight"
            and entry.get("stage") == "workflow_dispatch"
        ),
        None,
    )
    assert preflight_entry is not None
    assert preflight_entry.get("contract_required_tools") == [
        "search_knowledge_base",
        "search_concepts",
        "search_arxiv",
        "jira_search",
    ]
    assert preflight_entry.get("required_tool_surface_families") == [
        "knowledge_base",
        "arxiv",
        "jira",
    ]
    contract_check_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_turn_contract_check"
        ),
        None,
    )
    assert contract_check_entry is not None
    assert contract_check_entry.get("status") == "override_required"
    assert contract_check_entry.get("selected_workflow_id") == selected_workflow_id
    assert contract_check_entry.get("selected_workflow_can_satisfy_contract") is False
    assert contract_check_entry.get("required_surface_families") == [
        "knowledge_base",
        "arxiv",
        "jira",
    ]
    prepare_step_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_prepare_step"
            and entry.get("step_id") == "turn_contract_dispatch_preflight"
        ),
        None,
    )
    assert prepare_step_entry is not None
    assert (
        "could not satisfy the multi-surface turn contract"
        in str(prepare_step_entry.get("result_summary") or "").lower()
    )

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
        ),
        None,
    )
    assert override_entry is not None
    assert override_entry.get("prior_selected_workflow_id") == selected_workflow_id
    assert override_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert override_entry.get("turn_contract_required_tools") == [
        "search_knowledge_base",
        "search_concepts",
        "search_arxiv",
        "jira_search",
    ]
    assert override_entry.get("turn_contract_external_surface_families") == [
        "arxiv",
        "jira",
    ]


def test_multi_surface_turn_contract_records_satisfied_tool_pipeline_dispatch_check(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    structured_contract = _build_structured_turn_contract_payload(
        summary=(
            "Three grounded research actions based on represented papers, relevant "
            "recent arXiv work, and linked Jira tasks."
        ),
        grounding_requirement=(
            "Papers and tasks must be grounded in represented or retrieved evidence."
        ),
        selector_guidance="Use KB/concept retrieval, arXiv search, and Jira retrieval.",
        required_tools=(
            "search_knowledge_base",
            "search_concepts",
            "search_arxiv",
            "jira_search",
        ),
    )

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "search_knowledge_base": {"category": "read"},
            "search_concepts": {"category": "read"},
            "search_arxiv": {"category": "read"},
            "jira_search": {"category": "read"},
        },
    )
    dispatch_metadata = {
        "search_knowledge_base": _dispatch_surface("knowledge_base"),
        "search_concepts": _dispatch_surface("knowledge_base"),
        "search_arxiv": _dispatch_surface("arxiv", external_surface=True),
        "jira_search": _dispatch_surface("jira", external_surface=True),
    }
    monkeypatch.setattr(
        orchestrator_module,
        "get_tool_dispatch_surface_metadata",
        lambda tool_name: dispatch_metadata.get(str(tool_name).strip().lower()),
    )

    execute_calls: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "final_response": "Research actions via tool pipeline.",
                "tool_messages": [],
                "invocations": [],
                "iteration_count": 1,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt=(
            "Prepare my next three research actions from my represented papers, "
            "recent arXiv work, and linked Jira tasks."
        ),
        context=[
            {
                "role": "system",
                "content": (
                    "Expected answer contract for this turn:\n"
                    "- Success target: Three grounded research actions based on "
                    "represented papers, relevant recent arXiv work, and linked Jira tasks.\n"
                    "- Grounding requirement: Papers and tasks must be grounded in "
                    "represented or retrieved evidence.\n"
                    "- Required tools: search_knowledge_base, search_concepts, "
                    "search_arxiv, jira_search\n"
                    "- Selector guidance: Use KB/concept retrieval, arXiv search, "
                    "and Jira retrieval."
                ),
            }
        ],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": TOOL_CALLING_WORKFLOW_ID,
                        "confidence": 0.94,
                        "reasoning": (
                            "This needs multi-surface retrieval through the "
                            "general tool workflow."
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
                    "concept_id": TOOL_CALLING_WORKFLOW_ID,
                    "name": "Tool Calling Workflow",
                    "description": "General-purpose multi-surface tool workflow.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.98,
                    "confidence_score": 0.98,
                }
            ],
            "candidates": [
                {
                    "concept_id": TOOL_CALLING_WORKFLOW_ID,
                    "name": "Tool Calling Workflow",
                    "description": "General-purpose multi-surface tool workflow.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.98,
                    "confidence_score": 0.98,
                }
            ],
            "match_count": 1,
            **structured_contract,
        },
    )

    assert execute_calls == [TOOL_CALLING_WORKFLOW_ID]
    assert result.response_text == "Research actions via tool pipeline."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    contract_check_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_turn_contract_check"
        ),
        None,
    )
    assert contract_check_entry is not None
    assert contract_check_entry.get("status") == "selected_workflow_satisfies_contract"
    assert contract_check_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert contract_check_entry.get("selected_workflow_can_satisfy_contract") is True
    assert contract_check_entry.get("required_surface_families") == [
        "knowledge_base",
        "arxiv",
        "jira",
    ]
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
        for entry in result.aux_llm_calls
    )
    prepare_step_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_prepare_step"
            and entry.get("step_id") == "turn_contract_dispatch_preflight"
        ),
        None,
    )
    assert prepare_step_entry is not None
    assert (
        "satisfied the multi-surface turn contract"
        in str(prepare_step_entry.get("result_summary") or "").lower()
    )


def test_turn_contract_dispatch_preflight_outcome_tracks_dispatch_surface_metadata(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"
    structured_contract = _build_structured_turn_contract_payload(
        summary="Retrieve represented concept evidence and linked Jira issues.",
        grounding_requirement="Ground the answer in represented concepts and Jira data.",
        selector_guidance="Use KB retrieval and Jira retrieval.",
        required_tools=(
            "search_knowledge_base",
            "jira_search",
        ),
    )

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Inspect represented concepts and linked work items.",
    )
    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "search_knowledge_base": {"category": "read"},
            "jira_search": {"category": "read"},
        },
    )
    dispatch_metadata = {
        "search_knowledge_base": _dispatch_surface("knowledge_base"),
        "jira_search": _dispatch_surface("jira", external_surface=False),
    }
    monkeypatch.setattr(
        orchestrator_module,
        "get_tool_dispatch_surface_metadata",
        lambda tool_name: dispatch_metadata.get(str(tool_name).strip().lower()),
    )

    execute_calls: list[str] = []

    def _execute_workflow(workflow_id: str, **_kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id != selected_workflow_id:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        return SimpleNamespace(
            data={
                "final_response": "Custom workflow stayed selected.",
                "tool_messages": [],
                "invocations": [
                    {"tool": "search_knowledge_base", "payload": {"success": True}},
                    {"tool": "jira_search", "payload": {"success": True}},
                ],
                "iteration_count": 2,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="Summarise my represented concept notes and linked Jira issues.",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.95,
                        "reasoning": "The specialised represented-retrieval workflow fits best.",
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
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve represented concepts and linked work items.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve represented concepts and linked work items.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "match_count": 1,
            **structured_contract,
        },
    )

    assert execute_calls == [selected_workflow_id]
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "rag_selected"
    contract_check_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_turn_contract_check"
        ),
        None,
    )
    assert contract_check_entry is not None
    assert contract_check_entry.get("status") == "no_external_surface_requirement"
    assert contract_check_entry.get("required_surface_families") == [
        "knowledge_base",
        "jira",
    ]
    assert contract_check_entry.get("external_surface_families") == []
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason")
        == "selected_custom_workflow_cannot_satisfy_multi_surface_turn_contract"
        for entry in result.aux_llm_calls
    )


def test_direct_response_turn_contract_required_tools_recover_to_tool_pipeline(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    structured_contract = _build_structured_turn_contract_payload(
        summary=(
            "List visible Gmail profiles and inspect whether any are related to "
            "the authenticated user by represented predicates."
        ),
        grounding_requirement=(
            "The profile list must come from Gmail and predicate links must come "
            "from represented relation lookup."
        ),
        selector_guidance=(
            "Use Gmail profile inventory and Vontology relation-bearing evidence."
        ),
        required_tools=("gmail_list_profiles", "find_relations_with_argument"),
    )

    monkeypatch.setattr(
        orchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda **_kwargs: ("You are Von.", "#V#test_base_system_prompt"),
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "gmail_list_profiles": {"category": "read"},
            "find_relations_with_argument": {"category": "read"},
        },
    )
    dispatch_metadata = {
        "gmail_list_profiles": _dispatch_surface("gmail", external_surface=True),
        "find_relations_with_argument": _dispatch_surface("knowledge_base"),
    }
    monkeypatch.setattr(
        orchestrator_module,
        "get_tool_dispatch_surface_metadata",
        lambda tool_name: dispatch_metadata.get(str(tool_name).strip().lower()),
    )

    execute_calls: list[str] = []
    tool_pipeline_payload: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id != TOOL_CALLING_WORKFLOW_ID:
            raise AssertionError(f"Unexpected workflow execution: {workflow_id}")
        tool_pipeline_payload.update(dict(kwargs))
        return SimpleNamespace(
            data={
                "final_response": "Recovered with Gmail and predicate evidence.",
                "tool_messages": [],
                "invocations": [
                    {"tool": "gmail_list_profiles", "payload": {"success": True}},
                    {
                        "tool": "find_relations_with_argument",
                        "payload": {"success": True},
                    },
                ],
                "iteration_count": 2,
            },
            final_state="complete",
            completed=True,
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt=(
            "What gmail profiles can you see, and let me know if any are "
            "connected to me via predicates"
        ),
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
                        "confidence": 0.91,
                        "reasoning": ("This looks like an identity/context question."),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General direct response workflow.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.76,
                    "confidence_score": 0.76,
                },
                {
                    "concept_id": TOOL_CALLING_WORKFLOW_ID,
                    "name": "Tool Calling Workflow",
                    "description": "General tool workflow.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.74,
                    "confidence_score": 0.74,
                },
            ],
            "candidates": [
                {"concept_id": CHAT_ASSISTANT_WORKFLOW_ID},
                {"concept_id": TOOL_CALLING_WORKFLOW_ID},
            ],
            "match_count": 2,
            **structured_contract,
        },
    )

    assert execute_calls == [TOOL_CALLING_WORKFLOW_ID]
    assert result.response_text == "Recovered with Gmail and predicate evidence."
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.verdict == "tool_contract_override"
    assert result.workflow_routing.source == "selector_override"
    assert tool_pipeline_payload["data"]["turn_expected_outcome_contract_state"][
        "required_tools"
    ] == [
        "gmail_list_profiles",
        "find_relations_with_argument",
    ]

    contract_check_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_dispatch_turn_contract_check"
        ),
        None,
    )
    assert contract_check_entry is not None
    assert (
        contract_check_entry.get("status")
        == "direct_response_route_requires_tool_pipeline"
    )
    assert (
        contract_check_entry.get("override_reason")
        == "direct_response_route_cannot_satisfy_required_turn_tools"
    )
    assert contract_check_entry.get("selected_workflow_id") == (
        CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert contract_check_entry.get("selected_workflow_can_satisfy_contract") is False
    assert contract_check_entry.get("required_surface_families") == [
        "gmail",
        "knowledge_base",
    ]
    assert contract_check_entry.get("external_surface_families") == ["gmail"]

    override_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_selector_override"
            and entry.get("reason")
            == "direct_response_route_cannot_satisfy_required_turn_tools"
        ),
        None,
    )
    assert override_entry is not None
    assert override_entry.get("prior_selected_workflow_id") == (
        CHAT_ASSISTANT_WORKFLOW_ID
    )
    assert override_entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert override_entry.get("turn_contract_required_tools") == [
        "gmail_list_profiles",
        "find_relations_with_argument",
    ]


def test_completed_custom_workflow_missing_required_tools_recovers_to_tool_pipeline(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"
    structured_contract = _build_structured_turn_contract_payload(
        summary="List predicates for scientific papers.",
        grounding_requirement=(
            "Predicates must come from predicate incidence for the resolved concept."
        ),
        selector_guidance=(
            "Resolve the scientific paper concept, then inspect predicate incidence."
        ),
        required_tools=("search_concepts", "get_predicate_incidence"),
    )

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Retrieve grounded concept profiles from Vontology.",
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "search_concepts": {"category": "read"},
            "get_predicate_incidence": {"category": "read"},
        },
    )

    execute_calls: list[str] = []
    tool_pipeline_payload: dict[str, Any] = {}

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={
                    "final_response": (
                        "The specific predicates were not included in the provided "
                        "execution results."
                    ),
                    "tool_messages": [],
                    "invocations": [],
                    "iteration_count": 0,
                },
                final_state="complete",
                completed=True,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": "Recovered with predicate-incidence evidence.",
                    "tool_messages": [],
                    "invocations": [
                        {"tool": "search_concepts", "payload": {"success": True}},
                        {
                            "tool": "get_predicate_incidence",
                            "payload": {"success": True},
                        },
                    ],
                    "iteration_count": 2,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="What are key predicates for scientific papers in Vontology?",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.95,
                        "reasoning": (
                            "The specialised represented-retrieval workflow fits."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve grounded concept profiles.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve grounded concept profiles.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "match_count": 1,
            **structured_contract,
        },
    )

    assert execute_calls == [selected_workflow_id, TOOL_CALLING_WORKFLOW_ID]
    assert result.response_text == "Recovered with predicate-incidence evidence."
    assert tool_pipeline_payload["data"]["prior_failed_selected_workflow"] == {
        "workflow_id": selected_workflow_id,
        "completed": True,
        "tool_progress_detected": False,
        "missing_required_tools": [
            "search_concepts",
            "get_predicate_incidence",
        ],
        "recovery_reason": "custom_workflow_missing_required_prompt_tools",
        "final_state": "complete",
    }

    handoff = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "workflow_recovery_handoff"
        ),
        None,
    )
    assert handoff is not None
    assert handoff.get("reason") == "custom_workflow_missing_required_prompt_tools"
    assert handoff.get("missing_required_tools") == [
        "search_concepts",
        "get_predicate_incidence",
    ]

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
    assert terminal_boundary.get("status") == "follow_up_required"
    assert terminal_boundary.get("continued_to_tool_pipeline") is True
    assert terminal_boundary.get("missing_required_tools") == [
        "search_concepts",
        "get_predicate_incidence",
    ]


def test_custom_workflow_required_effects_recovery_carries_tools_to_pipeline(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#concept_search_instance_retrieval_workflow"

    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Retrieve grounded concept profiles from Vontology.",
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {
            "search_concepts": {"category": "read"},
            "get_predicate_incidence": {"category": "read"},
        },
    )

    execute_calls: list[str] = []
    tool_pipeline_payload: dict[str, Any] = {}

    required_effects_contract = {
        "schema_version": "workflow_required_effects_contract.v1",
        "required_effects": [
            {
                "effect_id": "effect_prompt_required_evidence_search_concepts_1",
                "effect_type": "required_evidence",
                "required_tools": ["search_concepts"],
            },
            {
                "effect_id": (
                    "effect_prompt_required_evidence_get_predicate_incidence_2"
                ),
                "effect_type": "required_evidence",
                "required_tools": ["get_predicate_incidence"],
            },
        ],
    }

    def _execute_workflow(workflow_id: str, **kwargs: Any):
        execute_calls.append(workflow_id)
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={
                    "final_response": (
                        "The selected workflow completed without evidence tools."
                    ),
                    "tool_messages": [],
                    "invocations": [],
                    "workflow_required_effects_contract": required_effects_contract,
                    "iteration_count": 0,
                },
                final_state="complete",
                completed=True,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": "Recovered with required evidence.",
                    "tool_messages": [],
                    "invocations": [
                        {"tool": "search_concepts", "payload": {"success": True}},
                        {
                            "tool": "get_predicate_incidence",
                            "payload": {"success": True},
                        },
                    ],
                    "iteration_count": 2,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    result = orchestrator.run(
        prompt="What are key predicates for scientific papers in Vontology?",
        context=[],
        llm_client=_CapturingLLM(
            [
                json.dumps(
                    {
                        "workflow_id": selected_workflow_id,
                        "confidence": 0.95,
                        "reasoning": (
                            "The specialised represented-retrieval workflow fits."
                        ),
                    }
                )
            ]
        ),
        model=None,
        user_namespace="#V#user@org",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve grounded concept profiles.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Concept Search Instance Retrieval Workflow",
                    "description": "Retrieve grounded concept profiles.",
                    "routing_eligible": True,
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "candidate_source": "workflow_discovery",
                    "relevance_score": 0.99,
                    "confidence_score": 0.99,
                }
            ],
            "match_count": 1,
        },
    )

    assert execute_calls == [selected_workflow_id, TOOL_CALLING_WORKFLOW_ID]
    assert result.response_text == "Recovered with required evidence."
    tool_pipeline_data = tool_pipeline_payload["data"]
    assert tool_pipeline_data["required_prompt_tools"] == [
        "search_concepts",
        "get_predicate_incidence",
    ]
    assert tool_pipeline_data["missing_prompt_tools"] == [
        "search_concepts",
        "get_predicate_incidence",
    ]
    contract_state = tool_pipeline_data["turn_expected_outcome_contract_state"]
    assert contract_state["required_tools"] == [
        "search_concepts",
        "get_predicate_incidence",
    ]


def test_custom_workflow_fallback_handoff_preserves_turn_expected_outcome_contract(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_POLICY_UNSAFE", "1")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#predicate_schema_wrapper_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Predicate/schema wrapper workflow for tool handoff contract tests.",
    )

    expected_contract = {
        "summary": "Identify predicates salient to SAIL students.",
        "grounding_requirement": (
            "Predicates must be substantiated by represented relationships or text relations."
        ),
        "precision_policy": "Prefer omission over unsupported predicate claims.",
        "selector_guidance": (
            "Use search_concepts and get_text_relations_summary before answering."
        ),
        "answering_guidance": "List only grounded predicates and say when evidence is missing.",
        "reasoning": "Predicate/schema turns need grounded ontology retrieval rather than generic chat.",
    }
    expected_discovery_contract = {
        "summary": expected_contract["summary"],
        "grounding_requirement": expected_contract["grounding_requirement"],
        "selector_guidance": expected_contract["selector_guidance"],
    }
    structured_contract = _build_structured_turn_contract_payload(
        summary=expected_discovery_contract["summary"],
        grounding_requirement=expected_discovery_contract["grounding_requirement"],
        selector_guidance=expected_discovery_contract["selector_guidance"],
    )

    class _ContractAwareSelectorLLM:
        def __init__(self) -> None:
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
            if "expected-success inference policy" in prompt:
                return "{}"
            if isinstance(prompt, str) and prompt.startswith("Select workflow"):
                return selected_workflow_id
            return "Recovered through the general tool workflow."

    llm = _ContractAwareSelectorLLM()
    tool_pipeline_payload: dict[str, Any] = {}

    def _fake_execute_workflow(workflow_id: str, **kwargs: Any):
        if workflow_id == selected_workflow_id:
            return SimpleNamespace(
                data={"response_text": "Selected specialised workflow failed."},
                final_state="#V#workflow_step_predicate_schema_wrapper_failed",
                completed=True,
                error=None,
            )
        if workflow_id == TOOL_CALLING_WORKFLOW_ID:
            tool_pipeline_payload.update(dict(kwargs))
            return SimpleNamespace(
                data={
                    "final_response": "Recovered through the general tool workflow.",
                    "tool_messages": [],
                    "invocations": [{"tool": "get_text_relations_summary"}],
                    "iteration_count": 1,
                },
                final_state="complete",
                completed=True,
            )
        raise AssertionError(f"Unexpected workflow execution: {workflow_id}")

    monkeypatch.setattr(orchestrator, "execute_workflow", _fake_execute_workflow)

    result = orchestrator.run(
        prompt="What predicates are salient to SAIL students?",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Predicate Schema Wrapper Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "candidates": [
                {
                    "concept_id": selected_workflow_id,
                    "name": "Predicate Schema Wrapper Workflow",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                }
            ],
            "query": "What predicates are salient to SAIL students?",
            **structured_contract,
        },
    )

    assert result.response_text == "Recovered through the general tool workflow."
    handoff_data = tool_pipeline_payload["data"]
    assert handoff_data["turn_expected_outcome_profile"] == expected_discovery_contract
    assert handoff_data["turn_expected_outcome_contract"] == expected_discovery_contract
    contract_state = handoff_data["turn_expected_outcome_contract_state"]
    assert contract_state["schema_version"] == "turn_expected_outcome_contract.v1"
    assert contract_state["fields"] == expected_discovery_contract
    assert contract_state["field_count"] == len(expected_discovery_contract)
    assert "turn_expected_outcome_contract" in (contract_state.get("sources") or [])
    assert (
        handoff_data["turn_expected_outcome_summary"]
        == expected_discovery_contract["summary"]
    )
    assert handoff_data["turn_expected_grounding_requirement"] == (
        expected_discovery_contract["grounding_requirement"]
    )
    assert (
        handoff_data["turn_selector_guidance"]
        == expected_discovery_contract["selector_guidance"]
    )
    assert "turn_expected_precision_policy" not in handoff_data
    assert "turn_answering_guidance" not in handoff_data
    assert "turn_expected_outcome_reasoning" not in handoff_data
