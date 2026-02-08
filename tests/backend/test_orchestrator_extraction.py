import pytest
import sys
from typing import Any, Mapping
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    ToolCallParsingError,
    _MissingToolCallDetectorSpec,
)


class _DummyGateway:
    enabled = True

    def __init__(self):
        self.calls = []

    def describe_methods(self):
        return {"test": {"description": "dummy"}}

    def invoke(self, method_name, payload):
        self.calls.append((method_name, dict(payload)))

        class Result:
            def __init__(self):
                self.payload = {"ok": True}
                self.duration_ms = 1.0

        return Result()


class _RecorderLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if not self.responses:
            raise RuntimeError("No responses left in _RecorderLLM")
        return self.responses.pop(0)


def test_instruction_message_requires_verification_tool_calls_for_concept_existence():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    message = orchestrator._instruction_message(user_namespace="#V#user")
    assert "VERIFICATION & CONSISTENCY RULES" in message
    assert "fetch_concept" in message
    assert "search_concepts" in message


def test_extract_tool_calls_accepts_single_tool_call():
    text = '{"action": "call_tool", "tool": "test", "payload": {}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls == [{"action": "call_tool", "tool": "test", "payload": {}}]


def test_extract_tool_calls_tolerates_trailing_json_marker_suffix():
    """Regression test: tolerate trailing markers like "[json]".

    Some models append a non-JSON suffix such as "[json]" after emitting a valid
    tool-call JSON payload. This should not be treated as a second JSON value.
    """

    text = '{"action":"call_tool","tool":"test","payload":{}}[json]'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls == [{"action": "call_tool", "tool": "test", "payload": {}}]


def test_extract_tool_calls_rejects_concatenated_json_values():
    """Still reject truly concatenated JSON values."""

    text = (
        '{"action":"call_tool","tool":"test","payload":{}}'
        '{"action":"call_tool","tool":"test","payload":{}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    with pytest.raises(ToolCallParsingError) as excinfo:
        orchestrator._extract_tool_calls(text)
    assert "multiple JSON values" in str(excinfo.value)


def test_extract_tool_calls_accepts_whitespace_separated_json_values():
    text = (
        '{"action":"call_tool","tool":"alpha","payload":{}}\n'
        '{"action":"call_tool","tool":"beta","payload":{"x":1}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert [call["tool"] for call in calls] == ["alpha", "beta"]
    assert calls[1]["payload"]["x"] == 1


def test_extract_tool_calls_accepts_json_array_batch():
    text = (
        '[{"action":"call_tool","tool":"test","payload":{}},'
        '{"action":"call_tool","tool":"test","payload":{"a":1}}]'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls("".join(text))
    assert calls is not None
    assert len(calls) == 2
    assert calls[0]["tool"] == "test"
    assert calls[1]["payload"]["a"] == 1


def test_extract_tool_calls_repairs_truncated_json_array():
    text = (
        '[{"action":"call_tool","tool":"test","payload":{}},'
        '{"action":"call_tool","tool":"test","payload":{"a":1}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert len(calls) == 2
    assert calls[1]["payload"]["a"] == 1


def test_interpret_model_turn_ignores_non_tool_json_without_error():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    interpretation = orchestrator._interpret_model_turn('{"foo": 1, "bar": [1, 2]}')
    assert interpretation.tool_calls is None
    assert interpretation.tool_call_parse_error is None
    assert interpretation.is_json_action is False


def test_interpret_model_turn_flags_tool_result_shaped_json_as_json_action():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    interpretation = orchestrator._interpret_model_turn(
        '{"tool": "test", "status": "ok", "payload": {"x": 1}}'
    )
    assert interpretation.tool_calls is None
    assert interpretation.tool_call_parse_error is None
    assert interpretation.is_json_action is True


def test_interpret_model_turn_captures_tool_call_parse_error():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    interpretation = orchestrator._interpret_model_turn(
        '{"action":"call_tool","tool":"test","payload":{"concept_id":"#V#foo}'
    )
    assert interpretation.tool_calls is None
    assert isinstance(interpretation.tool_call_parse_error, ToolCallParsingError)


def test_assess_missing_tool_call_prefers_parse_error_over_fence_and_classifier():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["NO"])
    aux_log: list[Mapping[str, Any]] = []

    assessment = orchestrator._assess_missing_tool_call(
        response_text="{bad json",
        use_structured=False,
        interpretation=None,
        llm_client=llm,
        model="primary-model",
        classifier_model="classifier-model",
        aux_log=aux_log,
        tool_call_parse_error=ToolCallParsingError("bad", raw_response="{bad json"),
    )

    assert assessment.retry_reason == "tool call parse error"


def test_assess_missing_tool_call_uses_fenced_json_reason_without_classifier_call():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["YES"])  # Would be consumed if classifier were called.
    aux_log: list[Mapping[str, Any]] = []

    interpretation = orchestrator._interpret_model_turn(
        '```json\n{"action":"call_tool","tool":"test","payload":{}}\n```'
    )

    assessment = orchestrator._assess_missing_tool_call(
        response_text=interpretation.response_text,
        use_structured=False,
        interpretation=interpretation,
        llm_client=llm,
        model="primary-model",
        classifier_model="classifier-model",
        aux_log=aux_log,
        tool_call_parse_error=None,
    )

    assert assessment.retry_reason == "fenced tool-call JSON detected"
    assert not llm.calls


def test_assess_missing_tool_call_retries_on_json_action_without_classifier_call():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["YES"])  # Would be consumed if classifier were called.
    aux_log: list[Mapping[str, Any]] = []

    interpretation = orchestrator._interpret_model_turn(
        '{"tool": "test", "status": "ok", "payload": {"x": 1}}'
    )
    assert interpretation.is_json_action

    assessment = orchestrator._assess_missing_tool_call(
        response_text=interpretation.response_text,
        use_structured=False,
        interpretation=interpretation,
        llm_client=llm,
        model="primary-model",
        classifier_model="classifier-model",
        aux_log=aux_log,
        tool_call_parse_error=None,
    )

    assert assessment.retry_reason == "JSON tool-call output detected"
    assert not llm.calls


def test_assess_missing_tool_call_backstops_classifier_no_with_heuristic_yes():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )

    llm = _RecorderLLM(["NO"])
    aux_log: list[Mapping[str, Any]] = []

    response_text = "I'll do that now and execute the tools."
    interpretation = orchestrator._interpret_model_turn(response_text)
    assert interpretation.heuristic_missing_tool_call

    assessment = orchestrator._assess_missing_tool_call(
        response_text=interpretation.response_text,
        use_structured=False,
        interpretation=interpretation,
        llm_client=llm,
        model="primary-model",
        classifier_model="classifier-model",
        aux_log=aux_log,
        tool_call_parse_error=None,
    )

    assert assessment.retry_reason == "heuristic missing tool call (classifier said no)"


