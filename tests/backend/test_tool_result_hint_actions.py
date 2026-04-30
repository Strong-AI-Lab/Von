"""Unit tests for ``workflows.durable.tool_result_hint_actions``."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.output_hint_contracts import (
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from src.backend.services import tool_result_hints as svc
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


def _env(llm_client: Any) -> WorkflowEnvironment:
    return WorkflowEnvironment(llm_client=llm_client)


def _make_request(
    *,
    inputs: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    llm_client: Any = None,
) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id=EXTRACT_SIGNALS_FROM_TOOL_RESULT_ACTION_ID,
        inputs=inputs or {},
        environment=_env(llm_client),
        data=data if data is not None else {},
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
