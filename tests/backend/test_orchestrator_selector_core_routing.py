"""Focused workflow selector routing tests split from the shared orchestrator harness."""

# ruff: noqa: F405

from __future__ import annotations

import pytest

from tests.backend.test_orchestrator_workflow_selector_routing import *  # noqa: F401,F403
from tests.backend.test_orchestrator_workflow_selector_routing import (
    _CapturingLLM,
    _build_orchestrator,
    _register_terminal_custom_workflow,
    _stub_execute_workflow_result,
)


def test_orchestrator_injects_voice_hint_when_prompt_asks_for_voice(monkeypatch):
    """Voice queries should be grounded via client capabilities snapshot."""
    from flask import Flask

    from src.backend.services.client_capabilities_service import (
        set_client_capabilities_snapshot,
    )

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test")

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)

    snapshot = {
        "speech_synthesis": {
            "supported": True,
            "voices_count": 3,
            "default_voice_lang": "en-NZ",
            "settings": {"voice_name": "Test Voice"},
        }
    }

    llm = _CapturingLLM(["Here is a reply."])

    with app.test_request_context("/"):
        set_client_capabilities_snapshot(snapshot)

        orchestrator.run(
            prompt="What voice are you using?",
            context=[],
            llm_client=llm,
            model=None,
            user_namespace="#V#user",
        )

    assert llm.calls, "Expected at least one LLM call"
    combined_context = "\n".join(
        str(item.get("content") or "")
        for item in (llm.calls[0].get("context") or [])
        if isinstance(item, dict)
    )
    assert "Client-reported speech synthesis settings" in combined_context
    assert "voice_name='Test Voice'" in combined_context


# ---------------------------------------------------------------------------
# Narration workflow routing (selector ON, presenter mode).
# ---------------------------------------------------------------------------


