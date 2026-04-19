import pytest
from typing import Any, Mapping

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    ToolCallParsingError,
    _MissingToolCallDetectorSpec,
)
from orchestrator_test_harness import build_db_independent_orchestrator


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


class _DisabledGateway(_DummyGateway):
    enabled = False

    def describe_methods(self):
        return {}


class _ExplicitPromptToolGateway(_DummyGateway):
    def describe_methods(self):
        return {
            "workflow_list_definitions": {"description": "list workflow definitions"},
            "workflow_list_instances": {"description": "list workflow instances"},
        }


class _ChecklistPromptToolGateway(_DummyGateway):
    def describe_methods(self):
        return {
            "workflow_list_definitions": {"description": "list workflow definitions"},
            "workflow_list_instances": {"description": "list workflow instances"},
            "create_concepts": {"description": "create concepts"},
            "fetch_concept": {"description": "fetch concept"},
        }


class _FileCopyPromptToolGateway(_DummyGateway):
    def describe_methods(self):
        return {
            "read_file_copy": {"description": "read a file copy concept"},
            "fetch_concept": {"description": "fetch concept"},
        }


class _ScholarlyPromptToolGateway(_DummyGateway):
    def describe_methods(self):
        return {
            "interpret_file_copy": {"description": "materialise scholarly representation"},
            "read_file_copy": {"description": "read a file copy concept"},
        }


class _RelationshipGateway(_DummyGateway):
    def describe_methods(self):
        return {"add_relationship": {"description": "relationship write"}}


class _RecorderLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, prompt, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if not self.responses:
            raise RuntimeError("No responses left in _RecorderLLM")
        return self.responses.pop(0)


def _make_tool_pipeline_orchestrator(monkeypatch, gateway, **kwargs):
    """Disable workflow selection so tests exercise tool-pipeline mechanics directly."""

    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=gateway,
        selector_enabled=False,
        max_tool_invocations=int(kwargs.pop("max_tool_invocations", 1)),
        tool_batch_cap=int(kwargs.pop("tool_batch_cap", 3)),
    )
    return orchestrator


def _make_workflow_action_request(
    orchestrator: InternalMCPChatOrchestrator,
    *,
    llm_client,
    data: Mapping[str, Any],
    action_id: str,
    model: str = "primary-model",
    user_namespace: str = "#V#user",
):
    class _Env:
        def __init__(self):
            self.llm_client = llm_client
            self.model = model
            self.user_namespace = user_namespace
            self.auxiliary_system_prompt = None
            self.max_tool_invocations = orchestrator._max_tool_invocations
            self.max_tool_result_chars = orchestrator._max_tool_result_chars
            self.max_tool_result_field_chars = orchestrator._max_tool_result_field_chars
            self.default_gmail_profile = None

    class _Request:
        def __init__(self):
            self.action_id = action_id
            self.data = dict(data)
            self.environment = _Env()
            self.trace = None

    return _Request()


def test_apply_vontology_template_search_concepts_empty_query_uses_filter_context():
    payload = {
        "results": [{"concept_id": "#V#one"}, {"concept_id": "#V#two"}],
        "query_info": {
            "query": "",
            "instance_of": "#V#project",
            "filter_kind": ["individual"],
        },
    }

    summary = InternalMCPChatOrchestrator._apply_vontology_template(
        'Found {count} concepts for "{query}"',
        payload,
    )

    assert summary is not None
    assert summary.startswith('Found 2 concepts for "')
    assert "instances of project" in summary
    assert "kind=individual" in summary
    assert 'for ""' not in summary


def test_apply_vontology_template_search_concepts_empty_query_omits_empty_query_suffix():
    payload = {
        "results": [{"concept_id": "#V#one"}],
        "query_info": {"query": ""},
    }

    summary = InternalMCPChatOrchestrator._apply_vontology_template(
        'Found {count} concepts for "{query}"',
        payload,
    )

    assert summary == "Found 1 concepts"
    assert 'for ""' not in summary


def test_apply_vontology_template_create_concepts_includes_created_concept_id():
    payload = {
        "successful": 1,
        "results": [
            {
                "success": True,
                "requested_name": "Planck Mission",
                "canonical_concept_id": "#V#planck_mission",
            }
        ],
    }

    summary = InternalMCPChatOrchestrator._apply_vontology_template(
        "Created {count} concept(s): {names}",
        payload,
        max_length=200,
    )

    assert summary is not None
    assert "Planck Mission (#V#planck_mission)" in summary


