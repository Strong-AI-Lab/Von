from __future__ import annotations

from typing import Any, cast

from flask import Flask

from orchestrator_test_harness import (
    _stub_stage_model_snapshot,
    _stub_stage_path,
    build_db_independent_orchestrator,
)
from src.backend.workflows.definitions import CHAT_ASSISTANT_WORKFLOW_ID


class _IdentityLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if isinstance(prompt, str) and "expected-success inference policy" in prompt:
            return (
                '{"expected_outcome_summary":"Answer from grounded identity context only.",'
                '"grounding_requirement":"Only state identity details grounded in the authenticated context.",'
                '"precision_policy":"Prefer explicit uncertainty over speculation.",'
                '"selector_guidance":"Prefer retrieval or verification only when grounded identity context is insufficient.",'
                '"answering_guidance":"If grounded identity context is available, answer directly and concisely.",'
                '"reasoning":"Authenticated identity questions should be answered from grounded actor or organisation context."}'
            )
        if isinstance(prompt, str) and prompt.strip() == "Select workflow":
            return (
                '{"workflow_id":"#V#chat_assistant_workflow",'
                '"confidence":0.91,'
                '"reasoning":"The authenticated identity request is a grounded direct-response turn."}'
            )
        if isinstance(prompt, str) and "organisation" in prompt.lower():
            return "You are in Test Org (#V#test_org)."
        return "You are Test User (#V#test_user)."


class _GatewayStub:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def _make_app(monkeypatch, *, llm: _IdentityLLM) -> Flask:
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(registry_factory, "discover_workflow_ids", lambda: [])
    monkeypatch.setattr(
        registry_factory, "_launch_deferred_registry_work", lambda **_kwargs: None
    )

    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_path",
        _stub_stage_path,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_show_tool_use_during_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_buttonify_model_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        lambda window_session_id, session_snapshot, user_concept_id: {
            "organisation_id": "#V#test_org",
            "chat_session_id": "window-session",
            "role": "member",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: {
            "query": "Who am I?",
            "requested_query": "Who am I?",
            "search_sources": ["capability_index"],
            "candidate_count": 1,
            "match_count": 1,
            "matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
            "candidates": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
            "routing_matches": [
                {
                    "concept_id": CHAT_ASSISTANT_WORKFLOW_ID,
                    "name": "Chat Assistant Workflow",
                    "description": "General conversational workflow.",
                    "is_executable": True,
                    "executability_reason": "executable_now",
                    "is_policy_safe": True,
                    "routing_eligible": True,
                    "routing_profile": {"role": "execution"},
                }
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        lambda concept_id: {
            "#V#test_user": {"name": "Test User"},
            "#V#test_org": {"name": "Test Org"},
        }.get(concept_id),
    )

    gateway = _GatewayStub()
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, gateway),
        selector_enabled=True,
        max_tool_invocations=1,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = True
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.register_blueprint(von_bp, url_prefix="/von")
    return app


def test_generate_authenticated_identity_turn_uses_direct_response_and_records_plain_response_telemetry(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "Who am I?"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "You are Test User (#V#test_user)."

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("verdict") in {"rag_selected", "rag_default"}
    assert workflow_routing.get("source") == "selector"

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    workflow_routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    dispatch = workflow_routing_diagnostics.get("dispatch") or {}
    assert dispatch.get("selected_execution_mode") == "direct_response"
    assert dispatch.get("dispatch_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert dispatch.get("dispatch_terminal_failure_reason") in {None, ""}

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert completion_report.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert completion_report.get("response_text") == "You are Test User (#V#test_user)."
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    assert selected_workflow_trace.get("child_workflow_final_state") == "plain_response"

    assert len(llm.calls) >= 2


def test_generate_authenticated_organisation_turn_uses_direct_response_and_preserves_org_context_telemetry(
    monkeypatch,
) -> None:
    llm = _IdentityLLM()
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post(
        "/von/generate", json={"prompt": "Which organisation am I in?"}
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert body.get("response") == "You are in Test Org (#V#test_org)."

    llm_debug = body.get("llm_debug") or {}
    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    workflow_routing_diagnostics = diagnostics.get("workflow_routing_diagnostics") or {}
    dispatch = workflow_routing_diagnostics.get("dispatch") or {}
    assert dispatch.get("selected_execution_mode") == "direct_response"
    assert dispatch.get("dispatch_workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert (
        completion_report.get("response_text") == "You are in Test Org (#V#test_org)."
    )
    execution = turn_record.get("execution") or {}
    selected_workflow_trace = execution.get("selected_workflow_trace") or {}
    assert selected_workflow_trace.get("selected_execution_mode") == "direct_response"
    assert selected_workflow_trace.get("child_workflow_final_state") == "plain_response"

    context_text = "\n".join(
        str(message.get("content") or "")
        for call in llm.calls
        for message in (call.get("context") or [])
        if isinstance(message, dict)
    )
    assert "CURRENT ORGANISATION CONTEXT: Test Org (#V#test_org)" in context_text
