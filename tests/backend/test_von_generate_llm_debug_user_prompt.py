import json

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
        lambda: "test-model",
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

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
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

    injected_msg = next(
        msg
        for msg in sent_context
        if msg.get("role") == "system"
        and "USER-SPECIFIC SYSTEM PROMPT" in msg.get("content", "")
    )
    assert "First prompt." in injected_msg.get("content", "")
    assert "Second prompt." in injected_msg.get("content", "")