def test_apply_vontology_template_create_concepts_uses_nested_concept_id_when_needed():
    payload = {
        "successful": 1,
        "results": [
            {
                "success": True,
                "input_name": "Otter Session",
                "concept": {"concept_id": "#V#otter_session_2"},
            }
        ],
    }

    summary = InternalMCPChatOrchestrator._apply_vontology_template(
        "Created {count} concept(s): {names}",
        payload,
        max_length=200,
    )

    assert summary is not None
    assert "Otter Session (#V#otter_session_2)" in summary


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


def test_assess_missing_tool_call_does_not_backstop_classifier_no_with_python_heuristic():
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
    assert interpretation.heuristic_missing_tool_call is False

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

    assert assessment.retry_reason is None


def test_assess_missing_tool_call_skips_semantic_retry_when_not_required():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["YES"])  # Would be consumed if classifier were called.
    aux_log: list[Mapping[str, Any]] = []

    response_text = "I'll now execute the tools."
    interpretation = orchestrator._interpret_model_turn(response_text)
    assert interpretation.heuristic_missing_tool_call is False

    assessment = orchestrator._assess_missing_tool_call(
        response_text=interpretation.response_text,
        use_structured=False,
        interpretation=interpretation,
        llm_client=llm,
        model="primary-model",
        classifier_model="classifier-model",
        aux_log=aux_log,
        tool_call_parse_error=None,
        allow_semantic_retry=False,
    )

    assert assessment.retry_reason is None
    assert not llm.calls


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


def test_extract_tool_calls_accepts_tool_uses_recipient_envelope():
    text = (
        '{"tool_uses": ['
        '{"recipient_name": "functions.test", "parameters": {"x": 1}},'
        '{"recipient_name": "test", "parameters": {"x": 2}}'
        "]}"
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert [call["tool"] for call in calls] == ["test", "test"]
    assert [call["payload"]["x"] for call in calls] == [1, 2]


def test_extract_tool_calls_recovers_tool_uses_from_corrupted_wrapper_and_cleans_payload():
    text = (
        '{"commentary to=multi_tool_use.parallel malformed wrapper"}'
        '{"tool_uses":[{"recipient_name":"functions.add_relationship","parameters":'
        '{"source_id":"\\nsalient_predicate_governance_workflow\\nIndividual\\n",'
        '"predicate":"\\nhas_step\\nPredicate\\n",'
        '"target":"\\nsalience_step_identify_type\\nIndividual\\n"}}]}'
        "json to=multi_tool_use.parallel"
    )
    orchestrator = InternalMCPChatOrchestrator(gateway=_RelationshipGateway())  # type: ignore[arg-type]
    calls = orchestrator._extract_tool_calls(text)
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["tool"] == "add_relationship"
    assert calls[0]["payload"]["source_id"] == "#V#salient_predicate_governance_workflow"
    assert calls[0]["payload"]["predicate"] == "#V#has_step"
    assert calls[0]["payload"]["target"] == "#V#salience_step_identify_type"


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


def test_looks_like_missing_tool_call_is_disabled_for_tool_promise_prose():
    """Tool-promise prose should not trigger Python heuristic recovery anymore."""
    text = "I'm going to search the web for authoritative information about you."
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    assert not orchestrator._looks_like_missing_tool_call(text)


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


def test_missing_tool_call_assess_requests_retry_when_classifier_flags_missing_tool_call():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["YES"])
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )
    response_text = "I will search the ontology now"
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=llm,
        action_id="missing_tool_call.assess",
        data={
            "response_text": response_text,
            "interpretation": orchestrator._interpret_model_turn(response_text),
            "use_structured": False,
            "missing_tool_assessor": orchestrator._assess_missing_tool_call,
            "classifier_model": "detector-model",
            "aux_llm_calls": [],
            "tool_call_parse_error": None,
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 1,
        },
    )
    result = orchestrator._action_missing_tool_call_assess(request)

    assert result.outputs["result"] is True
    assert result.outputs["missing_tool_call_retry_reason"]
    aux_types = [entry.get("type") for entry in result.outputs["aux_llm_calls"]]
    assert "missing_tool_call_detection" in aux_types
    assert "missing_tool_call_classifier" in aux_types