def test_workflow_selector_routes_to_narration_workflow(monkeypatch):
    """When enabled and classifier returns 'narration', orchestrator emits spoken+screen."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    presenter_protocol = {
        "role": "system",
        "content": (
            "PRESENTER MODE PROTOCOL:\n"
            "- Output EXACTLY TWO tagged blocks and nothing else:\n"
            "  <spoken>...brief talk track...</spoken>\n"
            "  <screen>...full on-screen content...</screen>\n"
        ),
    }

    llm = _CapturingLLM(
        [
            CHAT_NARRATION_WORKFLOW_ID,  # workflow selector verdict
            "Here is the answer on screen.",  # main assistant screen response
            "<spoken>Short talk track.</spoken>",  # narration generation
        ]
    )

    result = orchestrator.run(
        prompt="hi",
        context=[presenter_protocol],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.response_text == (
        "<spoken>Short talk track.</spoken>\n\n"
        "<screen>Here is the answer on screen.</screen>"
    )

    # Ensure we actually invoked the selector and then narration.
    assert len(llm.calls) == 3
    assert llm.calls[0]["prompt"].startswith("Select workflow")
    assert "hi" in llm.calls[0]["prompt"]
    narration_prompt = str(llm.calls[2]["prompt"] or "")
    assert "<spoken>" in narration_prompt
    assert "spoken" in narration_prompt.lower()

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: Selector fires without presenter mode.
# ---------------------------------------------------------------------------


def test_selector_fires_without_presenter_mode(monkeypatch):
    """Workflow selector should run for any authenticated turn (no presenter-mode gate)."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=TOOL_CALLING_WORKFLOW_ID,
        data={
            "final_response": "I'll help with that.",
            "tool_messages": [],
            "invocations": [],
            "iteration_count": 1,
        },
    )

    # No presenter mode context — selector should still fire.
    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,  # workflow selector verdict
        ]
    )

    result = orchestrator.run(
        prompt="Search for papers about transformers",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    # The selector still fires even when the final response comes from the
    # tool workflow rather than a separate presenter-mode path.
    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"].startswith("Select workflow")
    assert "Search for papers about transformers" in llm.calls[0]["prompt"]
    assert result.response_text == "I'll help with that."

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types

    # Verify the selector chose tool_calling workflow.
    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == TOOL_CALLING_WORKFLOW_ID
    assert selector_entry["verdict"] == "rag_selected"


def test_selector_prompt_unavailable_fails_closed_without_selector_llm(monkeypatch):
    """Missing authoritative selector prompt should skip selector LLM dispatch."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    original_render_prompt = orchestrator._prompt_templates.render_prompt

    def _render_without_selector_prompt(
        prompt_ids: Any,
        *,
        fallback: Any = None,
        variables: Any = None,
        max_chars: Any = None,
    ) -> Any:
        requested_prompt_ids = {
            str(item).strip()
            for item in (prompt_ids or ())
            if isinstance(item, str) and str(item).strip()
        }
        if requested_prompt_ids.intersection(orchestrator._TURN_SELECTOR_PROMPTS):
            return None
        return original_render_prompt(
            prompt_ids,
            fallback=fallback,
            variables=variables,
            max_chars=max_chars,
        )

    monkeypatch.setattr(
        orchestrator._prompt_templates,
        "render_prompt",
        _render_without_selector_prompt,
    )

    llm = _CapturingLLM(
        [
            "I'll help with that.",
        ]
    )

    result = orchestrator.run(
        prompt="hello",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert len(llm.calls) == 1
    assert all(
        not str(call["prompt"]).startswith("Select workflow") for call in llm.calls
    )
    assert result.response_text == "I'll help with that."

    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == CHAT_ASSISTANT_WORKFLOW_ID
    assert selector_entry["verdict"] == "selector_prompt_unavailable"
    assert selector_entry["selection_source"] == "selector_fail_closed"
    assert selector_entry["prompt_failure_reason"] == "selector_prompt_unavailable"


def test_selector_prompt_unavailable_recovers_single_discovered_execution_workflow(
    monkeypatch,
):
    """Fail-closed selector recovery should not drift to chat when one execution candidate is already grounded."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    selected_workflow_id = "#V#zhan_gmail_arxiv_ingestion_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=selected_workflow_id,
        purpose="Ingest unseen Gmail arXiv digests for Zhan and label completed mail.",
    )
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=selected_workflow_id,
        data={"response_text": "Grounded Gmail arXiv ingestion completed."},
    )

    original_render_prompt = orchestrator._prompt_templates.render_prompt

    def _render_without_selector_prompt(
        prompt_ids: Any,
        *,
        fallback: Any = None,
        variables: Any = None,
        max_chars: Any = None,
    ) -> Any:
        requested_prompt_ids = {
            str(item).strip()
            for item in (prompt_ids or ())
            if isinstance(item, str) and str(item).strip()
        }
        if requested_prompt_ids.intersection(orchestrator._TURN_SELECTOR_PROMPTS):
            return None
        return original_render_prompt(
            prompt_ids,
            fallback=fallback,
            variables=variables,
            max_chars=max_chars,
        )

    monkeypatch.setattr(
        orchestrator._prompt_templates,
        "render_prompt",
        _render_without_selector_prompt,
    )

    candidate = {
        "concept_id": selected_workflow_id,
        "name": "Zhan Gmail arXiv Ingestion Workflow",
        "description": "Process unseen Zhan Gmail arXiv messages and label completed work.",
        "match_source": "capability_index",
        "confidence_score": 0.98,
        "relevance_score": 0.98,
        "routing_eligible": True,
        "is_executable": True,
        "is_policy_safe": True,
        "turn_launchable": True,
        "executability_reason": "executable_now",
        "candidate_source": "workflow_discovery",
        "routing_profile": {"role": "execution"},
    }
    llm = _CapturingLLM(["Unexpected fallback response."])
    result = orchestrator.run(
        prompt="Process Zhan's unseen Gmail arXiv digests.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [candidate],
            "candidates": [candidate],
            "match_count": 1,
        },
        conversation_session_id="session-selector-prompt-unavailable-recovery",
        turn_id="turn-selector-prompt-unavailable-recovery",
    )

    assert all(
        not str(call["prompt"]).startswith("Select workflow") for call in llm.calls
    )
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == selected_workflow_id
    assert result.workflow_routing.verdict == "selector_prompt_unavailable"
    assert result.workflow_routing.source == "selector_fail_closed"
    assert result.response_text == "Grounded Gmail arXiv ingestion completed."

    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == selected_workflow_id
    assert selector_entry["selection_source"] == "selector_fail_closed"
    assert selector_entry["prompt_failure_reason"] == "selector_prompt_unavailable"
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "single_specialised_candidate_recovery_from_selector_prompt_unavailable"
    )
    assert selector_entry["selection_metadata"]["recovered_candidate_workflow_id"] == (
        selected_workflow_id
    )


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: Discovered workflows in selector prompt.
# ---------------------------------------------------------------------------


