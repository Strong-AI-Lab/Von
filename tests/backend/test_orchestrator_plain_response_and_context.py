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


def test_plain_response_skips_tool_calling(monkeypatch):
    """When the classifier returns 'plain_response', the orchestrator should
    generate a direct LLM response without invoking the tool-calling workflow.
    This means only 2 LLM calls: selector + planner (no plan handler overhead).
    """
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,  # selector verdict
            "Hello! How can I help?",  # direct planner response
        ]
    )

    result = orchestrator.run(
        prompt="Hi there",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    # Exactly 2 LLM calls: selector + planner.
    assert len(llm.calls) == 2
    assert llm.calls[0]["prompt"].startswith("Select workflow")
    assert "Hi there" in llm.calls[0]["prompt"]
    # The planner prompt should be the user's prompt, not a tool-call prompt.
    assert llm.calls[1]["prompt"] == "Hi there"

    assert result.response_text == "Hello! How can I help?"
    # No tool invocations for a plain response.
    assert result.tool_invocations == ()
    assert result.extra_messages == ()


def test_plain_response_has_routing_info(monkeypatch):
    """Plain response should include WorkflowRoutingInfo in the result."""
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,  # selector verdict
            "Just a chat reply.",  # planner response
        ]
    )

    result = orchestrator.run(
        prompt="Tell me a joke",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert isinstance(result.workflow_routing, WorkflowRoutingInfo)
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"

    dispatch_boundaries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_dispatch_boundary"
    ]
    selected_boundary = next(
        (
            entry
            for entry in dispatch_boundaries
            if entry.get("boundary") == "execution_mode_selected"
        ),
        None,
    )
    assert selected_boundary is not None
    assert selected_boundary.get("selected_execution_mode") == "direct_response"
    assert selected_boundary.get("selected_execution_mode_authority_source")
    assert dispatch_boundaries[-1].get("boundary") == "workflow_terminal"
    assert dispatch_boundaries[-1].get("selected_execution_mode") == "direct_response"


def test_plain_response_includes_turn_memory_and_policy_memory_context(monkeypatch):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        orchestrator_module,
        "build_turn_memory_context_state",
        lambda **_kwargs: {
            "schema_version": "conversation_turn_memory_context.v1",
            "status": "available",
            "fail_closed": False,
            "subject_contexts": [
                {
                    "status": "available",
                    "subject_role": "user",
                    "context_dossier_id": "#V#user_turn_dossier",
                    "workspace_fingerprint": "workspace-fp-1",
                }
            ],
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "render_turn_memory_context_messages",
        lambda _state: [
            {"role": "system", "content": "TURN MEMORY MESSAGE: identity bundle ready"}
        ],
    )
    monkeypatch.setattr(
        orchestrator_module,
        "build_selected_workflow_policy_memory_state",
        lambda **_kwargs: {
            "schema_version": "selected_workflow_policy_memory.v1",
            "status": "available",
            "selected_workflow_id": CHAT_ASSISTANT_WORKFLOW_ID,
            "suggestion_count": 1,
            "suggestions": [
                {
                    "memory_id": "#V#policy_memory_1",
                    "suggestion_id": "#V#workflow_suggestion_1",
                    "priority": "high",
                    "category": "grounding",
                    "title": "Prefer grounded identity evidence before answering",
                    "rationale": "Recent evaluated turns regressed into stale identity claims.",
                }
            ],
        },
    )

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "Grounded direct response.",
        ]
    )

    result = orchestrator.run(
        prompt="Who am I?",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        user_concept_id="#V#test_user",
    )

    assert result.response_text == "Grounded direct response."
    assert len(llm.calls) == 2
    direct_response_context = llm.calls[1]["context"]
    assert any(
        isinstance(message, dict)
        and "TURN MEMORY MESSAGE: identity bundle ready"
        in str(message.get("content") or "")
        for message in direct_response_context
    )
    assert any(
        isinstance(message, dict)
        and "RECENT POLICY MEMORY FOR #V#chat_assistant_workflow:"
        in str(message.get("content") or "")
        for message in direct_response_context
    )
    assert any(
        entry.get("type") == "turn_memory_context" for entry in result.aux_llm_calls
    )
    assert any(
        entry.get("type") == "selected_workflow_policy_memory"
        for entry in result.aux_llm_calls
    )


