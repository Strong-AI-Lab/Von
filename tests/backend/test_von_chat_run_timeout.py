from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

from src.backend.mcp_server.mcp_stdio_server import (
    VonChatRunTimeout,
    _run_blocking_with_timeout,
)


def _adaptive_result(*, status: str = "completed") -> SimpleNamespace:
    return SimpleNamespace(
        response_text="ok",
        terminal_status=status,
        tool_invocations=[],
        extra_messages=[],
        evidence_index=[],
        llm_usage=None,
    )


def _access_profile() -> dict[str, object]:
    return {
        "profile_id": "test-profile",
        "environment": {
            "authority_state": "local_noncanonical",
            "authority_kind": "local_engineering",
            "configured_database_name": "test_db",
        },
        "shared_authority_write_policy": {
            "mode": "read_only_chat",
            "write_category_tools_allowed": True,
        },
        "von_chat_run_policy": {
            "default_allow_writes": False,
            "default_dry_run": True,
        },
    }


def test_run_blocking_with_timeout_returns_without_waiting_for_late_thread() -> None:
    async def _runner() -> VonChatRunTimeout:
        def _block() -> None:
            time.sleep(0.3)

        try:
            await _run_blocking_with_timeout(_block, timeout_seconds=0.03)
        except VonChatRunTimeout as exc:
            return exc
        raise AssertionError("expected timeout")

    started = time.perf_counter()
    exc = asyncio.run(_runner())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.15
    assert exc.pid > 0
    assert exc.thread_id is None or exc.thread_id > 0


def test_repeated_timeouts_use_a_bounded_shared_worker_pool() -> None:
    async def _runner() -> list[object]:
        async def _timed_call() -> object:
            try:
                return await _run_blocking_with_timeout(
                    lambda: time.sleep(0.2),
                    timeout_seconds=0.01,
                )
            except VonChatRunTimeout as exc:
                return exc

        return await asyncio.gather(*(_timed_call() for _ in range(8)))

    outcomes = asyncio.run(_runner())

    assert all(isinstance(outcome, VonChatRunTimeout) for outcome in outcomes)
    live_workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("von_chat_run")
    ]
    assert len(live_workers) <= 4
    time.sleep(0.25)


def test_restricted_gateway_forwards_adaptive_read_transport_options() -> None:
    from src.backend.mcp_server.mcp_stdio_server import _RestrictedGateway

    captured: dict[str, object] = {}

    class _BaseGateway:
        enabled = True

        def describe_methods(self):
            return {"safe_read": {"category": "read"}}

        def get_method_definition(self, method_name):
            return SimpleNamespace(name=method_name, category="read")

        def get_method_timeout_sec(self, method_name):
            assert method_name == "safe_read"
            return 17.0

        def invoke(
            self,
            method_name,
            payload,
            *,
            deadline_monotonic=None,
            late_completion_observer=None,
            require_effect_admission_window=False,
        ):
            captured.update(
                {
                    "method_name": method_name,
                    "payload": payload,
                    "deadline_monotonic": deadline_monotonic,
                    "late_completion_observer": late_completion_observer,
                    "require_effect_admission_window": (
                        require_effect_admission_window
                    ),
                }
            )
            return "result"

    gateway = _RestrictedGateway(gateway=_BaseGateway(), allow_writes=False)
    assert gateway.get_method_timeout_sec("safe_read") == 17.0

    result = gateway.invoke(
        "safe_read",
        {"query": "bounded"},
        deadline_monotonic=123.5,
        late_completion_observer=None,
        require_effect_admission_window=False,
    )

    assert result == "result"
    assert captured == {
        "method_name": "safe_read",
        "payload": {"query": "bounded"},
        "deadline_monotonic": 123.5,
        "late_completion_observer": None,
        "require_effect_admission_window": False,
    }


def test_handle_von_chat_run_rejects_raw_stdio_identity_before_model_or_reads(
    monkeypatch,
) -> None:
    from src.backend.mcp_server import mcp_stdio_server as mod

    async def _runner() -> dict[str, object]:
        payload = await mod._handle_von_chat_run(
            {
                "prompt": "Hello",
                "user_namespace": "#V#user_alpha@org_beta",
            }
        )
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
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

    response = asyncio.run(_runner())

    assert response["success"] is False
    assert response["error_code"] == "workflow_actor_authority_required"