def test_selector_receives_discovered_workflows(monkeypatch):
    """Discovered workflows should reach selector context after JIT registration."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    custom_workflow_id = "#V#custom_analysis_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=custom_workflow_id,
        purpose="Runs a custom data analysis pipeline.",
    )

    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=custom_workflow_id,
        data={"final_response": "Custom analysis complete."},
    )

    llm = _CapturingLLM([custom_workflow_id.lower()])

    discovery_result = {
        "matches": [
            {
                "concept_id": custom_workflow_id,
                "name": "Custom Analysis",
                "description": "Runs a custom data analysis pipeline.",
                "relevance_score": 0.85,
                "is_executable": True,
                "executability_reason": "executable_now",
                "routing_eligible": True,
                "candidate_source": "workflow_discovery",
            }
        ],
        "candidates": [
            {
                "concept_id": custom_workflow_id,
                "name": "Custom Analysis",
                "description": "Runs a custom data analysis pipeline.",
                "relevance_score": 0.85,
                "is_executable": True,
                "executability_reason": "executable_now",
                "routing_eligible": True,
                "candidate_source": "workflow_discovery",
            }
        ],
        "match_count": 1,
    }

    result = orchestrator.run(
        prompt="Run a custom analysis",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    # Selector should have fired with the discovered workflow context.
    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    discovered_ids = set(selector_entry.get("discovered_workflow_ids", []))
    assert custom_workflow_id in discovered_ids
    assert {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
        CHAT_NARRATION_WORKFLOW_ID,
    }.issubset(discovered_ids)
    assert selector_entry.get("discovery_candidate_count") == 4
    assert selector_entry.get("discovery_excluded_count") == 0

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == custom_workflow_id
    assert result.response_text == "Custom analysis complete."


def test_required_tool_contract_preserves_represented_candidate_and_excludes_direct_defaults(
    monkeypatch,
):
    """Tool agreements inform represented selection but still exclude no-tool defaults."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": "#V#missing_tool_call_workflow",
                        "name": "Missing Tool Call Workflow",
                        "description": "Recovery workflow for already observed missing tool calls.",
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "candidate_source": "workflow_discovery",
                        "routing_profile": {"role": "execution"},
                    }
                ],
                "match_count": 1,
                "contract_projection": {
                    "required_tools": ["jira_get_issue", "jira_get_issue"],
                },
            },
            prompt="Tell me about JVNAUTOSCI-150 in JIRA",
        )
    )

    assert [candidate.get("concept_id") for candidate in discovered] == [
        "#V#missing_tool_call_workflow"
    ]
    assert [candidate.get("concept_id") for candidate in selector_candidates] == [
        "#V#missing_tool_call_workflow",
        TOOL_CALLING_WORKFLOW_ID,
    ]
    represented_candidate = selector_candidates[0]
    assert represented_candidate["routing_eligible"] is True
    assert represented_candidate["candidate_reason"] == (
        "discovered_workflow_candidate"
    )
    assert represented_candidate["selector_fast_path_eligible"] is False
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": [],
        "remaining_turn_level_required_tools": ["jira_get_issue"],
    }

    tool_candidate = selector_candidates[1]
    assert tool_candidate["candidate_source"] == "selector_default"
    assert tool_candidate["candidate_reason"] == "required_turn_tools"
    assert tool_candidate["routing_profile_role"] == "execution"
    assert tool_candidate["turn_launchable"] is True
    assert tool_candidate["covers_expected_tool_set"] is True
    assert tool_candidate["covers_success_contract"] is True
    assert tool_candidate["satisfies_expected_outcome_contract"] is True
    assert tool_candidate["matched_required_tools"] == ["jira_get_issue"]
    assert tool_candidate["routing_index_metadata"]["required_tools"] == [
        "jira_get_issue"
    ]

    excluded_by_id = {candidate["concept_id"]: candidate for candidate in excluded}
    assert set(excluded_by_id) == {
        CHAT_ASSISTANT_WORKFLOW_ID,
        CHAT_NARRATION_WORKFLOW_ID,
    }
    for workflow_id, candidate in excluded_by_id.items():
        assert candidate["routing_eligible"] is False
        assert candidate["required_tools"] == ["jira_get_issue"]
        assert candidate["candidate_source"] == "selector_default"
        assert candidate["candidate_reason"] == "selector_default_excluded"
        assert candidate["routing_exclusion_reason"] == (
            "direct_response_route_cannot_satisfy_required_turn_tools"
        )


def test_related_resolution_workflow_stays_visible_with_exact_coverage_gap(monkeypatch):
    """Related represented affordances remain selectable without becoming aliases."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#generic_resolution_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Resolve and verify a represented target.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Generic Resolution Workflow",
                        "description": "Resolve and verify a represented target.",
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "routing_profile": {"role": "execution"},
                        "routing_index_metadata": {
                            "required_tools": [
                                "search_concepts",
                                "fetch_concept",
                            ]
                        },
                    }
                ],
                "match_count": 1,
                "contract_projection": {
                    "required_tools": [
                        "resolve_concept_by_name",
                        "fetch_concept",
                    ]
                },
            },
            prompt="Resolve the named target, then read it back.",
        )
    )

    assert [item["concept_id"] for item in discovered] == [workflow_id]
    assert selector_candidates[0]["concept_id"] == workflow_id
    assert selector_candidates[0]["candidate_reason"] == (
        "discovered_workflow_candidate"
    )
    assert selector_candidates[0]["selector_fast_path_eligible"] is False
    assert selector_candidates[0]["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": ["fetch_concept"],
        "remaining_turn_level_required_tools": ["resolve_concept_by_name"],
    }
    assert not any(item.get("concept_id") == workflow_id for item in excluded)


@pytest.mark.parametrize(
    "candidate_tool",
    [
        "search_web",
        "jira_search",
        "gmail_list_profiles",
        "search_arxiv",
    ],
)
def test_cross_surface_workflow_stays_visible_without_false_tool_coverage(
    monkeypatch,
    candidate_tool,
):
    """Unrelated search surfaces remain candidates but do not cover KB resolution."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = f"#V#{candidate_tool}_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose=f"Use {candidate_tool} on its represented surface.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": f"{candidate_tool} Workflow",
                        "description": f"Use {candidate_tool}.",
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "routing_profile": {"role": "execution"},
                        "routing_index_metadata": {"required_tools": [candidate_tool]},
                    }
                ],
                "match_count": 1,
                "contract_projection": {"required_tools": ["resolve_concept_by_name"]},
            },
            prompt="Resolve the named represented concept.",
        )
    )

    assert [item["concept_id"] for item in discovered] == [workflow_id]
    represented_candidate = next(
        item for item in selector_candidates if item.get("concept_id") == workflow_id
    )
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": [],
        "remaining_turn_level_required_tools": ["resolve_concept_by_name"],
    }
    assert represented_candidate["selector_fast_path_eligible"] is False
    assert not any(item.get("concept_id") == workflow_id for item in excluded)


