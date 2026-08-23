"""Unit tests for ``workflows.durable.tool_result_hint_actions``."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.services import tool_result_hints as svc
from src.backend.services.output_hint_contracts import (
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.tool_result_hint_actions import (
    EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID,
    RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID,
    register_tool_result_hint_actions,
)


class _FakeLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        *,
        prompt: str,
        context: Any | None = None,
        model: str | None = None,
    ) -> str:
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        return self.response


@pytest.fixture
def registry() -> ActionRegistry:
    reg = ActionRegistry()
    register_tool_result_hint_actions(reg)
    return reg


def _env(
    llm_client: Any,
    *,
    gateway: Any | None = None,
    model: str | None = None,
) -> WorkflowEnvironment:
    return WorkflowEnvironment(llm_client=llm_client, gateway=gateway, model=model)


def _make_request(
    *,
    inputs: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    llm_client: Any = None,
    gateway: Any | None = None,
    model: str | None = None,
    workflow_id: str | None = None,
    workflow_state_id: str | None = None,
) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id=EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID,
        inputs=inputs or {},
        environment=_env(llm_client, gateway=gateway, model=model),
        data=data if data is not None else {},
        workflow_id=workflow_id,
        workflow_state_id=workflow_state_id,
    )


def test_action_registers(registry: ActionRegistry) -> None:
    assert EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID in set(registry.all_action_ids())
    assert RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID in set(registry.all_action_ids())


def test_register_is_idempotent() -> None:
    reg = ActionRegistry()
    register_tool_result_hint_actions(reg)
    register_tool_result_hint_actions(reg)  # must not raise
    ids = list(reg.all_action_ids())
    assert ids.count(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID) == 1
    assert ids.count(RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID) == 1


def test_missing_source_tool_concept_fails(registry: ActionRegistry) -> None:
    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(inputs={"tool_payload": {"x": 1}}, llm_client=_FakeLLM(""))
    result = spec.handler(req)
    assert result.status == "failed"
    assert "source_tool_concept" in (result.error or "")


def test_missing_payload_fails(registry: ActionRegistry) -> None:
    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={"source_tool_concept": "#V#some_tool"},
        llm_client=_FakeLLM(""),
    )
    result = spec.handler(req)
    assert result.status == "failed"
    assert "tool_payload" in (result.error or "")


def test_happy_path_returns_signals(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": "Extract things.",
    )
    fake_llm = _FakeLLM('{"items": [{"type": "url", "url": "https://example.com"}]}')

    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None

    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "tool_payload": {"body": "see https://example.com"},
        },
        llm_client=fake_llm,
    )
    result = spec.handler(req)

    assert result.status == "success", result.error
    assert result.outputs["hint_resolved"] is True
    assert result.outputs["hint_body_present"] is True
    assert result.outputs["source_tool_concept"] == "#V#some_tool"
    assert (
        result.outputs["hint_predicate_id"]
        == OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID
    )
    signals = result.outputs["signals"]
    assert isinstance(signals, dict)
    assert signals["items"][0]["url"] == "https://example.com"
    assert "Extract things." in fake_llm.calls[0]["prompt"]
    assert result.outputs["payload_max_chars"] == 12_000
    assert result.outputs["payload_truncated"] is False


def test_action_applies_authored_payload_bound(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": "Extract things.",
    )
    fake_llm = _FakeLLM('{"items": []}')
    tail_marker = "https://arxiv.org/abs/2608.12345"
    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        _make_request(
            inputs={
                "source_tool_concept": "#V#some_tool",
                "tool_payload": {
                    "body": ("earlier context " * 1_000) + tail_marker
                },
                "payload_max_chars": 100_000,
            },
            llm_client=fake_llm,
        )
    )

    assert result.status == "success", result.error
    assert tail_marker in fake_llm.calls[0]["prompt"]
    assert result.outputs["payload_max_chars"] == 100_000
    assert result.outputs["payload_truncated"] is False


def test_action_rejects_invalid_payload_bound(registry: ActionRegistry) -> None:
    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        _make_request(
            inputs={
                "source_tool_concept": "#V#some_tool",
                "tool_payload": {"body": "paper"},
                "payload_max_chars": 500,
            },
            llm_client=_FakeLLM('{"items": []}'),
        )
    )

    assert result.status == "failed"
    assert "payload_max_chars" in (result.error or "")


def test_payload_falls_back_to_data(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": "ok.",
    )
    fake_llm = _FakeLLM('{"items": []}')

    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={"source_tool_concept": "#V#some_tool"},
        data={"tool_payload": {"body": "no links"}},
        llm_client=fake_llm,
    )
    result = spec.handler(req)
    assert result.status == "success"
    assert result.outputs["signals"] == {"items": []}


def test_action_uses_workflow_model_policy_fallback_with_gateway(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": "Extract signals.",
    )
    captured: dict[str, Any] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs: Any):
            captured.update(kwargs)
            kwargs["record_llm_call"](
                call_type="llm.generate",
                model_name="gpt-4.1-mini",
                requested_model_name="gpt-4.1-mini",
                duration_ms=10,
                note="llm.generate failed; trying fallback",
                stage=kwargs["stage"],
                provider="openai",
                candidate={"provider": "openai"},
                workflow_stage_id=kwargs["workflow_stage_id"],
            )
            kwargs["record_llm_call"](
                call_type="llm.generate",
                model_name="granite3.3:2b",
                requested_model_name="gpt-4.1-mini",
                duration_ms=5,
                note="Fallback chain generate()",
                stage=kwargs["stage"],
                provider="ollama",
                candidate={"provider": "ollama"},
                workflow_stage_id=kwargs["workflow_stage_id"],
            )
            kwargs["aux_log"].append(
                {
                    "type": "workflow_model_policy_stage",
                    "stage": kwargs["stage"],
                    "policy_stage": kwargs["policy_stage"],
                    "fallback_used": True,
                    "fallback_attempt_count": 2,
                    "fallback_attempts": [
                        {
                            "attempt_no": 1,
                            "provider": "openai",
                            "status": "failed",
                            "failure_kind": "quota_exhausted",
                        },
                        {
                            "attempt_no": 2,
                            "provider": "ollama",
                            "status": "succeeded",
                        },
                    ],
                }
            )
            return (
                '{"items": [{"kind": "url", "url": "https://example.com"}]}',
                "granite3.3:2b",
                {"provider": "ollama", "model": "granite3.3:2b"},
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), {"models": []}, "user", "org"),
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._prefer_default_model_for_request",
        lambda request: False,
    )

    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "tool_payload": {"body": "see https://example.com"},
            "policy_stage": "signal_extraction",
        },
        llm_client=_FakeLLM("should not be called by stub"),
        gateway=object(),
        model="gpt-4.1-mini",
        workflow_id="#V#workflow_x",
        workflow_state_id="extract",
    )

    result = spec.handler(req)

    assert result.status == "success", result.error
    assert captured["stage"] == EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID
    assert captured["policy_stage"] == "signal_extraction"
    assert captured["default_model"] == "gpt-4.1-mini"
    assert captured["workflow_stage_id"] == "extract"
    assert result.outputs["signals"]["items"][0]["url"] == "https://example.com"
    assert result.outputs["selected_model"] == "granite3.3:2b"
    assert result.outputs["selected_model_candidate"]["provider"] == "ollama"
    assert result.outputs["llm_calls"][0]["provider"] == "openai"
    assert result.outputs["llm_calls"][0]["requested_model"] == "gpt-4.1-mini"
    assert result.outputs["llm_calls"][0]["selected_model"] == "gpt-4.1-mini"
    stage_summary = result.outputs["aux_llm_calls"][0]
    assert stage_summary["fallback_attempt_count"] == 2
    assert stage_summary["fallback_attempts"][0]["failure_kind"] == "quota_exhausted"


def test_action_preserves_model_policy_failure_telemetry(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": "Extract signals.",
    )

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs: Any):
            kwargs["record_llm_call"](
                call_type="llm.generate",
                model_name="gpt-4.1-mini",
                duration_ms=10,
                note="llm.generate failed; trying fallback",
                stage=kwargs["stage"],
                provider="openai",
                candidate={"provider": "openai"},
            )
            kwargs["aux_log"].append(
                {
                    "type": "workflow_model_policy_stage",
                    "stage": kwargs["stage"],
                    "policy_stage": kwargs["policy_stage"],
                    "fallback_used": True,
                    "fallback_attempt_count": 1,
                    "fallback_attempts": [
                        {
                            "attempt_no": 1,
                            "provider": "openai",
                            "status": "failed",
                            "failure_kind": "quota_exhausted",
                        }
                    ],
                }
            )
            raise RuntimeError("insufficient_quota")

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), {"models": []}, "user", "org"),
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._prefer_default_model_for_request",
        lambda request: False,
    )

    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "tool_payload": {"body": "x"},
            "policy_stage": "signal_extraction",
        },
        llm_client=None,
        gateway=object(),
        model="gpt-4.1-mini",
    )

    result = spec.handler(req)

    assert result.status == "failed"
    assert "llm_error:RuntimeError" in (result.error or "")
    assert result.outputs["llm_calls"][0]["provider"] == "openai"
    stage_summary = result.outputs["aux_llm_calls"][0]
    assert stage_summary["fallback_attempt_count"] == 1
    assert stage_summary["fallback_attempts"][0]["failure_kind"] == "quota_exhausted"


def test_hint_not_authored_returns_failure(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        svc,
        "resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": "",
    )
    fake_llm = _FakeLLM("")

    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "tool_payload": {"body": "x"},
        },
        llm_client=fake_llm,
    )
    result = spec.handler(req)
    assert result.status == "failed"
    assert "hint_not_authored" in (result.error or "")


def test_no_llm_client_fails_without_calling_service(registry: ActionRegistry) -> None:
    spec = registry.get(EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "tool_payload": {"body": "x"},
        },
        llm_client=None,
    )
    result = spec.handler(req)
    assert result.status == "failed"
    assert "LLM client" in (result.error or "")


def test_resolve_followup_hint_selects_required_action_kind(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.tool_result_hint_actions.resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": (
            '{"schema_version":"tool_output_followup_hint.v1",'
            '"entries":[{"action_kind":"terminal_completion",'
            '"action":{"type":"workflow_mcp.invoke_tool",'
            '"tool_name":"some_tool",'
            '"tool_arguments":{"add_labels":["done"]}},'
            '"upstream_filter":{"query_fragment":"-label:done"}}]}'
        ),
    )

    spec = registry.get(RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "required_action_kind": "terminal_completion",
        },
    )

    result = spec.handler(req)

    assert result.status == "success", result.error
    assert result.outputs["hint_resolved"] is True
    assert result.outputs["selected_action"]["tool_name"] == "some_tool"
    assert result.outputs["selected_tool_arguments"]["add_labels"] == ["done"]
    assert (
        result.outputs["selected_upstream_filter"]["query_fragment"]
        == "-label:done"
    )


def test_resolve_followup_hint_accepts_entry_level_tool_arguments(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.tool_result_hint_actions.resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": (
            '{"schema_version":"tool_output_followup_hint.v1",'
            '"entries":[{"action_kind":"terminal_completion",'
            '"action":{"type":"workflow_mcp.invoke_tool",'
            '"tool_name":"some_tool"},'
            '"tool_arguments":{"add_labels":["done"]}}]}'
        ),
    )

    spec = registry.get(RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "required_action_kind": "terminal_completion",
        },
    )

    result = spec.handler(req)

    assert result.status == "success", result.error
    assert result.outputs["selected_action"]["tool_name"] == "some_tool"
    assert result.outputs["selected_tool_arguments"]["add_labels"] == ["done"]


def test_resolve_followup_hint_fails_when_required_kind_missing(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.durable.tool_result_hint_actions.resolve_hint_body",
        lambda concept_id, predicate_id, lang="en-NZ": (
            '{"schema_version":"tool_output_followup_hint.v1","entries":[]}'
        ),
    )

    spec = registry.get(RESOLVE_TOOL_OUTPUT_FOLLOWUP_HINT_ACTION_ID)
    assert spec is not None
    req = _make_request(
        inputs={
            "source_tool_concept": "#V#some_tool",
            "required_action_kind": "terminal_completion",
        },
    )

    result = spec.handler(req)

    assert result.status == "failed"
    assert "terminal_completion" in (result.error or "")