def test_run_fails_closed_when_requested_turn_memory_context_is_unavailable(
    monkeypatch,
):
    import src.backend.services.workflow_selection_policy_service as policy_module

    monkeypatch.setattr(policy_module, "get_live_selection_policy", lambda: None)
    monkeypatch.setattr(
        orchestrator_module,
        "build_turn_memory_context_state",
        lambda **_kwargs: {
            "schema_version": "conversation_turn_memory_context.v1",
            "status": "unavailable",
            "fail_closed": True,
            "failure_reason": "requested_context_dossier_missing",
            "subject_contexts": [],
            "requested_memory_context": {
                "context_dossier_id": "#V#missing_dossier",
            },
        },
    )

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    llm = _CapturingLLM([])

    result = orchestrator.run(
        prompt="Answer using the requested dossier only.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
        turn_memory_context={"context_dossier_id": "#V#missing_dossier"},
    )

    assert llm.calls == []
    assert (
        result.response_text
        == "I couldn't use the authoritative memory context requested for this turn because the requested context dossier was not available."
    )
    assert any(
        entry.get("type") == "turn_memory_context" for entry in result.aux_llm_calls
    )


def test_workflow_selector_uses_provider_aware_classifier_fallback(monkeypatch):
    """Selector classification should honour provider-aware stage candidates.

    The classifier policy may prefer a local Ollama model for cheap routing.
    When that candidate is unreachable, workflow dispatch must fall back to the
    next candidate without trying the Ollama model name through the default
    OpenAI client.
    """

    monkeypatch.setattr(
        "src.backend.workflows.workflow_selector.recommend_workflow_with_policy",
        lambda **_kwargs: {
            "guidance_mode": "none",
            "recommended_workflow_id": None,
            "candidate_scores": [],
        },
    )

    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    llm = _CapturingLLM(["Hello! How can I help?"])

    ollama_candidate = _ModelCandidate(
        provider="ollama",
        model="granite3.3:2b",
        raw="ollama:granite3.3:2b",
        source="policy",
        host="http://localhost:11434",
    )
    openai_candidate = _ModelCandidate(
        provider="openai",
        model="gpt-5.2-chat-latest",
        raw="openai:gpt-5.2-chat-latest",
        source="policy",
    )

    original_stage_model_candidates = orchestrator._stage_model_candidates
    original_create_client_for_candidate = orchestrator._create_client_for_candidate

    monkeypatch.setattr(
        orchestrator,
        "_stage_model_candidates",
        lambda **kwargs: (
            [ollama_candidate, openai_candidate]
            if kwargs.get("stage") == "classifier"
            else original_stage_model_candidates(**kwargs)
        ),
    )

    class _SelectorFallbackClient:
        def generate(self, *_args: Any, **_kwargs: Any) -> str:
            return CHAT_ASSISTANT_WORKFLOW_ID

    def _create_client_for_candidate(
        candidate: _ModelCandidate,
        **kwargs: Any,
    ) -> tuple[Any, str | None, Mapping[str, Any]]:
        if candidate.provider == "ollama":
            return (
                object(),
                "granite3.3:2b",
                {
                    "provider": "ollama",
                    "model": "granite3.3:2b",
                    "raw": candidate.raw,
                    "source": candidate.source,
                    "host": "http://localhost:11434",
                },
            )
        if candidate.provider == "openai":
            return (
                _SelectorFallbackClient(),
                "gpt-5.2-chat-latest",
                {
                    "provider": "openai",
                    "model": "gpt-5.2-chat-latest",
                    "raw": candidate.raw,
                    "source": candidate.source,
                    "host": None,
                },
            )
        return original_create_client_for_candidate(candidate, **kwargs)

    monkeypatch.setattr(
        orchestrator,
        "_create_client_for_candidate",
        _create_client_for_candidate,
    )
    monkeypatch.setattr(
        orchestrator,
        "_probe_model_candidate_reachability",
        lambda *, telemetry: (
            {
                "provider": "ollama",
                "host": "http://localhost:11434",
                "probe_url": "http://localhost:11434/api/tags",
                "probe_timeout_ms": 1200,
                "duration_ms": 7,
                "reachable": False,
                "error": "connection refused",
                "error_class": "ConnectionError",
            }
            if telemetry.get("provider") == "ollama"
            else None
        ),
    )

    captured_progress: list[dict[str, Any]] = []
    tracker = ProgressTracker(
        callback=lambda info: captured_progress.append(dict(info))
    )

    result = orchestrator.run(
        prompt="Hi there",
        context=[],
        llm_client=llm,
        model="gpt-5.2-chat-latest",
        user_namespace="#V#user",
        progress_tracker=tracker,
    )

    assert result.response_text == "Hello! How can I help?"
    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Hi there"

    workflow_dispatch_events = [
        entry
        for entry in captured_progress
        if entry.get("stage") == "workflow_dispatch"
    ]
    failed_attempt = next(
        entry
        for entry in workflow_dispatch_events
        if entry.get("status") == "llm_call_end"
        and entry.get("fallback_attempt_no") == 1
    )
    assert failed_attempt["success"] is False
    assert failed_attempt["failure_kind"] == "provider_unreachable"
    assert failed_attempt["provider"] == "ollama"

    succeeded_attempt = next(
        entry
        for entry in workflow_dispatch_events
        if entry.get("status") == "llm_call_end"
        and entry.get("fallback_attempt_no") == 2
    )
    assert succeeded_attempt["success"] is True
    assert succeeded_attempt["fallback_used"] is True
    assert succeeded_attempt["provider"] == "openai"

    stage_summary = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_model_policy_stage"
        and entry.get("stage") == "workflow_dispatch"
    )
    assert stage_summary["policy_stage"] == "classifier"
    assert stage_summary["selected"]["provider"] == "openai"

    selector_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector"
    )
    assert selector_entry["policy_stage"] == "classifier"
    assert selector_entry["candidate"]["provider"] == "openai"
    assert selector_entry["model_name"] == "gpt-5.2-chat-latest"
    assert selector_entry["prompt_provenance"]["prompt_mode"] == (
        "rag_first_candidate_selector"
    )
    assert selector_entry["requested_prompt_ids"] == ["#V#chat_turn_classifier_prompt"]
    assert selector_entry["candidate_list"]["text"]
    assert selector_entry["selection_metadata"]["selection_resolution"] == (
        "candidate_label_exact_match"
    )
    assert selector_entry["response"]["text"] == CHAT_ASSISTANT_WORKFLOW_ID

    selector_prompt_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    assert selector_prompt_entry["prompt"]["text"]
    assert selector_prompt_entry["candidate_entries"]

    assert stage_summary["fallback_attempts"][0]["failure_kind"] == (
        "provider_unreachable"
    )
    assert stage_summary["fallback_attempts"][1]["response"]["text"] == (
        CHAT_ASSISTANT_WORKFLOW_ID
    )