def test_extract_tool_calls_accepts_fenced_json_array_batch():
    text = (
        "Here is the tool batch:\n"
        "```json\n"
        '[{"action":"call_tool","tool":"test","payload":{}},'
        '{"action":"call_tool","tool":"test","payload":{}}]\n'
        "```"
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert len(calls) == 2


def test_extract_tool_calls_accepts_unterminated_fenced_json_array_batch():
    text = (
        "```json\n"
        '[{"action":"call_tool","tool":"test","payload":{}},'
        '{"action":"call_tool","tool":"test","payload":{}}]\n'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls("".join(text))
    assert calls is not None
    assert len(calls) == 2


def test_interpret_model_turn_detects_unterminated_fenced_tool_call_json():
    text = '```json\n{"action":"call_tool","tool":"test","payload":{}}\n'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    interpretation = orchestrator._interpret_model_turn(text)
    assert interpretation.fenced_tool_call_json


def test_extract_tool_calls_accepts_missing_action_when_tool_is_known_in_batch():
    text = '[{"tool": "test", "payload": {}}, {"tool": "test", "payload": {"x": 2}}]'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert calls[0]["action"] == "call_tool"
    assert calls[1]["action"] == "call_tool"
    assert calls[1]["payload"]["x"] == 2


def test_extract_tool_calls_rejects_concatenated_json_objects():
    text = (
        '{"action": "call_tool", "tool": "a", "payload": {}}\n'
        '{"action": "call_tool", "tool": "b", "payload": {}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert [call["tool"] for call in calls] == ["a", "b"]


def test_extract_json_blob_pure_json():
    text = '{"action": "call_tool", "tool": "test", "payload": {}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_code_block():
    text = 'Here is the tool call:\n```json\n{"action": "call_tool", "tool": "test", "payload": {}}\n```'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) == {
        "action": "call_tool",
        "tool": "test",
        "payload": {},
    }


def test_extract_json_blob_embedded():
    text = 'I will call the tool now.\n{"action": "call_tool", "tool": "test", "payload": {}}\nThis should work.'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) is None


def test_extract_json_blob_embedded_with_newlines():
    text = 'Explanation...\n{\n  "action": "call_tool",\n  "tool": "test",\n  "payload": {}\n}\nEnd.'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) is None