def test_missing_tool_call_assess_does_not_use_python_heuristic_when_classifier_misses():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["NO"])
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )
    response_text = (
        "I’ll do one thing only in this turn: create the concept and then verify it.\n\n"
        "Proceeding now.\n\n"
        "Next message will contain the tool output."
    )
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=llm,
        action_id="missing_tool_call.assess",
        data={
            "response_text": response_text,
            "interpretation": orchestrator._interpret_model_turn(response_text),
            "use_structured": False,
            "missing_tool_assessor": orchestrator._assess_missing_tool_call,
            "classifier_model": "detector-model",
            "aux_llm_calls": [],
            "tool_call_parse_error": None,
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 1,
        },
    )
    result = orchestrator._action_missing_tool_call_assess(request)

    assert result.outputs["result"] is False
    assert result.outputs["missing_tool_call_retry_reason"] is None
    detection_entry = next(
        entry
        for entry in result.outputs["aux_llm_calls"]
        if entry.get("type") == "missing_tool_call_detection"
    )
    assert detection_entry["classifier_verdict"] == "no"
    assert not any(
        entry.get("type") == "missing_tool_call_heuristic"
        for entry in result.outputs["aux_llm_calls"]
    )


def test_missing_tool_retry_forces_explicitly_requested_workflow_tools():
    prompt = (
        "1) Call workflow_list_definitions.\n"
        "2) Call workflow_list_instances for #V#salient_predicate_governance_workflow."
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_ExplicitPromptToolGateway()  # type: ignore[arg-type]
    )
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=_RecorderLLM(["Proceeding now."]),
        action_id="missing_tool_call.retry",
        data={
            "response_text": "Proceeding now.",
            "user_prompt": prompt,
            "augmented_context": [],
            "missing_tool_call_assessment": {"path": "legacy"},
            "missing_tool_call_retry_reason": (
                "prompt requested tool(s) not yet invoked: "
                "workflow_list_definitions, workflow_list_instances"
            ),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "missing_prompt_tools": [
                "workflow_list_definitions",
                "workflow_list_instances",
            ],
            "missing_prompt_fetch_concept_ids": [],
            "missing_prompt_read_file_copy_ids": [],
            "missing_prompt_scholarly_representation_for_file_copy_ids": [],
            "required_prompt_create_type_name": None,
            "required_url_extraction_url": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
            "aux_llm_calls": [],
        },
    )

    result = orchestrator._action_missing_tool_call_retry(request)
    tool_calls = result.outputs["tool_calls"]

    assert [call["tool"] for call in tool_calls] == [
        "workflow_list_definitions",
        "workflow_list_instances",
    ]
    assert tool_calls[1]["payload"]["workflow_id"] == "#V#salient_predicate_governance_workflow"
    assert result.outputs["missing_tool_call_retry_success"] is True


def test_derive_prompt_tool_requirements_keeps_only_explicit_tools_from_checklist():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_ChecklistPromptToolGateway()  # type: ignore[arg-type]
    )
    prompt = (
        "1) Call workflow_list_definitions.\n"
        "2) Create a fresh test type under #V#event (e.g. #V#test_workflow_trigger_type_for_identify_step_fix_1).\n"
        "3) Call workflow_list_instances for #V#salient_predicate_governance_workflow.\n"
        "4) Verify migration by checking these concepts:\n"
        "- #V#workflow_mapping_target_type_id_to_concept_id_param\n"
        "- #V#workflow_mapping_tool_field_concept_id_to_validated_type_id\n"
        "- #V#workflow_mapping_tool_field_name_to_validated_type_name\n"
    )

    requirements = orchestrator._derive_prompt_tool_requirements(
        prompt,
        method_catalogue=orchestrator._gateway.describe_methods(),
    )

    required_tools = requirements.get("required_tools") or []
    assert "workflow_list_definitions" in required_tools
    assert "workflow_list_instances" in required_tools
    assert "create_concepts" not in required_tools
    assert "fetch_concept" not in required_tools
    assert requirements.get("required_create_type_name") is None
    assert requirements.get("required_fetch_concept_ids") == []


def test_derive_prompt_tool_requirements_does_not_infer_url_extraction_from_prompt_text():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DummyGateway()  # type: ignore[arg-type]
    )
    requirements = orchestrator._derive_prompt_tool_requirements(
        "Please read this: https://example.com/report",
        method_catalogue={
            "resilient_extract_url": {"description": "resilient URL extraction"},
            "extract_url": {"description": "URL extraction"},
        },
    )

    required_tools = requirements.get("required_tools") or []
    assert "resilient_extract_url" not in required_tools
    assert "extract_url" not in required_tools
    assert requirements.get("required_url_extraction_tool") is None
    assert requirements.get("required_url_extraction_url") is None