def test_handle_von_chat_run_projects_trusted_operator_scope_to_adaptive_turn(
    monkeypatch,
) -> None:
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

        def get_method_definition(self, _method_name):
            return None

        def invoke(self, method_name, payload=None):
            raise AssertionError(f"Unexpected tool invocation: {method_name}")

    def _adaptive(**kwargs):
        captured.update(kwargs)
        captured["actor_context_source"] = get_internal_mcp_actor_context_source()
        return _adaptive_result()

    async def _run_blocking(func, *, timeout_seconds):
        assert timeout_seconds == 91.0
        return func()

    async def _runner() -> dict[str, object]:
        payload = await mod._handle_von_chat_run(
            {
                "prompt": "Hello",
                "user_namespace": "#V#operator@trusted_org",
                "auxiliary_system_prompt": "Supplementary material",
                "gmail_profile": "represented-profile",
            }
        )
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "src.backend.services.adaptive_turn_service.execute_adaptive_turn",
        _adaptive,
    )
    monkeypatch.setattr(mod, "build_default_catalogue", lambda: object())
    monkeypatch.setattr(mod, "InternalMCPTransport", lambda: object())
    monkeypatch.setattr(
        mod,
        "_evaluate_stdio_write_access",
        lambda *_args, **_kwargs: (True, _access_profile(), {}),
    )
    monkeypatch.setattr(mod, "get_preferred_language", lambda: "en-NZ")
    monkeypatch.setattr(
        "src.backend.services.mail_profile_resource_vontology_service."
        "resolve_authorised_gmail_profile_for_user",
        lambda **_kwargs: {
            "success": True,
            "profile_id": "represented-profile",
        },
    )
    monkeypatch.setattr(
        "src.backend.integrations.google.gmail_service.list_profile_ids_from_env",
        lambda: ["represented-profile"],
    )

    def _gateway(**kwargs):
        gateway_kwargs.update(kwargs)
        return _StubGateway()

    monkeypatch.setattr(mod, "InternalMCPGateway", _gateway)
    monkeypatch.setattr(mod, "_run_blocking_with_timeout", _run_blocking)

    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        response = asyncio.run(_runner())

    assert response["success"] is True
    assert response["allow_writes"] is False
    assert response["dry_run"] is True
    assert response["delegation"] == "read_only"
    assert captured["user_namespace"] == "#V#operator@trusted_org"
    assert captured["user_concept_id"] == "#V#operator"
    assert captured["org_concept_id"] == "#V#trusted_org"
    assert captured["trusted_argument_values"] == {
        "gmail_profile": "represented-profile",
    }
    assert captured["actor_context_source"] == "trusted_operator_payload_fallback"
    assert gateway_kwargs["trusted_actor_payload_fallback"] is True
    context = captured["context"]
    assert isinstance(context, list)
    assert [item["role"] for item in context] == ["system", "user"]


def test_handle_von_chat_run_rejects_unrepresented_gmail_profile_before_model(
    monkeypatch,
) -> None:
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.mcp_server import mcp_stdio_server as mod

    async def _runner() -> dict[str, object]:
        payload = await mod._handle_von_chat_run(
            {
                "prompt": "Check my email",
                "user_namespace": "#V#operator@trusted_org",
                "gmail_profile": "unrepresented-profile",
            }
        )
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.services.mail_profile_resource_vontology_service."
        "resolve_authorised_gmail_profile_for_user",
        lambda **_kwargs: {
            "success": False,
            "error_code": "gmail_profile_not_authorised",
        },
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("mail authority must fail before model creation")
        ),
    )
    monkeypatch.setattr(
        mod,
        "build_default_catalogue",
        lambda: (_ for _ in ()).throw(
            AssertionError("mail authority must fail before catalogue creation")
        ),
    )

    with bind_internal_mcp_actor_context_source(
        "trusted_operator_payload_fallback"
    ):
        response = asyncio.run(_runner())

    assert response["success"] is False
    assert response["error_code"] == "gmail_profile_not_authorised"


def test_handle_von_chat_run_is_read_only_even_when_profile_allows_writes(
    monkeypatch,
) -> None:
    from src.backend.mcp_server import mcp_stdio_server as mod

    class _StubGateway:
        enabled = True

        def describe_methods(self):
            return {
                "safe_read": {"category": "read"},
                "bounded_effect": {
                    "category": "write",
                    "ordinary_turn_effect": True,
                },
            }

        def get_method_definition(self, method_name):
            if method_name == "safe_read":
                return SimpleNamespace(name=method_name, category="read")
            if method_name == "bounded_effect":
                return SimpleNamespace(name=method_name, category="write")
            return None

    def _adaptive(**kwargs):
        restricted_gateway = kwargs["gateway"]
        assert set(restricted_gateway.describe_methods()) == {"safe_read"}
        assert restricted_gateway.get_method_definition("safe_read") is not None
        assert restricted_gateway.get_method_definition("bounded_effect") is None
        return _adaptive_result()

    async def _run_blocking(func, *, timeout_seconds):
        assert timeout_seconds == 91.0
        return func()

    async def _runner() -> dict[str, object]:
        payload = await mod._handle_von_chat_run({"prompt": "Hello"})
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_llm_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "src.backend.services.adaptive_turn_service.execute_adaptive_turn",
        _adaptive,
    )
    monkeypatch.setattr(mod, "build_default_catalogue", lambda: object())
    monkeypatch.setattr(mod, "InternalMCPTransport", lambda: object())
    monkeypatch.setattr(
        mod,
        "InternalMCPGateway",
        lambda **_kwargs: _StubGateway(),
    )
    monkeypatch.setattr(
        mod,
        "_evaluate_stdio_write_access",
        lambda *_args, **_kwargs: (True, _access_profile(), {}),
    )
    monkeypatch.setattr(mod, "get_preferred_language", lambda: None)
    monkeypatch.setattr(mod, "_run_blocking_with_timeout", _run_blocking)

    response = asyncio.run(_runner())

    assert response["success"] is True
    assert response["allow_writes"] is False
    assert response["dry_run"] is True
    assert response["delegation"] == "read_only"


def test_handle_von_chat_run_rejects_effect_delegation_request(monkeypatch) -> None:
    from src.backend.mcp_server import mcp_stdio_server as mod

    async def _runner() -> dict[str, object]:
        payload = await mod._handle_von_chat_run(
            {"prompt": "Change something", "allow_writes": True}
        )
        return json.loads(payload[0].text)

    monkeypatch.setenv("VON_INTERNAL_MCP_ENABLE", "1")
    monkeypatch.setattr(
        "src.backend.languagemodels.llm_interface.get_active_model_name",
        lambda: "test-model",
    )
    monkeypatch.setattr(
        mod,
        "_evaluate_stdio_write_access",
        lambda *_args, **_kwargs: (True, _access_profile(), {}),
    )

    response = asyncio.run(_runner())

    assert response["success"] is False
    assert response["error_code"] == "von_chat_run_read_only"
    assert response["allow_writes"] is False
