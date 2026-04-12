
import pytest
from flask import Flask


class _StubLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return "ok"


@pytest.fixture()
def app(monkeypatch):
    from src.backend.server.routes.von_routes import von_bp

    llm = _StubLLM()

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
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )

    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **kwargs: (
            [
                {"concept_id": "#V#prompt1", "content": "First prompt."},
                {"concept_id": "#V#prompt2", "content": "Second prompt."},
            ]
            if kwargs.get("prompt_types")
            == (
                "#V#von_chat_behaviour_prompt",
                "#V#von_chat_behavior_prompt",
                "#V#von_llm_prompt",
            )
            else []
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        lambda concept_id: {"name": "Test User"} if concept_id == "#V#test_user" else None,
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
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    flask_app.config["TEST_LLM"] = llm
    return flask_app


def test_generate_includes_user_prompt_debug_metadata(app):
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Hello"})
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["response"] == "ok"

    llm_debug = body["llm_debug"]
    assert llm_debug["user_prompt"]["effective_user_concept_id"] == "#V#test_user"
    assert llm_debug["user_prompt"]["loaded"] is True
    assert llm_debug["user_prompt"]["chars"] > 0
    assert llm_debug["user_prompt"]["prompt_concept_ids"] == [
        "#V#prompt1",
        "#V#prompt2",
    ]

    llm_calls = app.config["TEST_LLM"].calls
    assert len(llm_calls) == 1

    sent_context = llm_calls[0]["context"]
    assert any(
        msg.get("role") == "system"
        and "USER-SPECIFIC SYSTEM PROMPT" in msg.get("content", "")
        for msg in sent_context
    )
    assert any(
        msg.get("role") == "system"
        and "Current user" in msg.get("content", "")
        and "#V#test_user" in msg.get("content", "")
        for msg in sent_context
    )

    injected_msg = next(
        msg
        for msg in sent_context
        if msg.get("role") == "system"
        and "USER-SPECIFIC SYSTEM PROMPT" in msg.get("content", "")
    )
    assert "First prompt." in injected_msg.get("content", "")
    assert "Second prompt." in injected_msg.get("content", "")