def test_extract_json_blob_invalid():
    text = "Just some text."
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result is None


def test_extract_json_blob_malformed_json():
    text = '{"action": "call_tool", ...'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    with pytest.raises(ToolCallParsingError):
        orchestrator._extract_json_blob(text)


def test_extract_json_blob_repairs_truncated_object():
    text = '{"action": "call_tool", "tool": "test", "payload": {}'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_accepts_trailing_characters():
    text = '{"action": "call_tool", "tool": "test", "payload": {}}x'
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_accepts_trailing_prose_after_tool_call():
    text = (
        '{"action": "call_tool", "tool": "test", "payload": {}}\n'
        "I will now execute this tool to gather the facts before answering."
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {}}


def test_extract_json_blob_rejects_multiple_json_objects():
    text = (
        '{"action": "call_tool", "tool": "a", "payload": {}}\n'
        '{"action": "call_tool", "tool": "b", "payload": {}}'
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=None)  # type: ignore[arg-type]
    with pytest.raises(ToolCallParsingError):
        orchestrator._extract_json_blob(text)


def test_extract_json_blob_accepts_missing_action_when_tool_is_known():
    text = '{"tool": "test", "payload": {"a": 1}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    result = orchestrator._extract_json_blob(text)
    assert result == {"action": "call_tool", "tool": "test", "payload": {"a": 1}}


def test_extract_json_blob_ignores_missing_action_when_tool_is_unknown():
    text = '{"tool": "not_a_tool", "payload": {"a": 1}}'
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._extract_json_blob(text) is None


def test_looks_like_missing_tool_call_detects_promise_to_search():
    """Test that 'I'm going to search...' without actual tool call is detected."""
    text = "I'm going to search the web for authoritative information about you."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_detects_promise_to_execute_tools():
    """Test that 'I'll execute the tools' without actual tool call is detected."""
    text = "I'll fix that now and actually execute the tools."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_detects_promise_to_fetch():
    """Test that 'Let me fetch...' without actual tool call is detected."""
    text = "Let me fetch the concept details now."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_ignores_short_explanatory_text():
    """Test that normal explanatory text without tool promises is not detected."""
    text = "I'll help you with that. What would you like to know?"
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert not orchestrator._looks_like_missing_tool_call(text)


def test_looks_like_missing_tool_call_ignores_long_prose():
    """Test that long prose responses (>500 chars) don't trigger false positives."""
    text = "I'm going to explain this carefully. " * 20  # Makes it > 500 chars
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert not orchestrator._looks_like_missing_tool_call(text)


def test_llm_detector_returns_true_on_yes():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )

    llm = _RecorderLLM(["YES"])
    aux_log: list[Mapping[str, Any]] = []
    invoked, decision = orchestrator._llm_detects_missing_tool_call(
        "I'm going to search the web",
        llm,
        fallback_model="fallback-model",
        aux_log=aux_log,
        path="legacy",
    )

    assert invoked is True
    assert decision is True
    assert llm.calls[0]["model"] == "detector-model"
    assert llm.calls[0]["context"] is None
    assert "search the web" in llm.calls[0]["prompt"]
    assert aux_log and aux_log[0]["type"] == "missing_tool_call_classifier"
    assert aux_log[0]["path"] == "legacy"


def test_llm_detector_strips_vontology_model_prefix_before_calling_llm():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="#V#gpt-4o-mini",
    )

    llm = _RecorderLLM(["NO"])
    aux_log: list[Mapping[str, Any]] = []
    invoked, decision = orchestrator._llm_detects_missing_tool_call(
        "I will fetch that now",
        llm,
        fallback_model="fallback-model",
        aux_log=aux_log,
        path="legacy",
    )

    assert invoked is True
    assert decision is False
    assert llm.calls[0]["model"] == "gpt-4o-mini"
    assert aux_log and aux_log[0]["model_raw"] == "#V#gpt-4o-mini"
    assert aux_log[0]["model_resolved"] == "gpt-4o-mini"
    assert aux_log[0]["path"] == "legacy"