def test_workflow_selector_reuses_augmented_context_and_tracks_context_lineage(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)
    base_context = [
        {
            "role": "system",
            "content": "CURRENT USER CONTEXT: Test User (#V#test_user)",
        },
        {"role": "assistant", "content": "Earlier context that still matters."},
        {"role": "user", "content": "What is my name?"},
    ]
    monkeypatch.setattr(
        orchestrator,
        "_build_augmented_context",
        lambda *args, **kwargs: list(base_context),
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "Your name is Test User.",
        ]
    )

    result = orchestrator.run(
        prompt="What is my name?",
        context=[],
        llm_client=llm,
        model="test-model",
        user_namespace="#V#user",
    )

    assert result.response_text == "Your name is Test User."
    selector_context = llm.calls[0]["context"]
    assert any(
        isinstance(message, dict)
        and message.get("content") == "CURRENT USER CONTEXT: Test User (#V#test_user)"
        for message in selector_context
    )
    assert any(
        isinstance(message, dict)
        and message.get("role") == "user"
        and message.get("content") == "What is my name?"
        for message in selector_context
    )

    stage_summary = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "workflow_model_policy_stage"
        and entry.get("stage") == "workflow_dispatch"
    )
    request = stage_summary["request"]
    assert request["context_lineage"]["base_context_source"] == "augmented_context"
    assert request["context_lineage"]["stage_added_message_count"] == 1
    assert request["context_lineage"]["stage_added_messages"][0]["role"] == "system"
    assert request["context_lineage"]["base_context_summary"]["message_count"] == 3
    assert request["context_summary"]["message_count"] == len(selector_context)
    assert request["context_summary"]["role_counts"]["system"] >= 2

    selector_prompt_entry = next(
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "workflow_selector_prompt"
    )
    assert selector_prompt_entry["context_lineage"]["base_context_source"] == (
        "augmented_context"
    )
    assert selector_prompt_entry["context_lineage"]["stage_added_message_count"] == 1