@pytest.mark.parametrize("required_tool", ["fetch_concept", "add_relationship"])
def test_resolution_workflow_stays_visible_without_covering_exact_other_operation(
    monkeypatch,
    required_tool,
):
    """A launchable workflow stays visible while exact operation gaps remain clear."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#resolution_only_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Resolve a represented target without verifying or mutating it.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Resolution Only Workflow",
                        "description": "Resolve a represented target.",
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "routing_profile": {"role": "execution"},
                        "routing_index_metadata": {
                            "required_tools": ["search_concepts"]
                        },
                    }
                ],
                "match_count": 1,
                "contract_projection": {"required_tools": [required_tool]},
            },
            prompt="Complete the exact required represented operation.",
        )
    )

    assert [item["concept_id"] for item in discovered] == [workflow_id]
    represented_candidate = next(
        item for item in selector_candidates if item.get("concept_id") == workflow_id
    )
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": [],
        "remaining_turn_level_required_tools": [required_tool],
    }
    assert represented_candidate["selector_fast_path_eligible"] is False
    assert not any(item.get("concept_id") == workflow_id for item in excluded)


def test_exact_required_tool_coverage_does_not_disable_represented_fast_path(
    monkeypatch,
):
    """Only exact declared coverage can retain pre-existing fast-path eligibility."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#exact_jira_read_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Read one Jira issue through the represented integration.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Exact Jira Read Workflow",
                        "description": "Read one Jira issue.",
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "selector_fast_path_eligible": True,
                        "routing_index_metadata": {
                            "required_tools": ["jira_get_issue"]
                        },
                    }
                ],
                "match_count": 1,
                "contract_projection": {"required_tools": ["jira_get_issue"]},
            },
            prompt="Read JVNAUTOSCI-2577.",
        )
    )

    assert [item["concept_id"] for item in discovered] == [workflow_id]
    represented_candidate = selector_candidates[0]
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": ["jira_get_issue"],
        "remaining_turn_level_required_tools": [],
    }
    assert represented_candidate["selector_fast_path_eligible"] is True
    assert not any(item.get("concept_id") == workflow_id for item in excluded)


def test_workflow_execute_contract_keeps_launchable_discovered_workflow(monkeypatch):
    """A launchable represented workflow stays visible for workflow_execute."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#arxiv_paper_representation_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Represent an arXiv paper through the canonical workflow.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Arxiv Paper Representation Workflow",
                        "description": (
                            "Represent an arXiv paper through the canonical workflow."
                        ),
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "match_source": "contract_direct_workflow_resolution",
                        "routing_profile": {"role": "execution"},
                    }
                ],
                "match_count": 1,
                "contract_projection": {
                    "required_tools": ["workflow_execute"],
                    "workflow_concept_ids": [workflow_id],
                },
            },
            prompt="Please represent https://arxiv.org/abs/2406.15341 in Vontology.",
        )
    )

    assert [candidate["concept_id"] for candidate in discovered] == [workflow_id]
    assert [candidate["concept_id"] for candidate in selector_candidates][:1] == [
        workflow_id
    ]
    represented_candidate = selector_candidates[0]
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": [],
        "remaining_turn_level_required_tools": ["workflow_execute"],
    }
    assert represented_candidate["selector_fast_path_eligible"] is False
    assert not any(candidate.get("concept_id") == workflow_id for candidate in excluded)


def test_workflow_execute_with_readback_tools_keeps_discovered_workflow_candidate(
    monkeypatch,
):
    """Read-back obligations should not hide launchable represented workflows."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#source_neutral_paper_reference_ingestion_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Ingest a paper reference and produce represented artefacts.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Source Neutral Paper Reference Ingestion Workflow",
                        "description": (
                            "Canonical entry workflow for ingesting scholarly paper "
                            "references and producing represented paper artefacts."
                        ),
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "match_source": "capability_index",
                        "routing_profile": {"role": "execution"},
                    }
                ],
                "match_count": 1,
                "contract_projection": {
                    "required_tools": [
                        "workflow_execute",
                        "workflow_get_execution_trace",
                        "workflow_get_instance",
                        "rag_list_indexed",
                        "rag_get_item",
                    ],
                },
            },
            prompt=(
                "Please ingest https://arxiv.org/abs/2406.15341 into Von, then "
                "read back the represented paper concept, file-copy, and blob evidence."
            ),
        )
    )

    assert [candidate["concept_id"] for candidate in discovered] == [workflow_id]
    assert [candidate["concept_id"] for candidate in selector_candidates][:1] == [
        workflow_id
    ]
    represented_candidate = selector_candidates[0]
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": [],
        "remaining_turn_level_required_tools": [
            "workflow_execute",
            "workflow_get_execution_trace",
            "workflow_get_instance",
            "rag_list_indexed",
            "rag_get_item",
        ],
    }
    assert represented_candidate["selector_fast_path_eligible"] is False
    assert not any(candidate.get("concept_id") == workflow_id for candidate in excluded)