def test_llm_detector_appends_response_when_placeholder_missing():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="You are a binary classifier. Output yes or no.",
        model="detector-model",
    )

    llm = _RecorderLLM(["YES"])
    aux_log: list[Mapping[str, Any]] = []
    invoked, decision = orchestrator._llm_detects_missing_tool_call(
        "I'll fetch JVNAUTOSCI-803 now",
        llm,
        fallback_model="fallback-model",
        aux_log=aux_log,
        path="legacy",
    )

    assert invoked is True
    assert decision is True
    assert "fetch JVNAUTOSCI-803" in llm.calls[0]["prompt"]
    assert aux_log and aux_log[0]["prompt_placeholder_response"] is False
    assert aux_log[0]["prompt_injection_mode"] == "append"
    assert aux_log[0]["path"] == "legacy"


def test_llm_detector_uses_fallback_model_when_missing():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="{response}",
        model=None,
    )

    llm = _RecorderLLM(["NO"])
    aux_log: list[Mapping[str, Any]] = []
    invoked, decision = orchestrator._llm_detects_missing_tool_call(
        "Normal explanatory text",
        llm,
        fallback_model="fallback-model",
        aux_log=aux_log,
        path="legacy",
    )

    assert invoked is True
    assert decision is False
    assert llm.calls[0]["model"] == "fallback-model"
    assert aux_log and aux_log[0]["model"] == "fallback-model"
    assert aux_log[0]["path"] == "legacy"


def test_run_retries_when_llm_detector_flags_missing_tool_call():
    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            "I will search the ontology now",  # initial response (no tool call)
            "YES",  # classifier verdict
            '{"action": "call_tool", "tool": "test", "payload": {}}',  # retry emits tool call
            "Final response",  # follow-up after tool invocation
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
    )
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    # Should have attempted a tool after classifier said YES
    assert gateway.calls
    method_name, payload = gateway.calls[0]
    assert method_name == "test"
    assert payload.get("namespace") == "#V#user"
    assert result.tool_invocations
    assert result.response_text == "Final response"
    assert result.aux_llm_calls

    aux_by_type = {}
    for entry in result.aux_llm_calls:
        if isinstance(entry, dict) and isinstance(entry.get("type"), str):
            aux_by_type.setdefault(entry["type"], []).append(entry)

    assert aux_by_type["missing_tool_call_detection"][0]["path"] == "legacy"
    assert aux_by_type["missing_tool_call_classifier"][0]["path"] == "legacy"
    for retry_entry in aux_by_type["missing_tool_call_retry"]:
        assert retry_entry["path"] == "legacy"


def test_run_retries_when_classifier_misses_but_heuristic_triggers():
    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            # Initial response: promises tool-backed actions, includes a strong heuristic trigger.
            (
                "I’ll do one thing only in this turn: create the concept and then verify it.\n\n"
                "Proceeding now.\n\n"
                "Next message will contain the tool output."  # No tool JSON payload
            ),
            "NO",  # classifier verdict (incorrect)
            '{"action": "call_tool", "tool": "test", "payload": {}}',  # retry emits tool call
            "Final response",  # follow-up after tool invocation
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
    )
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert gateway.calls
    method_name, payload = gateway.calls[0]
    assert method_name == "test"
    assert payload.get("namespace") == "#V#user"
    assert result.tool_invocations
    assert result.response_text == "Final response"
    assert result.aux_llm_calls


def test_run_retries_when_response_uses_smart_quotes_promising_tool_use():
    """Regression test: smart quotes should not bypass missing-tool-call detection.

    Some models emit curly apostrophes (e.g., "I’m") which previously bypassed
    the promise-pattern heuristic and prevented a retry.

    When the fallback detector is active, the orchestrator should still recover
    deterministically (without consuming an extra LLM turn for classification).
    """

    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            # Initial response: promises a tool-backed action, but emits no tool JSON.
            "I’m going to search the knowledge base now.",
            '{"action": "call_tool", "tool": "test", "payload": {}}',
            "Final response",
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert gateway.calls
    assert result.tool_invocations
    assert result.response_text == "Final response"
    aux_types = [entry.get("type") for entry in result.aux_llm_calls]
    assert "missing_tool_call_detection" in aux_types
    assert "missing_tool_call_retry" in aux_types


