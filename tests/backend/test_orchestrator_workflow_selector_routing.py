"""Tests for workflow selector routing in the internal MCP orchestrator."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.services.client_capabilities_service import (
    set_client_capabilities_snapshot,
)


class _StubGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}

    def invoke(self, _tool_name: str, _payload: Mapping[str, Any]):  # pragma: no cover
        raise AssertionError("Gateway should not be invoked in this test")


class _CapturingLLM:
    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def test_orchestrator_injects_voice_hint_when_prompt_asks_for_voice(monkeypatch):
    """Voice queries should be grounded via client capabilities snapshot."""

    from flask import Flask
    from typing import cast

    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1")

    app = Flask(__name__)
    app.config.update(SECRET_KEY="test")

    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
    )

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


def test_workflow_selector_routes_to_narration_workflow(monkeypatch):
    """When enabled and classifier returns 'narration', orchestrator emits spoken+screen."""

    monkeypatch.setenv("VON_CHAT_WORKFLOW_SELECTOR_ENABLED", "1")

    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
    )

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
            "narration",  # workflow selector verdict
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
        "<spoken>Short talk track.</spoken>\n\n<screen>Here is the answer on screen.</screen>"
    )

    # Ensure we actually invoked the selector and then narration.
    assert len(llm.calls) == 3
    assert llm.calls[0]["prompt"] == "Select workflow"
    assert llm.calls[2]["prompt"] == "Generate <spoken> talk track"

    aux_types = [
        entry.get("type") for entry in result.aux_llm_calls if isinstance(entry, dict)
    ]
    assert "workflow_selector" in aux_types