def test_contract_named_launchable_workflow_survives_internal_tool_contract_shape(
    monkeypatch,
):
    """Expected-outcome internal-tool wording must not veto workflow initiation."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#contract_named_paper_ingestion_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Ingest a paper reference through a represented workflow.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Contract Named Paper Ingestion Workflow",
                        "description": (
                            "Ingest a paper reference through a represented workflow."
                        ),
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": True,
                        "candidate_source": "workflow_discovery",
                        "match_source": "contract_direct_workflow_resolution",
                        "routing_profile": {"role": "execution"},
                    }
                ],
                "match_count": 1,
                "contract_projection": {
                    "required_tools": [
                        "paper:download_source",
                        "paper:finalise_cached_source",
                        "vontology:read_file_copy",
                    ],
                    "conditional_required_tools": ["workflow_execute"],
                    "workflow_concept_ids": [workflow_id],
                },
            },
            prompt=(
                "Please ingest this paper into Von, then read back the represented "
                "paper concept, file-copy, and blob evidence."
            ),
        )
    )

    assert [candidate["concept_id"] for candidate in discovered] == [workflow_id]
    assert [candidate["concept_id"] for candidate in selector_candidates][:1] == [
        workflow_id
    ]
    represented_candidate = selector_candidates[0]
    assert represented_candidate["candidate_reason"] == (
        "discovered_workflow_candidate"
    )
    assert represented_candidate["required_tool_coverage"] == {
        "coverage_basis": "exact_declared_tool_identity_advisory",
        "covered_required_tools": [],
        "remaining_turn_level_required_tools": [
            "paper:download_source",
            "paper:finalise_cached_source",
            "vontology:read_file_copy",
        ],
    }
    assert represented_candidate["selector_fast_path_eligible"] is False
    assert not any(candidate.get("concept_id") == workflow_id for candidate in excluded)


def test_contract_named_nonlaunchable_workflow_still_fails_required_tool_gate(
    monkeypatch,
):
    """Contract naming is not enough when the workflow cannot launch this turn."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    workflow_id = "#V#contract_named_unlaunchable_ingestion_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=workflow_id,
        purpose="Ingest a paper reference through a represented workflow.",
    )

    discovered, excluded, selector_candidates = (
        orchestrator._prepare_selector_candidates(
            workflow_discovery_result={
                "matches": [
                    {
                        "concept_id": workflow_id,
                        "name": "Contract Named Unlaunchable Ingestion Workflow",
                        "description": (
                            "Ingest a paper reference through a represented workflow."
                        ),
                        "is_executable": True,
                        "executability_reason": "executable_now",
                        "is_policy_safe": True,
                        "routing_eligible": True,
                        "turn_launchable": False,
                        "candidate_source": "workflow_discovery",
                        "match_source": "contract_direct_workflow_resolution",
                        "routing_profile": {"role": "execution"},
                    }
                ],
                "match_count": 1,
                "contract_projection": {
                    "required_tools": [
                        "paper:download_source",
                        "paper:finalise_cached_source",
                    ],
                    "conditional_required_tools": ["workflow_execute"],
                    "workflow_concept_ids": [workflow_id],
                },
            },
            prompt="Please ingest the relevant paper if you can resolve it.",
        )
    )

    assert not any(
        candidate.get("concept_id") == workflow_id for candidate in discovered
    )
    assert not any(
        candidate.get("concept_id") == workflow_id for candidate in selector_candidates
    )
    assert any(
        candidate.get("concept_id") == workflow_id
        and candidate.get("routing_exclusion_reason")
        == "required_tool_contract_not_satisfied"
        for candidate in excluded
    )


