"""Unit tests for ``services.tool_result_hints``."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from src.backend.services import tool_result_hints
from src.backend.services.output_hint_contracts import (
    OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID,
)
from src.backend.services.tool_result_hints import (
    LLMGenerationError,
    LLMGenerationResult,
    StructuredSignals,
    extract_signals_from_tool_result,
    resolve_hint_body,
)


class _FakeLLM:
    def __init__(self, response: str | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[list] = None,
        model: Optional[str] = None,
        llm_params: Optional[dict] = None,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "context": context, "model": model, "llm_params": llm_params}
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def test_resolve_hint_body_returns_empty_for_blank_inputs() -> None:
    assert resolve_hint_body("", "predicate") == ""
    assert resolve_hint_body("#V#tool_x", "") == ""


def test_resolve_hint_body_returns_first_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get_texts(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return [{"text": "  apply this hint  "}]

    monkeypatch.setattr(tool_result_hints, "get_texts_for_concept", fake_get_texts)

    assert resolve_hint_body("#V#tool_x", "#V#predicate_y") == "apply this hint"
    assert captured["subject_concept_id"] == "#V#tool_x"
    assert captured["predicate"] == "#V#predicate_y"
    assert captured["lang"] == "en-NZ"
    assert captured["limit"] == 1


def test_resolve_hint_body_swallows_service_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_: Any) -> list:
        raise RuntimeError("vontology offline")

    monkeypatch.setattr(tool_result_hints, "get_texts_for_concept", boom)

    assert resolve_hint_body("#V#tool_x", "#V#predicate_y") == ""


def test_extract_signals_returns_unresolved_when_hint_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints, "get_texts_for_concept", lambda **_: []
    )
    llm = _FakeLLM(response="should not be called")

    result = extract_signals_from_tool_result(
        "#V#tool_x", {"items": [1]}, None, llm_client=llm
    )

    assert isinstance(result, StructuredSignals)
    assert result.hint_resolved is False
    assert result.signals == {}
    assert result.warnings == ("hint_not_authored",)
    assert llm.calls == []


def test_extract_signals_parses_plain_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "Extract the senders."}],
    )
    llm = _FakeLLM(response='{"senders": ["alice", "bob"]}')

    result = extract_signals_from_tool_result(
        "#V#tool_x",
        {"items": [{"from": "alice"}, {"from": "bob"}]},
        {"prompt": "who wrote me?"},
        llm_client=llm,
    )

    assert result.hint_resolved is True
    assert result.signals == {"senders": ["alice", "bob"]}
    assert result.warnings == ()
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["prompt"]
    assert "Extract the senders." in prompt
    assert '"items"' in prompt  # payload was JSON-serialised into the prompt


def test_extract_signals_respects_larger_authored_payload_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "Extract every reference."}],
    )
    llm = _FakeLLM(response='{"items": []}')
    tail_marker = "https://arxiv.org/abs/2608.12345"
    payload = {"body": ("earlier context " * 1_000) + tail_marker}

    result = extract_signals_from_tool_result(
        "#V#tool_x",
        payload,
        None,
        llm_client=llm,
        payload_max_chars=30_000,
    )

    assert tail_marker in llm.calls[0]["prompt"]
    assert result.payload_max_chars == 30_000
    assert result.payload_serialised_chars > 12_000
    assert result.payload_included_chars == result.payload_serialised_chars
    assert result.payload_truncated is False


def test_extract_signals_reports_default_payload_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "Extract every reference."}],
    )
    llm = _FakeLLM(response='{"items": []}')
    tail_marker = "https://arxiv.org/abs/2608.12345"

    result = extract_signals_from_tool_result(
        "#V#tool_x",
        {"body": ("earlier context " * 1_000) + tail_marker},
        None,
        llm_client=llm,
    )

    assert tail_marker not in llm.calls[0]["prompt"]
    assert result.payload_max_chars == 12_000
    assert result.payload_included_chars == 12_000
    assert result.payload_serialised_chars > result.payload_included_chars
    assert result.payload_truncated is True


def test_extract_signals_strips_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "Return JSON."}],
    )
    llm = _FakeLLM(response='```json\n{"x": 1}\n```')

    result = extract_signals_from_tool_result(
        "#V#tool_x", {}, None, llm_client=llm
    )

    assert result.signals == {"x": 1}
    assert result.warnings == ()


def test_extract_signals_extracts_object_with_preamble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "hint"}],
    )
    llm = _FakeLLM(response='Here is the result:\n{"a": 1, "b": [2, 3]}\nDone.')

    result = extract_signals_from_tool_result(
        "#V#tool_x", {}, None, llm_client=llm
    )

    assert result.signals == {"a": 1, "b": [2, 3]}


def test_extract_signals_warns_on_unparseable_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "hint"}],
    )
    llm = _FakeLLM(response="not json at all")

    result = extract_signals_from_tool_result(
        "#V#tool_x", {}, None, llm_client=llm
    )

    assert result.signals == {}
    assert "json_parse_failed" in result.warnings
    assert result.hint_resolved is True


def test_extract_signals_handles_llm_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "hint"}],
    )
    llm = _FakeLLM(response=RuntimeError("model unavailable"))

    result = extract_signals_from_tool_result(
        "#V#tool_x", {}, None, llm_client=llm
    )

    assert result.signals == {}
    assert any(w.startswith("llm_error:") for w in result.warnings)
    assert result.hint_resolved is True


def test_extract_signals_accepts_generation_hook_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "hint"}],
    )
    llm_calls = ({"type": "llm.generate", "stage": "signal_extraction"},)
    aux_calls = (
        {
            "type": "workflow_model_policy_stage",
            "fallback_attempt_count": 2,
        },
    )

    def generate(**_: Any) -> LLMGenerationResult:
        return LLMGenerationResult(
            response='{"ok": true}',
            selected_model="granite3.3:2b",
            selected_candidate={"provider": "ollama"},
            llm_calls=llm_calls,
            aux_llm_calls=aux_calls,
        )

    result = extract_signals_from_tool_result(
        "#V#tool_x",
        {"items": [1]},
        None,
        llm_client=None,
        llm_generate=generate,
    )

    assert result.signals == {"ok": True}
    assert result.selected_model == "granite3.3:2b"
    assert result.selected_candidate == {"provider": "ollama"}
    assert result.llm_calls == llm_calls
    assert result.aux_llm_calls == aux_calls


def test_extract_signals_preserves_generation_failure_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tool_result_hints,
        "get_texts_for_concept",
        lambda **_: [{"text": "hint"}],
    )
    aux_calls = (
        {
            "type": "workflow_model_policy_stage",
            "fallback_attempts": [{"failure_kind": "quota_exhausted"}],
        },
    )

    def generate(**_: Any) -> LLMGenerationResult:
        raise LLMGenerationError(
            "insufficient_quota",
            error_class="RateLimitError",
            aux_llm_calls=aux_calls,
        )

    result = extract_signals_from_tool_result(
        "#V#tool_x",
        {"items": [1]},
        None,
        llm_client=None,
        llm_generate=generate,
    )

    assert result.signals == {}
    assert result.warnings == ("llm_error:RateLimitError",)
    assert result.aux_llm_calls == aux_calls


def test_extract_signals_default_predicate_is_canonical() -> None:
    """Guard against accidental drift of the canonical hint predicate id."""

    import inspect

    sig = inspect.signature(extract_signals_from_tool_result)
    default = sig.parameters["hint_predicate_id"].default
    assert default == OUTPUT_ITEM_SIGNAL_EXTRACTION_HINT_PREDICATE_ID


def test_extract_signals_no_llm_client() -> None:
    # Even with a hint resolved, a missing client should not crash.
    import unittest.mock as mock

    with mock.patch.object(
        tool_result_hints,
        "get_texts_for_concept",
        return_value=[{"text": "hint body"}],
    ):
        result = extract_signals_from_tool_result(
            "#V#tool_x", {}, None, llm_client=None
        )

    assert result.hint_resolved is True
    assert result.signals == {}
    assert "no_llm_client" in result.warnings
