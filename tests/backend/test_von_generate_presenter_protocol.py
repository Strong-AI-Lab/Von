from __future__ import annotations

from flask import Flask
from typing import Protocol


class _LLMProtocol(Protocol):
    def generate(self, prompt, context, model) -> str:  # pragma: no cover
        ...


class _StubLLM:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return self._response_text


class _StubLLMSequence:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        if not self._responses:
            raise AssertionError("No stubbed LLM responses remaining")
        return self._responses.pop(0)


def _make_app(monkeypatch, llm: _LLMProtocol) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

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
        lambda: None,
    )

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(von_bp, url_prefix="/von")

    flask_app.config["CONTEXT"] = []
    flask_app.config["INTERNAL_MCP_ORCHESTRATOR"] = None
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def test_generate_extracts_presenter_blocks_and_returns_response_channels(monkeypatch):
    llm = _StubLLM(
        "<spoken>Hello there.</spoken>\n<screen>Here is the on-screen content.</screen>"
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "Here is the on-screen content."
    assert body["response_channels"] == {
        "screen": "Here is the on-screen content.",
        "spoken": "Hello there.",
        "format": "tagged_blocks_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug["presenter_channels"]["screen"] == "Here is the on-screen content."
    assert llm_debug["presenter_channels"]["spoken"] == "Hello there."
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is False
    assert llm_debug.get("spoken_backfill_second_pass_reason") is None

    assert len(llm.calls) == 1
    sent_context = llm.calls[0]["context"]
    assert sent_context
    assert sent_context[0]["role"] == "system"
    assert "PRESENTER MODE PROTOCOL" in sent_context[0]["content"]


def test_generate_generates_spoken_when_only_screen_tag_present(monkeypatch):
    llm = _StubLLMSequence(
        [
            "<screen>Only screen.</screen>",
            "<spoken>Short talk track.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "Only screen."
    assert body["response_channels"] == {
        "screen": "Only screen.",
        "spoken": "Short talk track.",
        "format": "narration_fallback_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert llm_debug.get("spoken_backfill_second_pass_reason") == "missing_spoken"

    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_generate_presenter_mode_falls_back_to_second_pass_spoken(monkeypatch):
    llm = _StubLLMSequence(
        [
            "This is the full on-screen answer with details.",
            "<spoken>Short summary for TTS.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    assert body["response"] == "This is the full on-screen answer with details."
    assert body["response_channels"] == {
        "screen": "This is the full on-screen answer with details.",
        "spoken": "Short summary for TTS.",
        "format": "narration_fallback_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert (
        llm_debug.get("spoken_backfill_second_pass_reason")
        == "missing_presenter_channels"
    )

    assert len(llm.calls) == 2
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"