def test_zero_candidate_capability_index_blocker_forces_discovery_refresh(
    monkeypatch,
):
    """A transiently unavailable discovery surface must not become selector truth."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    stale_discovery = {
        "query": "Represent this arXiv paper.",
        "requested_query": "Represent this arXiv paper.",
        "matches": [],
        "candidates": [],
        "candidate_count": 0,
        "match_count": 0,
        "errors": [
            "capability_index_wait_timed_out",
            "capability_index_build_in_progress",
        ],
        "match_absence_reason": "capability_index_wait_timed_out_build_in_progress",
        "stage_timings": [
            {
                "stage": "capability_index_unavailable",
                "errors": [
                    "capability_index_wait_timed_out",
                    "capability_index_build_in_progress",
                ],
            }
        ],
    }

    assert orchestrator._should_refresh_turn_workflow_discovery_result(
        workflow_discovery_result=stale_discovery,
        requested_query="Represent this arXiv paper.",
        discovery_query_input="Represent this arXiv paper.",
    )


def test_clean_zero_candidate_discovery_can_be_reused(monkeypatch):
    """A real no-match result is different from operational unavailability."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    clean_no_match = {
        "query": "hello",
        "requested_query": "hello",
        "matches": [],
        "candidates": [],
        "candidate_count": 0,
        "match_count": 0,
        "match_absence_reason": "no_relevant_workflow",
    }

    assert not orchestrator._should_refresh_turn_workflow_discovery_result(
        workflow_discovery_result=clean_no_match,
        requested_query="hello",
        discovery_query_input="hello",
    )


def test_top_level_selector_applies_represented_fast_path_before_llm(monkeypatch):
    """Represented candidate policy should route the top-level path without selector LLM drift."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    custom_workflow_id = "#V#custom_contract_covering_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=custom_workflow_id,
        purpose="Runs a custom external-source scan and representation workflow.",
    )
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=custom_workflow_id,
        data={"final_response": "Represented fast-path workflow completed."},
    )

    candidate = {
        "concept_id": custom_workflow_id,
        "name": "Custom Contract Covering Workflow",
        "description": "Runs a custom external-source scan and representation workflow.",
        "relevance_score": 0.98,
        "is_executable": True,
        "is_policy_safe": True,
        "routing_eligible": True,
        "candidate_source": "workflow_discovery",
        "selector_fast_path_policy": {
            "enabled": True,
            "rule": "unique_candidate_covers_contract",
            "authority_source": "repo_seed_text_relation:#V#hasWorkflowSelectorFastPathPolicyJson",
            "require_contract_coverage": True,
        },
        "covers_expected_tool_set": True,
        "covers_success_contract": True,
        "satisfies_expected_outcome_contract": True,
        "selector_fast_path_eligible": True,
        "turn_launchable": True,
    }
    llm = _CapturingLLM([])

    result = orchestrator.run(
        prompt="Run the represented external-source scan.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [candidate],
            "candidates": [candidate],
            "match_count": 1,
            "selector_fast_path_policy": candidate["selector_fast_path_policy"],
        },
    )

    assert llm.calls == []
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == custom_workflow_id
    assert result.workflow_routing.source == "represented_fast_path"
    assert result.response_text == "Represented fast-path workflow completed."

    fast_path_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict)
        and e.get("type") == "workflow_selector_represented_fast_path"
    )
    assert fast_path_entry["applied"] is True
    assert fast_path_entry["selected_workflow_id"] == custom_workflow_id

    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == custom_workflow_id
    assert selector_entry["selection_source"] == "represented_fast_path"
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "represented_fast_path"
    )


def test_agent_test_top_level_selector_reuses_authoritative_discovery_candidate(
    monkeypatch,
):
    """AgentTest local replay should not let the selector LLM reorder discovery."""

    monkeypatch.setenv("VON_AGENT_TEST_INSTANCE", "1")
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    custom_workflow_id = "#V#custom_authoritative_discovery_workflow"
    _register_terminal_custom_workflow(
        orchestrator,
        workflow_id=custom_workflow_id,
        purpose="Runs an authoritative discovered workflow for local replay.",
    )
    _stub_execute_workflow_result(
        monkeypatch,
        orchestrator,
        expected_workflow_id=custom_workflow_id,
        data={
            "final_response": "AgentTest authoritative discovery workflow completed."
        },
    )

    candidate = {
        "concept_id": custom_workflow_id,
        "name": "Custom Authoritative Discovery Workflow",
        "description": "Runs an authoritative discovered workflow for local replay.",
        "relevance_score": 0.98,
        "is_executable": True,
        "is_policy_safe": True,
        "routing_eligible": True,
        "candidate_source": "workflow_discovery",
        "turn_launchable": True,
    }
    llm = _CapturingLLM([])

    result = orchestrator.run(
        prompt="Run the authoritative discovered local replay workflow.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result={
            "matches": [candidate],
            "candidates": [candidate],
            "match_count": 1,
        },
    )

    assert llm.calls == []
    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == custom_workflow_id
    assert result.workflow_routing.source == "agent_test_authoritative_discovery"
    assert (
        result.response_text == "AgentTest authoritative discovery workflow completed."
    )

    fast_path_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict)
        and e.get("type") == "workflow_selector_agent_test_authoritative_discovery"
    )
    assert fast_path_entry["selected_workflow_id"] == custom_workflow_id

    selector_entry = next(
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_selector"
    )
    assert selector_entry["workflow_id"] == custom_workflow_id
    assert selector_entry["selection_source"] == "agent_test_authoritative_discovery"
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "agent_test_authoritative_discovery"
    )


def test_non_executable_discovered_workflow_filtered_by_default(monkeypatch):
    """Non-executable discovered workflows should not reach selector candidates by default."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE", "0")

    llm = _CapturingLLM(
        [
            TODO_REFRESH_WORKFLOW_ID.lower(),  # selector verdict (ignored)
            "Fallback response.",
        ]
    )

    discovery_result = {
        "candidates": [
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo Refresh Workflow",
                "description": "Refreshes user's todo list from Jira.",
                "is_executable": False,
                "executability_reason": "graph_incomplete",
            }
        ],
        "candidate_count": 1,
    }

    result = orchestrator.run(
        prompt="Refresh my todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    discovered_ids = set(selector_entry.get("discovered_workflow_ids", []))
    assert TODO_REFRESH_WORKFLOW_ID not in discovered_ids
    assert {
        CHAT_ASSISTANT_WORKFLOW_ID,
        TOOL_CALLING_WORKFLOW_ID,
        CHAT_NARRATION_WORKFLOW_ID,
    }.issubset(discovered_ids)
    assert selector_entry.get("discovery_excluded_count") == 1

    execution_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_execution"
        ),
        None,
    )
    assert execution_entry is None