def test_run_recovers_from_invalid_tool_call_json_with_retry():
    """Regression test: invalid JSON tool-call output should trigger a retry.

    Previously, ToolCallParsingError would bubble out of orchestrator.run(),
    short-circuiting the missing-tool-call recovery path.
    """

    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            # Initial response: clearly a tool call but malformed JSON.
            '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            # Retry response: valid tool call.
            '{"action":"call_tool","tool":"test","payload":{}}',
            # Follow-up after tool invocation.
            "Final response",
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert gateway.calls
    assert result.tool_invocations
    assert result.response_text == "Final response"

    # Ensure we recorded the detection path and the retry attempt.
    aux_types = [entry.get("type") for entry in result.aux_llm_calls]
    assert "missing_tool_call_detection" in aux_types
    assert "missing_tool_call_retry" in aux_types


def test_run_recovers_from_late_turn_invalid_tool_call_json_with_retry():
    """Regression test (JVNAUTOSCI-842): late-turn parse errors should not crash.

    Scenario: after executing a tool, the follow-up response attempts another tool
    call but emits malformed JSON. The orchestrator should retry once and continue.
    """

    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            # Initial response: tool call.
            '{"action":"call_tool","tool":"test","payload":{}}',
            # Follow-up after tool invocation: malformed JSON tool call.
            '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            # Retry response: valid tool call.
            '{"action":"call_tool","tool":"test","payload":{}}',
            # Follow-up after second tool invocation.
            "Final response",
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=2,
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert len(gateway.calls) == 2
    assert result.response_text == "Final response"
    assert all(
        inv.get("tool") != "__tool_call_parse_error__"
        for inv in result.tool_invocations
    )

    aux_types = [entry.get("type") for entry in result.aux_llm_calls]
    assert "missing_tool_call_detection" in aux_types
    assert "missing_tool_call_retry" in aux_types


def test_run_surfaces_late_turn_parse_error_when_retry_also_invalid():
    """Regression test (JVNAUTOSCI-842): late-turn parse errors should surface cleanly.

    If both the original follow-up and the retry response are malformed, the
    orchestrator should return an actionable serialisation error and include a
    parse-error tool invocation (while preserving earlier tool invocations).
    """

    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            # Initial response: tool call.
            '{"action":"call_tool","tool":"test","payload":{}}',
            # Follow-up after tool invocation: malformed JSON tool call.
            '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            # Retry response: still malformed.
            '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=2,
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert len(gateway.calls) == 1
    assert (
        "Tool call was not executed due to an MCP serialisation error"
        in result.response_text
    )
    assert any(
        inv.get("tool") == "__tool_call_parse_error__"
        for inv in result.tool_invocations
    )
    assert any(inv.get("tool") == "test" for inv in result.tool_invocations)


def test_run_applies_missing_tool_retry_budget_across_plan_and_backfill():
    """Retry budget is per-turn, not per-phase (plan/backfill)."""

    gateway = _DummyGateway()
    llm = _RecorderLLM(
        [
            # Plan response: malformed tool call -> consumes retry budget.
            '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            # Plan retry response: valid tool call.
            '{"action":"call_tool","tool":"test","payload":{}}',
            # Backfill response: malformed chained tool call.
            '{"action":"call_tool","tool":"test","payload":{"y":"oops}',
            # Should not be consumed when budget enforcement works.
            '{"action":"call_tool","tool":"test","payload":{}}',
        ]
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=2,
    )
    orchestrator.configure_execution_caps(max_missing_tool_call_retries_per_turn=1)

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert len(gateway.calls) == 1
    assert (
        "Tool call was not executed due to an MCP serialisation error"
        in result.response_text
    )

    retry_responses = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "missing_tool_call_retry"
        and entry.get("stage") == "response"
    ]
    assert len(retry_responses) == 1

    retry_skips = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
        and entry.get("type") == "missing_tool_call_retry"
        and entry.get("stage") == "skipped"
        and entry.get("mechanism") == "budget"
    ]
    assert retry_skips

    # The fourth scripted response remains unused when no second retry occurs.
    assert len(llm.calls) == 3
