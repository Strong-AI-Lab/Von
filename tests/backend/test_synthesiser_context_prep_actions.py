"""Unit tests for ``workflows.durable.synthesiser_context_prep_actions``."""

from __future__ import annotations

from typing import Any

import pytest

from src.backend.services.output_hint_contracts import (
    OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
    OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
)
from src.backend.services.synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
    SynthesiserContextFramingTemplate,
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
    prompt_contract: dict[str, Any] | None = None,
    workflow_state_metadata: dict[str, Any] | None = None,
) -> WorkflowActionRequest:
    return WorkflowActionRequest(
        action_id=SYNTHESISER_CONTEXT_PREP_ACTION_ID,
        inputs=inputs or {},
        environment=_env(),
        data=data if data is not None else {},
        prompt_contract=prompt_contract,
        workflow_state_metadata=workflow_state_metadata,
    )


def _represented_template(
    prompt_concept_id: str = "#V#test_synthesiser_context_framing_prompt",
) -> SynthesiserContextFramingTemplate:
    return SynthesiserContextFramingTemplate(
        prompt_concept_id=prompt_concept_id,
        loaded_prompt_concept_id=prompt_concept_id,
        schema_version=SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
        active_request_template="AUTH active request: {active_user_message}",
        tool_hints_template="AUTH tool {tool_concept_id}:\n{hint_sections}",
        collection_presentation_hint_template=(
            "AUTH collection: {collection_presentation_hint}"
        ),
        item_summary_hint_template="AUTH item: {item_summary_hint}",
        diagnostics={
            "loaded_prompt_concept_id": prompt_concept_id,
            "schema_version": SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
        },
    )


def _install_represented_template(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prompt_concept_id: str = "#V#test_synthesiser_context_framing_prompt",
) -> list[str | None]:
    requested_prompt_ids: list[str | None] = []
    template = _represented_template(prompt_concept_id)

    def fake_resolve(*, prompt_concept_id: str | None = None, **_kwargs: Any):
        requested_prompt_ids.append(prompt_concept_id)
        return template, dict(template.diagnostics)

    monkeypatch.setattr(mod, "resolve_synthesiser_context_framing_template", fake_resolve)
    return requested_prompt_ids


def test_handler_stages_active_user_message(
    registry: ActionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No tool concepts -> only the active-request system message.
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")
    _install_represented_template(monkeypatch)

    data: dict[str, Any] = {"user_message_text": "What's on my plate?"}
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert result.status == "success"
    messages = result.outputs["system_messages"]
    assert messages == ["AUTH active request: What's on my plate?"]
    message_records = result.outputs["system_message_records"]
    assert message_records[0]["source"] == "vontology_prompt_template"
    assert (
        message_records[0]["source_prompt_concept_id"]
        == "#V#test_synthesiser_context_framing_prompt"
    )
    assert message_records[0]["template_field"] == "active_request_template"
    assert result.outputs["tool_concept_ids_seen"] == []
    assert result.outputs["hints_resolved_count"] == 0
    assert result.outputs["active_user_message_present"] is True
    assert result.outputs["synthesiser_system_messages"] == message_records
    # Staged into shared turn data.
    assert data["synthesiser_system_messages"] == message_records


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
    _install_represented_template(monkeypatch)

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
    assert messages[0] == "AUTH active request: show me everything"
    alpha_msg = next(m for m in messages if "#V#tool_alpha" in m)
    beta_msg = next(m for m in messages if "#V#tool_beta" in m)
    assert "AUTH collection: Group alpha items by date." in alpha_msg
    assert "AUTH item: Summarise alpha by title only." in alpha_msg
    assert "AUTH item: Summarise beta by score." in beta_msg
    assert all("#V#tool_gamma" not in m for m in messages)
    alpha_record = next(
        message
        for message in result.outputs["system_message_records"]
        if message.get("tool_concept_id") == "#V#tool_alpha"
    )
    assert alpha_record["template_field"] == "tool_hints_template"
    assert alpha_record["hint_predicate_ids"] == [
        OUTPUT_COLLECTION_PRESENTATION_HINT_PREDICATE_ID,
        OUTPUT_ITEM_SUMMARY_HINT_PREDICATE_ID,
    ]

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
    _install_represented_template(monkeypatch)

    data = {"recent_user_prompts": ["older message", "latest message"]}
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert result.outputs["system_messages"] == ["AUTH active request: latest message"]


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
    assert result.outputs["context_framing_template_required"] is False


def test_handler_fails_closed_when_represented_template_missing(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")
    monkeypatch.setattr(
        mod,
        "resolve_synthesiser_context_framing_template",
        lambda **_kwargs: (
            None,
            {"error": "synthesiser_context_framing_prompt_missing_or_empty"},
        ),
    )

    req = _make_request(data={"user_message_text": "needs a represented template"})
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert result.status == "failed"
    assert result.error == "synthesiser_context_framing_template_unavailable"
    assert result.outputs["result"] is False
    assert result.outputs["context_framing_template_required"] is True
    assert (
        result.outputs["context_framing_template_diagnostics"]["error"]
        == "synthesiser_context_framing_prompt_missing_or_empty"
    )
    assert "synthesiser_system_messages" not in req.data


def test_handler_uses_prompt_contract_for_represented_template(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")
    requested_prompt_ids = _install_represented_template(
        monkeypatch,
        prompt_concept_id="#V#custom_synthesiser_context_framing_prompt",
    )

    req = _make_request(
        data={"user_message_text": "contract prompt"},
        prompt_contract={
            "resolved_prompt_concept_id": (
                "#V#custom_synthesiser_context_framing_prompt"
            )
        },
    )
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert result.status == "success"
    assert requested_prompt_ids == ["#V#custom_synthesiser_context_framing_prompt"]
    assert result.outputs["system_messages"] == [
        "AUTH active request: contract prompt"
    ]


def test_handler_inputs_override_data(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")
    _install_represented_template(monkeypatch)

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
    _install_represented_template(monkeypatch)

    data: dict[str, Any] = {
        "user_message_text": "hello",
        "synthesiser_system_messages": ["pre-existing entry"],
    }
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    result = spec.handler(req)

    assert "pre-existing entry" in data["synthesiser_system_messages"]
    assert any(
        isinstance(message, dict)
        and message.get("content") == "AUTH active request: hello"
        for message in data["synthesiser_system_messages"]
    )
    assert (
        result.outputs["synthesiser_system_messages"]
        == data["synthesiser_system_messages"]
    )


def test_handler_dedupes_repeated_invocations(
    registry: ActionRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "resolve_hint_body", lambda *a, **k: "")
    _install_represented_template(monkeypatch)

    data: dict[str, Any] = {
        "user_message_text": "repeat",
        "synthesiser_system_messages": [
            {"role": "system", "content": "AUTH active request: repeat"}
        ],
    }
    req = _make_request(data=data)
    spec = registry.get(SYNTHESISER_CONTEXT_PREP_ACTION_ID)
    assert spec is not None

    spec.handler(req)

    # Should not duplicate the active-request line.
    occurrences = [
        m
        for m in data["synthesiser_system_messages"]
        if isinstance(m, dict) and m.get("content") == "AUTH active request: repeat"
    ]
    assert len(occurrences) == 1