def test_derive_prompt_tool_requirements_ignores_incidental_urls_without_read_intent():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DummyGateway()  # type: ignore[arg-type]
    )
    requirements = orchestrator._derive_prompt_tool_requirements(
        "Meeting notice: Zoom link https://example.com/join/abc for tomorrow's call.",
        method_catalogue={
            "resilient_extract_url": {"description": "resilient URL extraction"},
            "extract_url": {"description": "URL extraction"},
        },
    )

    required_tools = requirements.get("required_tools") or []
    assert "resilient_extract_url" not in required_tools
    assert "extract_url" not in required_tools
    assert requirements.get("required_url_extraction_tool") is None
    assert requirements.get("required_url_extraction_url") is None


def test_infer_required_prompt_tool_retry_tool_calls_forces_url_extraction():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DummyGateway()  # type: ignore[arg-type]
    )

    forced_calls = orchestrator._infer_required_prompt_tool_retry_tool_calls(
        user_text="Please read this: https://example.com/report",
        missing_required_tools=["resilient_extract_url"],
        required_url_extraction_url="https://example.com/report",
    )

    assert forced_calls == [
        {
            "action": "call_tool",
            "tool": "resilient_extract_url",
            "payload": {"url": "https://example.com/report"},
        }
    ]


def test_derive_missing_prompt_requirements_tracks_missing_fetch_targets():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_ChecklistPromptToolGateway()  # type: ignore[arg-type]
    )
    required_fetch_ids = [
        "#V#workflow_mapping_target_type_id_to_concept_id_param",
        "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id",
    ]

    (
        missing_tools,
        missing_fetch_ids,
        missing_read_file_copy_ids,
        missing_scholarly_ids,
    ) = orchestrator._derive_missing_prompt_requirements(
        required_tools=["fetch_concept"],
        required_fetch_concept_ids=required_fetch_ids,
        tool_invocations=[
            {
                "tool": "fetch_concept",
                "payload": {
                    "concept_id": "#V#workflow_mapping_target_type_id_to_concept_id_param"
                },
            }
        ],
    )

    assert missing_tools == ["fetch_concept"]
    assert missing_fetch_ids == [
        "#V#workflow_mapping_tool_field_concept_id_to_validated_type_id"
    ]
    assert missing_read_file_copy_ids == []
    assert missing_scholarly_ids == []
    reason = orchestrator._build_missing_prompt_retry_reason(
        missing_tools=missing_tools,
        missing_fetch_concept_ids=missing_fetch_ids,
    )
    assert reason == (
        "prompt requested tool(s) not yet invoked: "
        "fetch_concept; "
        "fetch_concept targets: #V#workflow_mapping_tool_field_concept_id_to_validated_type_id"
    )


def test_derive_prompt_tool_requirements_detects_concept_verification_intent():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_ChecklistPromptToolGateway()  # type: ignore[arg-type]
    )

    requirements = orchestrator._derive_prompt_tool_requirements(
        "Can you verify whether #V#workflow_mapping_target_type_id_to_concept_id_param exists?",
        method_catalogue=orchestrator._gateway.describe_methods(),
    )

    assert "fetch_concept" not in (requirements.get("required_tools") or [])
    assert requirements.get("required_fetch_concept_ids") == []


def test_derive_prompt_tool_requirements_does_not_infer_paper_representation_from_context():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_ScholarlyPromptToolGateway()  # type: ignore[arg-type]
    )

    requirements = orchestrator._derive_prompt_tool_requirements(
        "Fully represent the corresponding paper.",
        method_catalogue=orchestrator._gateway.describe_methods(),
        context_messages=[
            {
                "role": "user",
                "content": (
                    "Attached file concept: "
                    "#V#uploaded_file_copy_76c1c13fed0140f496133d008b4cfad7"
                ),
            }
        ],
    )

    required_tools = requirements.get("required_tools") or []
    assert "interpret_file_copy" not in required_tools
    assert requirements.get("required_scholarly_representation_for_file_copy_ids") == []