def test_mutative_wording_does_not_override_plain_response_routing(monkeypatch):
    """Mutative wording alone must not trigger Python-side routing overrides."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    # Write-policy checks in orchestrator should only keep this on the
    # tool-calling path when mutative intent is explicitly requested.
    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"add_relationship": {"category": "write"}},
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,  # selector verdict
            "Plain response only.",
        ]
    )

    result = orchestrator.run(
        prompt="Create a concept link in Vontology.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"
    assert result.response_text == "Plain response only."
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in result.aux_llm_calls
    )
    gate_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "write_policy_gate"
            and entry.get("stage") == "routing"
        ),
        None,
    )
    assert gate_entry is not None
    assert gate_entry.get("gate_state") == "not_applied"
    assert gate_entry.get("reason") == "workflow_llm_owns_mutation_routing"


def test_explicit_tool_requirement_is_telemetry_visible_even_without_python_routing_override(
    monkeypatch,
):
    """Explicit tool mentions should be surfaced in telemetry without forcing routing."""
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"workflow_list_definitions": {"category": "read"}},
    )
    monkeypatch.setattr(
        orchestrator._gateway,
        "invoke",
        lambda _tool_name, _payload: _InvokeResult({"workflows": []}),
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "I inspected workflow definitions.",
        ]
    )

    result = orchestrator.run(
        prompt="Call workflow_list_definitions and confirm what exists.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.source == "selector"

    requirement_entry = next(
        (
            entry
            for entry in result.aux_llm_calls
            if isinstance(entry, dict)
            and entry.get("type") == "prompt_tool_requirements_preflight"
            and entry.get("stage") == "workflow_dispatch"
        ),
        None,
    )
    assert requirement_entry is not None
    assert requirement_entry.get("required_tools") == ["workflow_list_definitions"]
    assert requirement_entry.get("missing_tools") == ["workflow_list_definitions"]
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in result.aux_llm_calls
    )


def test_incidental_url_prompt_stays_on_plain_response_path(monkeypatch):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"resilient_extract_url": {"category": "read"}},
    )

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "Meeting noted.",
        ]
    )

    result = orchestrator.run(
        prompt="Meeting notice: Zoom link https://example.com/join/abc for tomorrow's call.",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.tool_invocations == ()
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "workflow_selector_override"
        and entry.get("reason") == "required_prompt_tools_missing_preselector"
        for entry in result.aux_llm_calls
    )


def test_url_read_prompt_stays_selector_owned_without_python_url_preselection(
    monkeypatch,
):
    orchestrator = _build_orchestrator(monkeypatch, selector_enabled=True)

    gateway_calls: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        orchestrator._gateway,
        "describe_methods",
        lambda: {"resilient_extract_url": {"category": "read"}},
    )

    def _invoke(tool_name: str, payload: Mapping[str, Any]):
        gateway_calls.append((tool_name, dict(payload)))
        return _InvokeResult(
            {
                "success": True,
                "url": payload.get("url"),
                "title": "Example report",
                "content": "Example report body",
            }
        )

    monkeypatch.setattr(orchestrator._gateway, "invoke", _invoke)

    llm = _CapturingLLM(
        [
            CHAT_ASSISTANT_WORKFLOW_ID,
            "I can help with that.",
        ]
    )

    result = orchestrator.run(
        prompt="Please read this: https://example.com/report",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.workflow_routing is not None
    assert result.workflow_routing.workflow_id == CHAT_ASSISTANT_WORKFLOW_ID
    assert result.workflow_routing.verdict == "rag_selected"
    assert result.workflow_routing.source == "selector"
    assert gateway_calls == []
    assert result.tool_invocations == ()

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
    assert preflight_entry.get("required_tools") == []
    assert preflight_entry.get("required_url_extraction_tool") is None
    assert preflight_entry.get("required_url_extraction_url") is None
    assert not any(
        isinstance(entry, dict) and entry.get("type") == "workflow_selector_override"
        for entry in result.aux_llm_calls
    )
