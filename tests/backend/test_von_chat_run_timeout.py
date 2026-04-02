import asyncio
import json
import time
from types import SimpleNamespace

from src.backend.mcp_server.mcp_stdio_server import (
    _run_blocking_with_timeout,
    VonChatRunTimeout,
)


def test_run_blocking_with_timeout_times_out():
    async def _runner():
        def _block():
            time.sleep(0.2)

        try:
            await _run_blocking_with_timeout(_block, timeout_seconds=0.05)
        except VonChatRunTimeout as exc:
            assert exc.pid > 0
            # Thread id may be None if timeout happens before worker starts.
            assert exc.thread_id is None or exc.thread_id > 0
            return True
        return False

    assert asyncio.run(_runner()) is True


def test_handle_von_chat_run_derives_identity_components_from_namespace(
    monkeypatch,
):
    from src.backend.mcp_server import mcp_stdio_server as mod

    captured: dict[str, object] = {}

    class _StubGateway:
        enabled = True

        def describe_methods(self):
            return {}

        def invoke(self, method_name, payload=None):
            raise AssertionError(f"Unexpected tool invocation: {method_name}")

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def run(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                response_text="ok",
                tool_invocations=[],
                extra_messages=[],
                aux_llm_calls=[],
            )

    async def _run_blocking(func, *, timeout_seconds):
        assert timeout_seconds == 90.0
        return func()

    async def _runner():
        payload = await mod._handle_von_chat_run(
            {
                "prompt": "Hello",
                "user_namespace": "#V#user_alpha@org_beta",
            }
        )
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda: object(),
    )
    monkeypatch.setattr(mod, "build_default_catalogue", lambda: object())
    monkeypatch.setattr(mod, "InternalMCPTransport", lambda: object())
    monkeypatch.setattr(
        mod,
        "InternalMCPGateway",
        lambda **_kwargs: _StubGateway(),
    )
    monkeypatch.setattr(mod, "_run_blocking_with_timeout", _run_blocking)

    response_payload = asyncio.run(_runner())

    assert response_payload["success"] is True
    assert response_payload["model"] == "test-model"
    assert captured["user_namespace"] == "#V#user_alpha@org_beta"
    assert captured["user_concept_id"] == "#V#user_alpha"
    assert captured["org_concept_id"] == "#V#org_beta"