def test_run_does_not_force_read_file_copy_from_prompt_semantics(monkeypatch):
    gateway = _FileCopyPromptToolGateway()
    llm = _RecorderLLM(
        [
            "I should inspect the uploaded file first.",
            "File copy inspected.",
        ]
    )
    orchestrator = _make_tool_pipeline_orchestrator(
        monkeypatch,
        gateway,
        max_tool_invocations=2,
    )

    result = orchestrator.run(
        prompt=(
            "Please review uploaded file #V#computer_file_copy_case_1 and summarise key points."
        ),
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    called_tools = [tool for tool, _ in gateway.calls]
    assert called_tools.count("read_file_copy") == 0
    assert all(tool != "read_file_copy" for tool, _ in result.tool_invocations)


def test_run_does_not_force_interpret_file_copy_from_paper_representation_language(
    monkeypatch,
):
    gateway = _ScholarlyPromptToolGateway()
    llm = _RecorderLLM(
        [
            "I will represent the corresponding paper now.",
            "Paper representation complete.",
        ]
    )
    orchestrator = _make_tool_pipeline_orchestrator(
        monkeypatch,
        gateway,
        max_tool_invocations=2,
    )

    result = orchestrator.run(
        prompt="Fully represent the corresponding paper.",
        context=[
            {
                "role": "user",
                "content": (
                    "Attached file concept: "
                    "#V#uploaded_file_copy_76c1c13fed0140f496133d008b4cfad7"
                ),
            }
        ],
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    called_tools = [tool for tool, _ in gateway.calls]
    assert called_tools.count("interpret_file_copy") == 0
    assert all(tool != "interpret_file_copy" for tool, _ in result.tool_invocations)


def test_missing_tool_retry_forces_only_explicit_checklist_tools():
    prompt = (
        "1) Call workflow_list_definitions.\n"
        "2) Create a fresh test type under #V#event (e.g. #V#test_workflow_trigger_type_for_identify_step_fix_1).\n"
        "3) Call workflow_list_instances for #V#salient_predicate_governance_workflow.\n"
        "4) Verify migration by checking these concepts:\n"
        "- #V#workflow_mapping_target_type_id_to_concept_id_param\n"
        "- #V#workflow_mapping_tool_field_concept_id_to_validated_type_id\n"
        "- #V#workflow_mapping_tool_field_name_to_validated_type_name\n"
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_ChecklistPromptToolGateway()  # type: ignore[arg-type]
    )
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=_RecorderLLM(["I will do that now."]),
        action_id="missing_tool_call.retry",
        data={
            "response_text": "I will do that now.",
            "user_prompt": prompt,
            "augmented_context": [],
            "missing_tool_call_assessment": {"path": "legacy"},
            "missing_tool_call_retry_reason": (
                "prompt requested tool(s) not yet invoked: "
                "workflow_list_definitions, workflow_list_instances"
            ),
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 2,
            "missing_prompt_tools": [
                "workflow_list_definitions",
                "workflow_list_instances",
            ],
            "missing_prompt_fetch_concept_ids": [],
            "missing_prompt_read_file_copy_ids": [],
            "missing_prompt_scholarly_representation_for_file_copy_ids": [],
            "required_prompt_create_type_name": None,
            "required_url_extraction_url": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
            "aux_llm_calls": [],
        },
    )
    result = orchestrator._action_missing_tool_call_retry(request)
    tool_calls = result.outputs["tool_calls"]

    assert [call["tool"] for call in tool_calls] == [
        "workflow_list_definitions",
        "workflow_list_instances",
    ]


def test_missing_tool_call_assess_handles_smart_quotes_in_tool_promises():
    """Regression test: smart quotes should not bypass missing-tool-call detection.

    Some models emit curly apostrophes (e.g., "I’m") which previously bypassed
    the promise-pattern heuristic and prevented a retry.

    When the fallback detector is active, the orchestrator should still recover
    deterministically (without consuming an extra LLM turn for classification).
    """

    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    llm = _RecorderLLM(["YES"])
    orchestrator._missing_tool_call_detector_loaded = True
    orchestrator._missing_tool_call_detector = _MissingToolCallDetectorSpec(
        action_id="#V#detect_missing_tool_call_action",
        prompt_id="#V#missing_tool_call_detection_prompt",
        prompt_text="Answer YES or NO for: {response}",
        model="detector-model",
    )
    response_text = "I’m going to search the knowledge base now."
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=llm,
        action_id="missing_tool_call.assess",
        data={
            "response_text": response_text,
            "interpretation": orchestrator._interpret_model_turn(response_text),
            "use_structured": False,
            "missing_tool_assessor": orchestrator._assess_missing_tool_call,
            "classifier_model": "detector-model",
            "aux_llm_calls": [],
            "tool_call_parse_error": None,
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 1,
        },
    )
    result = orchestrator._action_missing_tool_call_assess(request)

    assert result.outputs["result"] is True
    assert result.outputs["missing_tool_call_retry_reason"]


