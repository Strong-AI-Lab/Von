import asyncio
import json
import time
from types import SimpleNamespace

import pytest

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


def test_orchestrator_run_rejects_untrusted_actor_before_turn_setup() -> None:
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )
    from src.backend.services.workflow_actor_scope_service import (
        WorkflowActorScopeError,
    )

    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    with bind_internal_mcp_actor_context_source("tool_payload_fallback"):
        with pytest.raises(WorkflowActorScopeError) as exc_info:
            orchestrator.run(
                prompt="Do not start turn setup.",
                context=None,
                llm_client=object(),
                model="test-model",
                user_namespace="#V#forged_user@forged_org",
                user_concept_id="#V#forged_user",
                org_concept_id="#V#forged_org",
            )

    assert exc_info.value.reason == "workflow_actor_authority_required"


def test_handle_von_chat_run_rejects_raw_stdio_identity_claims_before_execution(
    monkeypatch,
):
    from src.backend.mcp_server import mcp_stdio_server as mod

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            raise AssertionError("actor claims must fail before orchestrator creation")

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
        lambda: (_ for _ in ()).throw(
            AssertionError("actor claims must fail before LLM creation")
        ),
    )
    monkeypatch.setattr(
        mod,
        "build_default_catalogue",
        lambda: (_ for _ in ()).throw(
            AssertionError("actor claims must fail before catalogue creation")
        ),
    )

    response_payload = asyncio.run(_runner())

    assert response_payload["success"] is False
    assert response_payload["error_code"] == "workflow_actor_authority_required"
    assert response_payload["workflow_actor_scope"] == {
        "schema_version": "workflow_actor_scope_resolution.v1",
        "status": "rejected",
        "reason": "workflow_actor_authority_required",
        "mismatch_fields": [],
    }


def test_handle_von_chat_run_preserves_explicit_trusted_operator_provenance(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
        get_internal_mcp_actor_context_source,
    )
    from src.backend.mcp_server import mcp_stdio_server as mod

    captured: dict[str, object] = {}
    gateway_kwargs: dict[str, object] = {}

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
            captured["actor_context_source"] = (
                get_internal_mcp_actor_context_source()
            )
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
                "user_namespace": "#V#operator@trusted_org",
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

    def _gateway(**kwargs):
        gateway_kwargs.update(kwargs)
        return _StubGateway()

    monkeypatch.setattr(mod, "InternalMCPGateway", _gateway)
    monkeypatch.setattr(mod, "_run_blocking_with_timeout", _run_blocking)

    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        response_payload = asyncio.run(_runner())

    assert response_payload["success"] is True
    assert captured["user_namespace"] == "#V#operator@trusted_org"
    assert captured["user_concept_id"] == "#V#operator"
    assert captured["org_concept_id"] == "#V#trusted_org"
    assert captured["actor_context_source"] == "trusted_operator_payload_fallback"
    assert gateway_kwargs["trusted_actor_payload_fallback"] is True


def test_handle_von_chat_run_defaults_to_write_enabled_on_noncanonical_local_db(
    monkeypatch,
):
    from src.backend.mcp_server import mcp_stdio_server as mod

    class _StubGateway:
        enabled = True

        def describe_methods(self):
            return {}

        def invoke(self, method_name, payload=None):
            raise AssertionError(f"Unexpected tool invocation: {method_name}")

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            pass

        def run(self, **_kwargs):
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
        payload = await mod._handle_von_chat_run({"prompt": "Hello"})
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.delenv("VON_MCP_ALLOW_WRITES", raising=False)
    monkeypatch.setattr(
        "src.backend.db.mongo_client._is_running_under_pytest",
        lambda: False,
    )
    monkeypatch.setattr(
        "src.backend.db.mongo_client.get_configured_database_name",
        lambda: "dev_von_db",
    )
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
    assert response_payload["allow_writes"] is True
    assert response_payload["dry_run"] is False
    assert response_payload["access_profile"]["authority_state"] == "local_noncanonical"
