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


class _StubOrchestrator:
    def __init__(self, result):
        self._result = result
        self.calls: list[dict] = []

    def configure_execution_caps(self, **_kwargs) -> None:
        return None

    def set_progress_callback(self, _callback) -> None:
        return None

    def run(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self._result

    def execute_workflow(self, *_args, **_kwargs):
        return None


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
    display_elements = body["display_elements"]
    assert display_elements["schema_version"] == "turn_display_elements_v1"
    assert display_elements["validation"]["valid"] is True
    screen_element = next(
        element
        for element in display_elements["elements"]
        if element["element_id"] == "screen_text"
    )
    spoken_element = next(
        element
        for element in display_elements["elements"]
        if element["element_id"] == "spoken_text"
    )
    assert screen_element["payload"]["text"] == "Here is the on-screen content."
    assert spoken_element["payload"]["text"] == "Hello there."
    assert llm_debug["display_elements"]["schema_version"] == "turn_display_elements_v1"

    assert len(llm.calls) == 1
    sent_context = llm.calls[0]["context"]
    assert sent_context
    assert sent_context[0]["role"] == "system"
    assert "PRESENTER MODE PROTOCOL" in sent_context[0]["content"]


def test_generate_preserves_fenced_code_inside_screen_block(monkeypatch):
    llm = _StubLLM(
        "<spoken>Talk track.</spoken>\n"
        "<screen>Strict JSON schema:\n"
        "```json\n"
        '{"mapping_type": "input"}\n'
        "```\n"
        "Done.</screen>"
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    expected_screen = (
        "Strict JSON schema:\n"
        "```json\n"
        '{"mapping_type": "input"}\n'
        "```\n"
        "Done."
    )
    assert body["response"] == expected_screen
    assert body["response_channels"]["screen"] == expected_screen
    assert body["response_channels"]["spoken"] == "Talk track."


def test_generate_buttonify_heuristic_preflight_skips_model_pass(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("VON_BUTTONIFY_MODEL_ENABLE", "1")
    monkeypatch.setenv("VON_BUTTONIFY_HEURISTIC_PREFLIGHT_ENABLE", "1")

    llm = _StubLLM('Please reply with one of: "Proceed", "Hold".')
    app = _make_app(monkeypatch, llm)

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Hello"})

    assert resp.status_code == 200
    body = resp.get_json()
    llm_debug = body["llm_debug"]
    buttonify = llm_debug.get("buttonify")
    assert isinstance(buttonify, dict)
    assert buttonify.get("source") == "heuristic_preflight"
    assert buttonify.get("options") == ["Proceed", "Hold"]

    # Preflight should avoid a second model pass for buttonify extraction.
    assert len(llm.calls) == 1


def test_extract_presenter_channels_ignores_tags_inside_fenced_blocks():
    from src.backend.server.routes.von_routes import _extract_presenter_channels

    text = (
        "```html\n"
        "<screen>Ignore this fake block.</screen>\n"
        "```\n"
        "<spoken>Real spoken.</spoken>\n"
        "<screen>Real screen.</screen>"
    )
    channels = _extract_presenter_channels(text)
    assert channels is not None
    assert channels["spoken"] == "Real spoken."
    assert channels["screen"] == "Real screen."


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


def test_generate_accepts_plain_text_from_narration_second_pass(monkeypatch):
    llm = _StubLLMSequence(
        [
            "This is the full on-screen answer with **markdown** and details.",
            "Short summary for TTS.",
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

    assert (
        body["response"]
        == "This is the full on-screen answer with **markdown** and details."
    )
    assert body["response_channels"] == {
        "screen": "This is the full on-screen answer with **markdown** and details.",
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


def test_generate_narration_prompt_includes_preferred_and_max_speaking_seconds(
    monkeypatch,
):
    llm = _StubLLMSequence(
        [
            "This is the full on-screen answer with details.",
            "<spoken>Short talk track.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    client = app.test_client()

    with client.session_transaction() as sess:
        sess["client_capabilities_snapshot"] = {
            "kind": "client_capabilities",
            "client_reported": True,
            "speech_synthesis": {
                "supported": True,
                "voices_count": 1,
                "default_voice_lang": "en-NZ",
                "settings": {
                    "max_speaking_seconds": 40,
                    "preferred_speaking_seconds": 20,
                },
            },
        }

    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    assert len(llm.calls) == 2

    system_text = llm.calls[1]["context"][0]["content"]
    assert "Speech timing hint" in system_text, system_text
    assert "preferred_speaking_seconds=20" in system_text
    assert "max_speaking_seconds=40" in system_text


def test_presenter_mode_uses_tool_screen_when_screen_tag_missing(monkeypatch):
    import json

    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    tool_payload = {
        "tool": "fetch_concept",
        "status": "ok",
        "duration_ms": 12,
        "payload": {"concept_id": "#V#example"},
    }
    tool_message = {"role": "tool", "content": json.dumps(tool_payload)}
    orchestrator_result = OrchestratorResult(
        response_text="Plain response without presenter tags.",
        extra_messages=[tool_message],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    expected_screen = "Plain response without presenter tags."

    assert body["response"] == expected_screen
    assert body["response_channels"] == {
        "screen": expected_screen,
        "spoken": "Short talk track.",
        "format": "screen_backfill_from_response_v1",
    }

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"
    assert llm_debug.get("spoken_backfill_second_pass_attempted") is True
    assert llm_debug.get("spoken_backfill_second_pass_reason") == "missing_spoken"

    assert len(llm.calls) == 1
    assert llm.calls[0]["prompt"] == "Generate <spoken> talk track"


def test_presenter_mode_rejects_hallucinated_description_write_in_screen_backfill(
    monkeypatch,
):
    import json

    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(
        [
            "<screen>Description updated: YES</screen>",
            "<spoken>Short talk track.</spoken>",
        ]
    )
    app = _make_app(monkeypatch, llm)

    tool_payload = {
        "tool": "add_relationship",
        "status": "ok",
        "duration_ms": 12,
        "payload": {
            "source_id": "#V#example",
            "predicate": "#V#is_a",
            "target": "#V#concept",
            "added": True,
        },
    }
    tool_message = {"role": "tool", "content": json.dumps(tool_payload)}
    orchestrator_result = OrchestratorResult(
        response_text=(
            "Tool results:\n\n" "```json\n" '{"tool": "add_relationship"}\n' "```"
        ),
        extra_messages=[tool_message],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": "Hello", "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()

    screen_text = body["response_channels"]["screen"]
    assert "Writes ledger (authoritative):" in screen_text
    assert "Description updated: NO" in screen_text

    llm_debug = body["llm_debug"]
    assert llm_debug.get("screen_backfill_second_pass_attempted") is True
    assert llm_debug.get("screen_backfill_second_pass_reason") == "missing_screen"

    assert len(llm.calls) == 2
    assert llm.calls[0]["prompt"] == "Generate <screen> display content"
    assert llm.calls[1]["prompt"] == "Generate <spoken> talk track"


def test_presenter_mode_preserves_required_screen_json_fence_from_prompt(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    orchestrator_result = OrchestratorResult(
        response_text="Tool-grounded facts only.",
        extra_messages=[],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    prompt = (
        "Please include this exact fenced block verbatim:\n\n"
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "If anything fails, include exact error text/reason_code."
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": prompt, "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    screen_text = body["response_channels"]["screen"]
    expected_fence = (
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "```"
    )

    assert expected_fence in screen_text
    assert body["llm_debug"].get("screen_backfill_second_pass_attempted") is True
    assert body["llm_debug"].get("screen_backfill_second_pass_reason") in {
        "missing_screen",
        "missing_screen_fence",
    }
    display_elements = body["display_elements"]
    json_blocks = [
        element
        for element in display_elements["elements"]
        if element["element_type"] == "json_block"
    ]
    assert json_blocks
    assert any(element["payload"]["fence"] == expected_fence for element in json_blocks)


def test_presenter_mode_can_disable_legacy_screen_fence_insertion(monkeypatch):
    from src.backend.integrations.internal_mcp.orchestrator import OrchestratorResult

    monkeypatch.setenv("VON_DISPLAY_ELEMENTS_SCREEN_FENCE_COMPAT_ENABLE", "0")

    llm = _StubLLMSequence(["<spoken>Short talk track.</spoken>"])
    app = _make_app(monkeypatch, llm)

    orchestrator_result = OrchestratorResult(
        response_text="Tool-grounded facts only.",
        extra_messages=[],
        tool_invocations=(),
        aux_llm_calls=(),
    )
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = _StubOrchestrator(orchestrator_result)

    prompt = (
        "Please include this exact fenced block verbatim:\n\n"
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "If anything fails, include exact error text/reason_code."
    )

    client = app.test_client()
    resp = client.post(
        "/von/generate",
        json={"prompt": prompt, "presenter_mode": True},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    screen_text = body["response_channels"]["screen"]
    expected_fence = (
        "```json\n"
        '{"sentinel":"FENCE_MUST_SURVIVE","check":"presenter_screen_code_fence_preserved"}\n'
        "```"
    )

    # With compat disabled, screen text is no longer mutated to append the fence.
    assert expected_fence not in screen_text
    assert body["llm_debug"]["display_elements_screen_fence_compat_enabled"] is False

    display_elements = body["display_elements"]
    assert "required_screen_json_fence_appended" in display_elements["reason_codes"]
    json_blocks = [
        element
        for element in display_elements["elements"]
        if element["element_type"] == "json_block"
    ]
    assert json_blocks
    assert any(element["payload"]["fence"] == expected_fence for element in json_blocks)