def test_missing_tool_retry_recovers_from_invalid_tool_call_json():
    """Regression test: invalid JSON tool-call output should trigger a retry.

    Previously, ToolCallParsingError would bubble out of orchestrator.run(),
    short-circuiting the missing-tool-call recovery path.
    """

    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=_RecorderLLM(['{"action":"call_tool","tool":"test","payload":{}}']),
        action_id="missing_tool_call.retry",
        data={
            "response_text": '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            "user_prompt": "hello",
            "augmented_context": [],
            "missing_tool_call_assessment": {"path": "legacy"},
            "missing_tool_call_retry_reason": "tool call parse error",
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 1,
            "missing_prompt_tools": [],
            "missing_prompt_fetch_concept_ids": [],
            "missing_prompt_read_file_copy_ids": [],
            "missing_prompt_scholarly_representation_for_file_copy_ids": [],
            "required_prompt_create_type_name": None,
            "required_url_extraction_url": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
            "tool_call_parse_error": ToolCallParsingError("bad json"),
            "aux_llm_calls": [],
        },
    )
    result = orchestrator._action_missing_tool_call_retry(request)

    assert result.outputs["missing_tool_call_retry_success"] is True
    assert result.outputs["tool_calls"] == [
        {"action": "call_tool", "tool": "test", "payload": {}}
    ]


def test_missing_tool_retry_recovers_from_late_turn_invalid_tool_call_json():
    """Regression test (JVNAUTOSCI-842): late-turn parse errors should not crash.

    Scenario: after executing a tool, the follow-up response attempts another tool
    call but emits malformed JSON. The orchestrator should retry once and continue.
    """

    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=_RecorderLLM(['{"action":"call_tool","tool":"test","payload":{}}']),
        action_id="missing_tool_call.retry",
        data={
            "response_text": '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            "user_prompt": "hello",
            "augmented_context": [],
            "missing_tool_call_assessment": {"path": "legacy"},
            "missing_tool_call_retry_reason": "tool call parse error",
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 1,
            "missing_prompt_tools": [],
            "missing_prompt_fetch_concept_ids": [],
            "missing_prompt_read_file_copy_ids": [],
            "missing_prompt_scholarly_representation_for_file_copy_ids": [],
            "required_prompt_create_type_name": None,
            "required_url_extraction_url": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
            "tool_call_parse_error": ToolCallParsingError("bad json"),
            "aux_llm_calls": [],
        },
    )
    result = orchestrator._action_missing_tool_call_retry(request)

    assert result.outputs["missing_tool_call_retry_success"] is True
    assert result.outputs["tool_calls"][0]["tool"] == "test"


# ---- Tests for widened prompt requirement detection (JVNAUTOSCI-1422) ----


class _GitHubMCPGateway(_DummyGateway):
    def describe_methods(self):
        return {
            "github_read_file": {"description": "read a file from GitHub"},
            "github_read_tree": {"description": "read repo tree from GitHub"},
            "github_search_code": {"description": "search code on GitHub"},
            "jira_search": {"description": "search Jira"},
        }


def test_extract_explicit_prompt_tool_requirements_matches_use_verb():
    """'use <tool>' should be detected like 'call <tool>'."""
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Please use fetch_concept for #V#foo",
        method_catalogue={"fetch_concept": {"description": "fetch"}},
    )
    assert "fetch_concept" in result


def test_extract_explicit_prompt_tool_requirements_matches_run_verb():
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Run search_concepts for anything matching 'test'",
        method_catalogue={"search_concepts": {"description": "search"}},
    )
    assert "search_concepts" in result


def test_extract_explicit_prompt_tool_requirements_matches_invoke_verb():
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Invoke download_paper on arXiv:2301.12345",
        method_catalogue={"download_paper": {"description": "download"}},
    )
    assert "download_paper" in result


def test_extract_explicit_prompt_tool_requirements_ignores_mcp_family_phrases():
    """Natural-language MCP family phrases are not explicit tool names."""
    catalogue = _GitHubMCPGateway().describe_methods()
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Use the GitHub MCP read tools to fetch docs/engineering/manual.md",
        method_catalogue=catalogue,
    )
    assert result == []


def test_extract_explicit_prompt_tool_requirements_ignores_unqualified_mcp_family_phrases():
    """Unqualified MCP-family wording is still not an explicit tool request."""
    catalogue = _GitHubMCPGateway().describe_methods()
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Use the GitHub MCP tools to explore the repo",
        method_catalogue=catalogue,
    )
    assert result == []


def test_extract_explicit_prompt_tool_requirements_ignores_jira_mcp_family_phrases():
    """Jira MCP family wording is not treated as an explicit tool requirement."""
    catalogue = _GitHubMCPGateway().describe_methods()
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Use the Jira MCP tools to find the task",
        method_catalogue=catalogue,
    )
    assert result == []


def test_extract_explicit_prompt_tool_requirements_deduplicates():
    """Repeated explicit tool mentions should only surface once."""
    catalogue = {"github_read_file": {"description": "read a file from GitHub"}}
    result = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Use github_read_file and then call github_read_file again",
        method_catalogue=catalogue,
    )
    assert result.count("github_read_file") == 1


