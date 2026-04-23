import pytest
from flask import Flask


class _CapturingLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        return "ok"


class _CapturingOrchestrator:
    def __init__(self):
        self.calls = []
        self.build_augmented_calls = []

    def run(self, **kwargs):
        return self.execute_conversation_turn_supervised(**kwargs)

    def _build_augmented_context(self, context, **kwargs):
        self.build_augmented_calls.append(
            {"context": list(context or []), **kwargs}
        )
        return [
            {
                "role": "system",
                "content": "Authenticated context ready for LLM execution.",
            }
        ] + list(context or [])

    def execute_conversation_turn_supervised(self, **kwargs):
        self.calls.append(kwargs)

        from src.backend.integrations.internal_mcp.orchestrator import (
            OrchestratorResult,
        )

        return OrchestratorResult(
            response_text="ok", extra_messages=[], tool_invocations=[], aux_llm_calls=[]
        )


@pytest.fixture()
def app(monkeypatch):
    import src.backend.workflows.durable.registry_factory as registry_factory

    monkeypatch.setattr(
        registry_factory,
        "discover_workflow_ids",
        lambda: [],
    )
    monkeypatch.setattr(
        registry_factory,
        "_launch_deferred_registry_work",
        lambda **_kwargs: None,
    )

    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_INTERNAL_MCP_ALLOW_USER_TOOL_CALLS", "0")
    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "0")

    # Force an authenticated user for the request.
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#michael_witbrock",
    )

    # Stub auxiliary prompt loader.
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **kwargs: (
            [{"concept_id": "#V#test_prompt", "content": "Please be terse."}]
            if kwargs.get("prompt_types")
            == (
                "#V#von_chat_behaviour_prompt",
                "#V#von_chat_behavior_prompt",
                "#V#von_llm_prompt",
            )
            else []
        ),
    )

    llm = _CapturingLLM()
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
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
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._add_chat_history_message",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_context_concept_reference_metadata",
        lambda *_args, **_kwargs: {
            "source": "sent_context_user_assistant",
            "metadata_version": 1,
            "message_roles": ["user", "assistant"],
            "messages_scanned": 0,
            "concept_count": 0,
            "concept_count_capped": False,
            "max_concepts": None,
            "include_direct_supertypes": False,
            "max_direct_supertypes": 0,
            "concepts": [],
        },
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._finalise_llm_debug_info",
        lambda **kwargs: kwargs["llm_debug_info"],
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_workflow_registry_read_only",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda *_args, **_kwargs: None,
        raising=False,
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.config["TESTING"] = True
    flask_app.config["PROPAGATE_EXCEPTIONS"] = True
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["_TEST_LLM"] = llm

    return flask_app


def test_generate_passes_auxiliary_prompt_to_orchestrator(app):
    orchestrator = _CapturingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    assert orchestrator.calls, "expected orchestrator.run() to be called"
    call = orchestrator.calls[0]
    assert call.get("auxiliary_system_prompt") == "Please be terse."


def test_generate_supervised_path_reports_actual_sent_context_and_mcp_access(app):
    orchestrator = _CapturingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Who am I?"})
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    assert orchestrator.calls, "expected orchestrator execution"
    assert orchestrator.calls[0].get("context") == []

    assert orchestrator.build_augmented_calls, "expected sent-context reconstruction"
    build_call = orchestrator.build_augmented_calls[0]
    assert build_call.get("user_concept_id") == "#V#michael_witbrock"
    assert build_call.get("user_namespace") == "#V#michael_witbrock"
    assert build_call.get("auxiliary_system_prompt") == "Please be terse."

    body = resp.get_json()
    llm_debug = body["llm_debug"]
    sent_stats = llm_debug["context_stats"]["sent_to_llm"]
    assert sent_stats["by_role"]["system"] == 1

    diagnostics = llm_debug["turn_execution_diagnostics"]
    mcp_access = diagnostics["mcp_access"]
    assert "turn_execution_get_diagnostics" in mcp_access
    assert "chat_history_get_segments" in mcp_access
    assert (
        mcp_access["turn_execution_get_diagnostics"]["arguments"]["request_id"]
        == body["request_id"]
    )


def test_generate_includes_auxiliary_prompt_without_orchestrator(app):
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    app.config["INTERNAL_MCP_GATEWAY"] = None

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    llm = app.config["_TEST_LLM"]
    assert llm.calls, "expected llm.generate() to be called"

    context = llm.calls[0]["context"]
    assert context and context[0]["role"] == "system"

    system_texts = [m["content"] for m in context if m.get("role") == "system"]
    assert any("USER-SPECIFIC SYSTEM PROMPT" in text for text in system_texts)
    assert any("Please be terse." in text for text in system_texts)


def test_generate_recovers_header_only_identity_without_session(app, monkeypatch):
    from flask import request as flask_request

    orchestrator = _CapturingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()

    model_calls: list[dict[str, object]] = []

    def _effective_user() -> str | None:
        header_value = flask_request.headers.get("X-User-Concept-ID")
        return header_value.strip() if isinstance(header_value, str) else None

    def _effective_context(window_session_id, session_snapshot, user_concept_id):
        assert window_session_id == "window-identity"
        assert session_snapshot.get("user_concept_id") is None
        assert user_concept_id == "#V#header_user"
        return {
            "organisation_id": "#V#header_org",
            "chat_session_id": "session-from-window",
            "role": "member",
        }

    def _capture_model_name(*, user_concept_id=None, org_concept_id=None):
        model_calls.append(
            {
                "user_concept_id": user_concept_id,
                "org_concept_id": org_concept_id,
            }
        )
        return "test-model"

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        _effective_user,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_effective_context",
        _effective_context,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        _capture_model_name,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._resolve_shared_conversation_owner",
        lambda **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes._add_chat_history_message",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello from a restarted session"},
        headers={
            "X-User-Concept-ID": "#V#header_user",
            "X-Von-Window-Session": "window-identity",
        },
    )
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    assert model_calls, "expected scoped model lookup"
    assert model_calls[0]["user_concept_id"] == "#V#header_user"
    assert model_calls[0]["org_concept_id"] == "#V#header_org"

    assert orchestrator.calls, "expected orchestrator.run() to be called"
    call = orchestrator.calls[0]
    assert call.get("user_concept_id") == "#V#header_user"
    assert call.get("org_concept_id") == "#V#header_org"
    assert call.get("user_namespace") == "#V#header_user@header_org"


def test_generate_passes_turn_memory_context_to_orchestrator(app):
    orchestrator = _CapturingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={
            "prompt": "Who am I?",
            "turn_memory_context": {
                "subject_id": "#V#test_user",
                "subject_kind": "concept",
                "context_dossier_id": "#V#user_turn_dossier",
            },
        },
    )
    if resp.status_code != 200:
        raise AssertionError(
            f"Unexpected status {resp.status_code}: {resp.get_json() or resp.get_data(as_text=True)}"
        )

    assert orchestrator.calls, "expected orchestrator.run() to be called"
    assert orchestrator.calls[0]["turn_memory_context"] == {
        "subject_id": "#V#test_user",
        "subject_kind": "concept",
        "context_dossier_id": "#V#user_turn_dossier",
    }


def test_generate_rejects_non_object_turn_memory_context(app):
    orchestrator = _CapturingOrchestrator()
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = object()

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={
            "prompt": "Who am I?",
            "turn_memory_context": ["not", "an", "object"],
        },
    )

    assert resp.status_code == 400
    assert resp.get_json() == {
        "error": "invalid_turn_memory_context",
        "detail": "turn_memory_context must be an object.",
    }
    assert orchestrator.calls == []
