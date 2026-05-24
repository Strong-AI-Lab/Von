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