def test_missing_tool_retry_surfaces_parse_error_when_retry_also_invalid():
    """Regression test (JVNAUTOSCI-842): late-turn parse errors should surface cleanly.

    If both the original follow-up and the retry response are malformed, the
    orchestrator should return an actionable serialisation error and include a
    parse-error tool invocation (while preserving earlier tool invocations).
    """

    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=_RecorderLLM(['{"action":"call_tool","tool":"test","payload":{"x":"oops}']),
        action_id="missing_tool_call.retry",
        data={
            "response_text": '{"action":"call_tool","tool":"test","payload":{"x":"oops}',
            "user_prompt": "hello",
            "augmented_context": [],
            "missing_tool_call_assessment": {"path": "legacy"},
            "missing_tool_call_retry_reason": "tool call parse error",
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 1,
            "missing_prompt_tools": [],
            "missing_prompt_fetch_concept_ids": [],
            "missing_prompt_read_file_copy_ids": [],
            "missing_prompt_scholarly_representation_for_file_copy_ids": [],
            "required_prompt_create_type_name": None,
            "required_url_extraction_url": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
            "tool_call_parse_error": ToolCallParsingError("bad json"),
            "aux_llm_calls": [],
        },
    )
    result = orchestrator._action_missing_tool_call_retry(request)

    assert result.outputs["missing_tool_call_retry_success"] is False
    assert isinstance(result.outputs["tool_call_parse_error"], ToolCallParsingError)
    assert isinstance(result.outputs["response_text"], str)


def test_missing_tool_retry_respects_retry_budget():
    """Retry budget is per-turn, not per-phase (plan/backfill)."""

    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    request = _make_workflow_action_request(
        orchestrator,
        llm_client=_RecorderLLM(['{"action":"call_tool","tool":"test","payload":{}}']),
        action_id="missing_tool_call.retry",
        data={
            "response_text": '{"action":"call_tool","tool":"test","payload":{"y":"oops}',
            "user_prompt": "hello",
            "augmented_context": [],
            "missing_tool_call_assessment": {"path": "legacy"},
            "missing_tool_call_retry_reason": "tool call parse error",
            "missing_tool_call_retry_attempts": 1,
            "missing_tool_call_retry_budget": 1,
            "missing_prompt_tools": [],
            "missing_prompt_fetch_concept_ids": [],
            "missing_prompt_read_file_copy_ids": [],
            "missing_prompt_scholarly_representation_for_file_copy_ids": [],
            "required_prompt_create_type_name": None,
            "required_url_extraction_url": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
            "tool_call_parse_error": ToolCallParsingError("bad json"),
            "aux_llm_calls": [],
        },
    )
    result = orchestrator._action_missing_tool_call_retry(request)

    assert result.outputs["missing_tool_call_retry_success"] is False
    assert result.outputs["missing_tool_call_retry_suppressed"] is True
    assert result.outputs["missing_tool_call_retry_stop_reason"] == "retry_budget_exhausted"


def test_missing_tool_call_retry_stops_on_no_progress_guard_with_safe_response():
    repeated_response = "I will search the knowledge base now."
    llm = _RecorderLLM([repeated_response])

    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DummyGateway(),  # type: ignore[arg-type]
        max_tool_invocations=1,
    )

    class _Env:
        def __init__(self, llm_client):
            self.llm_client = llm_client
            self.model = "primary-model"
            self.user_namespace = "#V#user"

    class _Request:
        def __init__(self, data, environment):
            self.data = data
            self.environment = environment
            self.trace = None

    request = _Request(
        data={
            "response_text": repeated_response,
            "user_prompt": "",
            "augmented_context": [],
            "missing_tool_call_retry_attempts": 0,
            "missing_tool_call_retry_budget": 3,
            "missing_tool_call_assessment": {"path": "legacy"},
            "aux_llm_calls": [],
            "missing_prompt_tools": [],
            "missing_prompt_fetch_concept_ids": [],
            "required_prompt_create_type_name": None,
            "extract_tool_calls_fn": orchestrator._extract_tool_calls,
        },
        environment=_Env(llm),
    )

    result = orchestrator._action_missing_tool_call_retry(request)

    assert (
        result.outputs.get("response_text")
        == "I couldn't execute the requested tool action because repeated "
        "recovery produced no executable tool call."
    )
    assert result.outputs.get("missing_tool_call_retry_suppressed") is True
    assert (
        result.outputs.get("missing_tool_call_retry_stop_reason")
        == "no_state_change_guard_triggered"
    )
    assert (
        result.outputs.get("missing_tool_call_recovery_outcome")
        == "no_state_change_guard_triggered"
    )
    assert len(llm.calls) == 1

    aux_entries = request.data.get("aux_llm_calls")
    assert isinstance(aux_entries, list)
    retry_guard_entries = [
        entry
        for entry in aux_entries
        if isinstance(entry, dict)
        and entry.get("type") == "missing_tool_call_retry"
        and entry.get("mechanism") == "no_progress_guard"
    ]
    assert retry_guard_entries
    assert retry_guard_entries[-1].get("stage") == "tool_recovery"
    assert (
        retry_guard_entries[-1].get("stop_reason")
        == "no_state_change_guard_triggered"
    )


