from __future__ import annotations

import pytest
from flask import Flask


class _StubLLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return "unexpected"


class _StubGatewayNoInterpret:
    enabled = True

    def describe_methods(self):
        return {"read_file_copy": {"description": "read file copy"}}


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
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [
            {
                "role": "user",
                "content": (
                    "Attached file concept: "
                    "#V#uploaded_file_copy_76c1c13fed0140f496133d008b4cfad7"
                ),
            },
            {"role": "assistant", "content": "Understood."},
        ],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")
    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = _StubGatewayNoInterpret()
    flask_app.config["_test_llm"] = llm
    return flask_app


def test_orchestrator_unavailable_uses_direct_llm_fallback_for_scholarly_language_prompt(
    app,
):
    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "Fully represent the corresponding paper"},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    text = body.get("response") or ""
    assert text == "unexpected"

    llm = app.config["_test_llm"]
    assert len(llm.calls) == 1

    llm_debug = body.get("llm_debug") or {}
    aux_calls = llm_debug.get("aux_llm_calls") or []
    fallback_entry = next(
        (
            entry
            for entry in aux_calls
            if isinstance(entry, dict)
            and entry.get("type") == "orchestrator_unavailable_fallback"
        ),
        None,
    )
    assert fallback_entry is not None
    assert not any(
        isinstance(entry, dict)
        and entry.get("type") == "orchestrator_unavailable_fail_closed"
        for entry in aux_calls
    )
