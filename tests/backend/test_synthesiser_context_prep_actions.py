"""Unit tests for ``workflows.durable.synthesiser_context_prep_actions``."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.output_hint_contracts import (
    OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable import synthesiser_context_prep_actions as mod
from src.backend.workflows.durable.synthesiser_context_prep_actions import (
    SYNTHESISER_CONTEXT_PREP_ACTION_ID,
    register_synthesiser_context_prep_actions,
)


@pytest.fixture
def registry() -> ActionRegistry:
    reg = ActionRegistry()
    register_synthesiser_context_prep_actions(reg)
    return reg


def _env() -> WorkflowEnvironment:
    return WorkflowEnvironment(llm_client=None)


def test_register_synthesiser_context_prep_actions_adds_action(
    registry: ActionRegistry,
) -> None:
    assert SYNTHESISER_CONTEXT_PREP_ACTION_ID in set(registry.all_action_ids())


def test_register_is_idempotent() -> None:
    reg = ActionRegistry()
    register_synthesiser_context_prep_actions(reg)
    register_synthesiser_context_prep_actions(reg)  # must not raise
    ids = list(reg.all_action_ids())
    assert ids.count(SYNTHESISER_CONTEXT_PREP_ACTION_ID) == 1


def _make_request(
    *,
    inputs: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id=SYNTHESISER_CONTEXT_PREP_ACTION_ID,
        inputs=inputs or {},
        environment=_env(),
        data=data if data is not None else {},
    )


def test_handler_stages_active_user_message(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No tool concepts -> only the active-request system message.
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")

    data: dict[str, Any] = {"user_message_text": "What's on my plate?"}
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert result.status == "success"
    messages = result.outputs["system_messages"]
    assert messages == ["Active request for this turn: What's on my plate?"]
    assert result.outputs["tool_concept_ids_seen"] == []
    assert result.outputs["hints_resolved_count"] == 0
    assert result.outputs["active_user_message_present"] is True
    assert result.outputs["synthesiser_system_messages"] == messages
    # Staged into shared turn data.
    assert data["synthesiser_system_messages"] == messages


def test_handler_resolves_hints_per_tool_concept(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_calls: list[tuple[str, str]] = []

    def fake_resolve(
        tool_concept_id: str, predicate_id: str, *, lang: str = "en-NZ"
    ) -> str:
        captured_calls.append((tool_concept_id, predicate_id))
        if tool_concept_id == "#V#tool_alpha":
            if predicate_id == OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID:
                return "Group alpha items by date."
            if predicate_id == OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID:
                return "Summarise alpha by title only."
        if tool_concept_id == "#V#tool_beta":
            if predicate_id == OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID:
                return "Summarise beta by score."
        return ""

    monkeypatch.setattr(mod, "resolve_hint_body", fake_resolve)

    data: dict[str, Any] = {
        "prompt": "show me everything",
        "invocations": [
            {"tool_concept_id": "#V#tool_alpha"},
            {"tool": "#V#tool_beta"},  # fallback key
            {"tool_concept_id": "#V#tool_gamma"},  # no hints authored
            {"tool_concept_id": "#V#tool_alpha"},  # duplicate, deduped
        ],
    }
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    messages = result.outputs["system_messages"]
    # First message: active request; then two tools with hints (gamma dropped).
    assert messages[0] == "Active request for this turn: show me everything"
    alpha_msg = next(m for m in messages if "#V#tool_alpha" in m)
    beta_msg = next(m for m in messages if "#V#tool_beta" in m)
    assert "Group alpha items by date." in alpha_msg
    assert "Summarise alpha by title only." in alpha_msg
    assert "Summarise beta by score." in beta_msg
    assert all("#V#tool_gamma" not in m for m in messages)

    assert result.outputs["tool_concept_ids_seen"] == [
        "#V#tool_alpha",
        "#V#tool_beta",
        "#V#tool_gamma",
    ]
    assert result.outputs["hints_resolved_count"] == 2

    # Both hint predicates were consulted for each unique tool.
    assert len(captured_calls) == 6  # 3 tools x 2 predicates


def test_handler_falls_back_to_recent_user_prompts(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")

    data = {"recent_user_prompts": ["older message", "latest message"]}
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert result.outputs["system_messages"] == [
        "Active request for this turn: latest message"
    ]


def test_handler_handles_no_user_message_and_no_tools(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")

    req = _make_request(data={})
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)
    assert result.status == "success"
    assert result.outputs["system_messages"] == []
    assert result.outputs["synthesiser_system_messages"] == []
    assert result.outputs["active_user_message_present"] is False
    assert req.data["synthesiser_system_messages"] == []


def test_handler_inputs_override_data(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")

    req = _make_request(
        inputs={
            "user_message_text": "from inputs",
            "invocations": [{"tool_concept_id": "#V#tool_x"}],
        },
        data={"user_message_text": "from data", "invocations": []},
    )
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert "from inputs" in result.outputs["system_messages"][0]
    assert result.outputs["tool_concept_ids_seen"] == ["#V#tool_x"]


def test_handler_appends_to_existing_synth_messages(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")

    data: dict[str, Any] = {
        "user_message_text": "hello",
        "synthesiser_system_messages": ["pre-existing entry"],
    }
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert "pre-existing entry" in data["synthesiser_system_messages"]
    assert "Active request for this turn: hello" in data["synthesiser_system_messages"]
    assert (
        result.outputs["synthesiser_system_messages"]
        == data["synthesiser_system_messages"]
    )


def test_handler_dedupes_repeated_invocations(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")

    data: dict[str, Any] = {
        "user_message_text": "repeat",
        "synthesiser_system_messages": ["Active request for this turn: repeat"],
    }
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    spec.handler(req)

    # Should not duplicate the active-request line.
    occurrences = [
        m
        for m in data["synthesiser_system_messages"]
        if m == "Active request for this turn: repeat"
    ]
    assert len(occurrences) == 1