def test_run_gateway_disabled_sanitises_internal_action_marker_response():
    llm = _RecorderLLM(["<action>call_tool</action>"])

    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DisabledGateway(),  # type: ignore[arg-type]
        max_tool_invocations=1,
    )

    result = orchestrator.run(
        prompt="Please search for test",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert len(result.tool_invocations) == 0
    assert (
        result.response_text
        == "I couldn't execute the requested tool action because no executable "
        "tool call was produced."
    )

    sanitised_entries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "action_output_sanitised"
    ]
    assert sanitised_entries
    assert sanitised_entries[-1].get("reason") == "xml_action_marker"
    assert sanitised_entries[-1].get("source_stage") == "run.gateway_disabled"


def test_sanitise_user_visible_action_output_rewrites_action_json_and_logs_reason():
    orchestrator = InternalMCPChatOrchestrator(gateway=_DummyGateway())  # type: ignore[arg-type]
    aux_log: list[Mapping[str, Any]] = []

    safe_text = orchestrator._sanitise_user_visible_action_output(
        '{"action":"call_tool","tool":"test","payload":{}}',
        aux_log=aux_log,
        source_stage="unit_test",
    )

    assert (
        safe_text
        == "I couldn't execute the requested tool action because no executable "
        "tool call was produced."
    )
    assert aux_log
    assert aux_log[0].get("type") == "action_output_sanitised"
    assert aux_log[0].get("reason") == "json_action_payload"
    assert aux_log[0].get("source_stage") == "unit_test"


def test_run_preserves_claim_like_response_without_python_completion_validation():
    llm = _RecorderLLM(
        [
            "I completed the task and merged the branch.",
        ]
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DummyGateway(),  # type: ignore[arg-type]
        max_tool_invocations=1,
    )

    result = orchestrator.run(
        prompt="What happened?",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert result.response_text == "I completed the task and merged the branch."

    aux_types = {
        entry.get("type")
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
    }
    assert "completion_claim_detection" not in aux_types
    assert "completion_claim_validation" not in aux_types


def test_run_keeps_non_claim_responses_free_of_completion_validation_events():
    llm = _RecorderLLM(
        [
            "Here are two options for next steps, and I can apply either approach.",
        ]
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=_DummyGateway(),  # type: ignore[arg-type]
        max_tool_invocations=1,
    )

    result = orchestrator.run(
        prompt="What next?",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert (
        result.response_text
        == "Here are two options for next steps, and I can apply either approach."
    )
    aux_types = {
        entry.get("type")
        for entry in result.aux_llm_calls
        if isinstance(entry, dict)
    }
    assert "completion_claim_detection" not in aux_types
    assert "completion_claim_validation" not in aux_types


def test_run_critic_emits_annotated_response_critic_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_CRITIC_ENABLE", "1")
    llm = _RecorderLLM(["Initial response."])
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=_DummyGateway(),
        selector_enabled=False,
        max_tool_invocations=1,
    )

    original_run_llm_with_fallbacks = orchestrator._run_llm_with_fallbacks

    def _patched_run_llm_with_fallbacks(*args, **kwargs):
        if kwargs.get("stage") == "critic":
            return (
                '{"approve": false, "revised_response": "Critic-approved response."}',
                "critic-model",
                None,
            )
        return original_run_llm_with_fallbacks(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_fallbacks",
        _patched_run_llm_with_fallbacks,
    )

    result = orchestrator.run(
        prompt="Review the response.",
        context=None,
        llm_client=llm,
        model="primary-model",
        user_namespace="#V#user",
    )

    assert result.response_text == "Critic-approved response."
    critic_entries = [
        entry
        for entry in result.aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "critic"
    ]
    assert critic_entries
    assert critic_entries[-1].get("decision_class") == "response_critic"
    assert critic_entries[-1].get("decision_source") == "llm_review_response"