def test_non_executable_discovered_workflow_can_be_overridden(monkeypatch):
    """Explicit override should allow non-executable discovered workflows into selector context."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    monkeypatch.setenv("VON_WORKFLOW_SELECTOR_ALLOW_NON_EXECUTABLE", "1")

    llm = _CapturingLLM(
        [
            TODO_REFRESH_WORKFLOW_ID.lower(),  # selector verdict
        ]
    )

    discovery_result = {
        "candidates": [
            {
                "concept_id": TODO_REFRESH_WORKFLOW_ID,
                "name": "Todo Refresh Workflow",
                "description": "Refreshes user's todo list from Jira.",
                "is_executable": False,
                "executability_reason": "graph_incomplete",
            }
        ],
        "candidate_count": 1,
    }

    result = orchestrator.run(
        prompt="Refresh my todo list",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
    )

    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert TODO_REFRESH_WORKFLOW_ID in selector_entry.get("discovered_workflow_ids", [])

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


def test_workflow_selector_emits_dispatch_progress_events(monkeypatch):
    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "guidance_mode": "none",
            "recommended_workflow_id": None,
            "candidate_scores": [],
        },
    )
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    llm = _CapturingLLM([TOOL_CALLING_WORKFLOW_ID, "Fallback response."])
    captured_progress: list[dict[str, Any]] = []
    tracker = ProgressTracker(
        callback=lambda info: captured_progress.append(dict(info))
    )

    discovery_result = {
        "matches": [
            {
                "concept_id": TOOL_CALLING_WORKFLOW_ID,
                "name": "Tool calling workflow",
                "description": "Default tool-calling route.",
                "is_executable": True,
                "executability_reason": "executable_now",
            }
        ],
        "candidates": [
            {
                "concept_id": TOOL_CALLING_WORKFLOW_ID,
                "name": "Tool calling workflow",
                "description": "Default tool-calling route.",
                "is_executable": True,
                "executability_reason": "executable_now",
            }
        ],
        "match_count": 1,
        "candidate_count": 1,
    }

    result = orchestrator.run(
        prompt="Use the best workflow for this request.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        workflow_discovery_result=discovery_result,
        progress_tracker=tracker,
    )

    workflow_dispatch_events = [
        entry
        for entry in captured_progress
        if entry.get("stage") == "workflow_dispatch"
    ]
    assert workflow_dispatch_events
    assert any(
        entry.get("phase_label") == "Selecting workflow"
        and entry.get("workflow_candidate_count") == 3
        for entry in workflow_dispatch_events
    )
    assert any(
        entry.get("status") == "llm_call_start" for entry in workflow_dispatch_events
    )
    assert any(
        entry.get("status") == "llm_call_end" and entry.get("success") is True
        for entry in workflow_dispatch_events
    )
    selected_event = next(
        entry
        for entry in workflow_dispatch_events
        if entry.get("workflow_selector_verdict") == "rag_selected"
        and entry.get("status") == "thinking"
    )
    assert selected_event.get("phase_label") == "Workflow selected"
    assert selected_event.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert any(
        entry.get("phase") == "workflow_dispatch"
        and entry.get("selected_workflow_id") == TOOL_CALLING_WORKFLOW_ID
        and entry.get("goal_label")
        == f"Execute {selected_event.get('selected_workflow_name')}."
        for entry in captured_progress
    )
    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 2.2: Non-standard workflow routing via execute_workflow.
# ---------------------------------------------------------------------------


def test_selector_disabled_skips_classifier(monkeypatch):
    """When selector is disabled, no classifier LLM call should be made."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)

    llm = _CapturingLLM(["A direct response."])

    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert llm.calls
    assert llm.calls[0]["prompt"] == "Hello"
    assert all(
        not str(call["prompt"]).startswith("Select workflow") for call in llm.calls
    )

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" not in aux_types


