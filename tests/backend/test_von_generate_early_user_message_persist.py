"""Tests for JVNAUTOSCI-1423: early user message persistence.

Verifies that the user message is persisted to chat history immediately
after the chat-history lookup, BEFORE orchestrator/LLM work begins.
"""

import pytest
from flask import Flask

_VERSION_INFO = {
    "schema_version": "runtime_code_version.v1",
    "version": "test-version+gabcd1234",
    "source": "test",
    "legacy_app_version": "legacy-test-version",
    "git_commit": "abcd1234abcd1234abcd1234abcd1234abcd1234",
    "git_short_commit": "abcd1234",
    "git_branch": "test-branch",
    "git_dirty": False,
}


class _StubLLM:
    def __init__(self):
        self.calls = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        return "test response"


class _MessageRecorder:
    """Records add_message_to_history calls in order."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        # Positional: user_id, session_id, message
        entry = {}
        if len(args) >= 3:
            entry["user_id"] = args[0]
            entry["session_id"] = args[1]
            entry["message"] = args[2]
        elif len(args) >= 1:
            entry["user_id"] = args[0]
        entry.update(kwargs)
        self.calls.append(entry)

    @property
    def user_message_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if isinstance(c.get("message"), dict) and c["message"].get("role") == "user"
        ]

    @property
    def assistant_message_calls(self) -> list[dict]:
        return [
            c for c in self.calls
            if isinstance(c.get("message"), dict) and c["message"].get("role") == "assistant"
        ]


@pytest.fixture()
def recorder():
    return _MessageRecorder()


@pytest.fixture()
def app(monkeypatch, recorder):
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
        "src.backend.server.routes.von_routes.get_runtime_code_version_info",
        lambda: dict(_VERSION_INFO),
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        recorder,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
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
    flask_app.config["INTERNAL_MCP_GATEWAY"] = None

    return flask_app


def test_user_message_persisted_exactly_once(app, recorder):
    """User message must appear exactly once in history calls — the early persist."""
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Hello early persist"})
    assert resp.status_code == 200

    user_calls = recorder.user_message_calls
    assert len(user_calls) == 1, (
        f"Expected exactly 1 user-message persist call, got {len(user_calls)}: "
        f"{[c['message'] for c in user_calls]}"
    )
    assert user_calls[0]["message"]["content"] == "Hello early persist"
    assert user_calls[0]["message"]["role"] == "user"


def test_user_message_persisted_before_assistant(app, recorder):
    """User message must be persisted before the assistant response."""
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Order test"})
    assert resp.status_code == 200

    all_calls = recorder.calls
    user_indices = [
        i for i, c in enumerate(all_calls)
        if isinstance(c.get("message"), dict) and c["message"].get("role") == "user"
    ]
    assistant_indices = [
        i for i, c in enumerate(all_calls)
        if isinstance(c.get("message"), dict) and c["message"].get("role") == "assistant"
    ]

    assert len(user_indices) >= 1, "No user message persist call found"
    assert len(assistant_indices) >= 1, "No assistant message persist call found"
    assert user_indices[0] < assistant_indices[0], (
        f"User message (index {user_indices[0]}) must come before "
        f"assistant message (index {assistant_indices[0]})"
    )


def test_early_persist_failure_falls_back_to_downstream(app, monkeypatch, recorder):
    """If early persist fails, the downstream path should still persist the user message."""
    call_count = {"early": 0}

    original_recorder_call = recorder.__call__

    def _fail_on_first_user(*args, **kwargs):
        msg = args[2] if len(args) >= 3 else kwargs.get("message", {})
        if isinstance(msg, dict) and msg.get("role") == "user":
            call_count["early"] += 1
            if call_count["early"] == 1:
                raise RuntimeError("Simulated early persist failure")
        original_recorder_call(*args, **kwargs)

    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        _fail_on_first_user,
    )

    client = app.test_client()
    resp = client.post("/von/generate", json={"prompt": "Fallback test"})
    assert resp.status_code == 200

    # The first attempt failed, but fallback should have retried
    assert call_count["early"] >= 2, (
        f"Expected at least 2 user-message persist attempts (1 early fail + 1 fallback), "
        f"got {call_count['early']}"
    )


def test_user_message_contains_author_user_id(app, recorder):
    """Early-persisted user message must include the author_user_id field."""
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Author check"})
    assert resp.status_code == 200

    user_calls = recorder.user_message_calls
    assert len(user_calls) >= 1
    assert user_calls[0]["message"].get("author_user_id") == "#V#test_user"


def test_assistant_message_still_persisted(app, recorder):
    """Assistant response must still be persisted after early user persist."""
    client = app.test_client()

    resp = client.post("/von/generate", json={"prompt": "Assistant check"})
    assert resp.status_code == 200

    assistant_calls = recorder.assistant_message_calls
    assert len(assistant_calls) >= 1, "No assistant message was persisted"
    assert assistant_calls[0]["message"]["content"] == "test response"
