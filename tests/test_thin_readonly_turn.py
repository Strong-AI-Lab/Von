from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scripts import try_thin_readonly_turn as candidate


class _FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


def test_thin_loop_uses_read_result_and_returns_answer() -> None:
    first = SimpleNamespace(
        id="response-1",
        model=candidate.MODEL,
        output_text="",
        output=[
            {
                "type": "reasoning",
                "id": "reasoning-1",
                "encrypted_content": "opaque-state",
                "summary": [],
            },
            {
                "type": "function_call",
                "id": "function-1",
                "call_id": "call-1",
                "name": "lookup",
                "arguments": '{"query":"alpha"}',
            },
        ],
        usage=None,
    )
    final = SimpleNamespace(
        id="response-2",
        model=candidate.MODEL,
        output_text="Alpha is supported by the read evidence.",
        output=[],
        usage={"input_tokens": 20, "output_tokens": 8},
    )
    responses = _FakeResponses([first, final])
    client = SimpleNamespace(responses=responses)

    result = candidate.run_candidate_turn(
        "Find alpha.",
        client=client,
        provider_tools=[
            {
                "type": "function",
                "name": "lookup",
                "description": "Read a value.",
                "parameters": {"type": "object"},
            }
        ],
        invoke_tool=lambda name, payload: {
            "tool": name,
            "query": payload["query"],
            "value": "alpha evidence",
        },
    )

    assert result["outcome"] == "answered"
    assert result["final_text"] == "Alpha is supported by the read evidence."
    assert len(responses.calls) == 2
    assert responses.calls[0]["input"] == [{"role": "user", "content": "Find alpha."}]
    assert responses.calls[0]["store"] is False
    continuation_input = responses.calls[1]["input"]
    assert continuation_input[1] == first.output[0]
    assert continuation_input[2] == first.output[1]
    assert continuation_input[3] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": ('{"tool": "lookup", "query": "alpha", "value": "alpha evidence"}'),
    }
    assert result["tool_batches"][0]["results"][0]["status"] == "ok"


def test_write_definition_is_rejected_before_invocation() -> None:
    definition = SimpleNamespace(
        category="write",
        description="Change something.",
        input_schema=object(),
    )

    class _Gateway:
        called = False

        @staticmethod
        def get_method_definition(_name: str) -> Any:
            return definition

        def invoke(self, _name: str, _payload: dict[str, Any]) -> None:
            self.called = True

    gateway = _Gateway()

    with pytest.raises(candidate.ReadOnlyBoundaryError, match="not currently"):
        candidate.invoke_frozen_read_tool(
            gateway,
            frozenset({"change_thing"}),
            "change_thing",
            {},
        )

    assert gateway.called is False


@pytest.mark.parametrize("call_ids", [("",), ("duplicate", "duplicate")])
def test_invalid_provider_call_ids_stop_before_tools(call_ids: tuple[str, ...]) -> None:
    response = SimpleNamespace(
        id="malformed-response",
        model=candidate.MODEL,
        status="completed",
        output_text="",
        output=[
            {
                "type": "function_call",
                "id": f"item-{index}",
                "call_id": call_id,
                "name": "lookup",
                "arguments": "{}",
            }
            for index, call_id in enumerate(call_ids)
        ],
        usage=None,
        error=None,
        incomplete_details=None,
    )
    responses = _FakeResponses([response])
    invoked: list[str] = []

    result = candidate.run_candidate_turn(
        "Look something up.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda name, _payload: invoked.append(name),
    )

    assert result["outcome"] == "provider_protocol_error"
    assert invoked == []
    assert len(responses.calls) == 1