# ---------------------------------------------------------------------------
# JVNAUTOSCI-922 Phase 1.3: No user_namespace → selector skipped.
# ---------------------------------------------------------------------------


def test_no_namespace_skips_selector(monkeypatch):
    """Without user_namespace, selector should not fire even when enabled."""

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(["Response without selector."])

    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model=None,
        # No user_namespace — selector should be skipped.
    )

    assert llm.calls
    assert llm.calls[0]["prompt"] == "Hello"
    assert all(
        not str(call["prompt"]).startswith("Select workflow") for call in llm.calls
    )

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" not in aux_types


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Plain response routing (skips tool-calling overhead).
# ---------------------------------------------------------------------------


def test_tool_seeking_has_routing_info(monkeypatch):
    """Tool-calling path should also include WorkflowRoutingInfo."""
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            TOOL_CALLING_WORKFLOW_ID,  # selector verdict
            "Let me search for that.",  # plan handler response (no tools found)
        ]
    )

    result = orchestrator.run(
        prompt="Search for transformers papers",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == TOOL_CALLING_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert isinstance(result.workflow_routing.selection_rationale, str)

    selector_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert selector_entry.get("prompt", {}).get("text")
    assert TOOL_CALLING_WORKFLOW_ID in str(
        selector_entry.get("response", {}).get("text") or ""
    )
    assert isinstance(selector_entry.get("candidate_entries"), list)
    assert any(
        isinstance(entry, dict) and entry.get("concept_id") == TOOL_CALLING_WORKFLOW_ID
        for entry in selector_entry.get("candidate_entries", [])
    )

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    assert [entry.get("boundary") for entry in dispatch_boundaries[:3]] == [
        "execution_mode_selected",
        "contract_resolution",
        "workflow_handoff",
    ]
    assert dispatch_boundaries[-1].get("boundary") == "workflow_terminal"
    assert dispatch_boundaries[-1].get("selected_execution_mode") == "tool_pipeline"
    assert dispatch_boundaries[-1].get("dispatch_workflow_id")


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Routing timing telemetry.
# ---------------------------------------------------------------------------


def test_routing_duration_ms_in_aux_llm_calls(monkeypatch):
    """Routing telemetry should include timing in aux_llm_calls."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "Quick reply.",
        ]
    )

    result = orchestrator.run(
        prompt="Hi",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    selector_entry = next(
        (
            e
            for e in result.aux_llm_calls
            if isinstance(e, dict) and e.get("type") == "workflow_selector"
        ),
        None,
    )
    assert selector_entry is not None
    assert "routing_duration_ms" in selector_entry
    assert isinstance(selector_entry["routing_duration_ms"], float)
    assert selector_entry["routing_duration_ms"] >= 0
    prepare_steps = [
        e
        for e in result.aux_llm_calls
        if isinstance(e, dict) and e.get("type") == "workflow_dispatch_prepare_step"
    ]
    assert prepare_steps
    assert any(
        step.get("step_id") == "selector_candidate_preparation"
        for step in prepare_steps
    )
    assert all(
        step.get("stage") == "workflow_dispatch_prepare" for step in prepare_steps
    )
    assert all(isinstance(step.get("duration_ms"), int) for step in prepare_steps)


# ---------------------------------------------------------------------------
# JVNAUTOSCI-825: Selector default-on behaviour.
# ---------------------------------------------------------------------------


def test_selector_enabled_by_default(monkeypatch):
    """The selector remains enabled without any compatibility toggle."""
    monkeypatch.delenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", raising=False)

    selector = WorkflowSelector(
        registry=MagicMock(),
        prompt_service=MagicMock(),
    )
    assert selector.enabled()


def test_selector_ignores_legacy_disable_env(monkeypatch):
    """Legacy selector env toggles no longer affect runtime routing."""
    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "0")

    selector = WorkflowSelector(
        registry=MagicMock(),
        prompt_service=MagicMock(),
    )
    assert selector.enabled()


def test_no_routing_info_when_selector_suppressed_in_harness(monkeypatch):
    """Harness-level selector suppression should omit workflow_routing data."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=False)

    llm = _CapturingLLM(["A direct response."])

    result = orchestrator.run(
        prompt="Hello",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is None
