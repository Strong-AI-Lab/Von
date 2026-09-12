from __future__ import annotations

from contextvars import ContextVar
from threading import Event
from time import perf_counter
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


def _tool_response(index: int, name: str = "lookup") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"response-{index}",
        model=candidate.MODEL,
        status="completed",
        output_text="",
        output=[
            {
                "type": "function_call",
                "id": f"function-{index}",
                "call_id": f"call-{index}",
                "name": name,
                "arguments": f'{{"query":"value-{index}"}}',
            }
        ],
        usage=None,
        error=None,
        incomplete_details=None,
    )


def _text_response(index: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"response-{index}",
        model=candidate.MODEL,
        status="completed",
        output_text=text,
        output=[],
        usage={"input_tokens": 20, "output_tokens": 8},
        error=None,
        incomplete_details=None,
    )


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


def test_thin_loop_has_no_fixed_tool_batch_or_model_call_limit() -> None:
    responses = _FakeResponses(
        [
            _tool_response(1),
            _tool_response(2),
            _tool_response(3),
            _tool_response(4),
            _text_response(5, "The accumulated read evidence supports an answer."),
        ]
    )

    result = candidate.run_candidate_turn(
        "Investigate the available evidence.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda name, payload: {
            "tool": name,
            "value": payload["query"],
        },
    )

    assert result["outcome"] == "answered"
    assert len(result["tool_batches"]) == 4
    assert len(responses.calls) == 5
    assert candidate.frozen_configuration()["fixed_tool_batch_limit"] is None
    assert candidate.frozen_configuration()["fixed_model_call_limit"] is None


def test_read_failure_is_evidence_for_an_alternate_route() -> None:
    responses = _FakeResponses(
        [
            _tool_response(1, "primary_lookup"),
            _tool_response(2, "alternate_lookup"),
            _text_response(3, "The alternate read supplied useful evidence."),
        ]
    )

    def invoke(
        name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "primary_lookup":
            raise TimeoutError("primary read timed out")
        return {"tool": name, "value": payload["query"]}

    result = candidate.run_candidate_turn(
        "Find evidence by a sensible available route.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=invoke,
    )

    assert result["outcome"] == "answered"
    assert [batch["results"][0]["status"] for batch in result["tool_batches"]] == [
        "error",
        "ok",
    ]
    assert len(responses.calls) == 3


def test_same_batch_reads_receive_equal_opportunity_before_synthesis() -> None:
    now = [0.0]
    invoked: list[str] = []
    actor_context: ContextVar[str | None] = ContextVar(
        "a2_test_actor_context",
        default=None,
    )
    actor_context.set("known-actor")
    observed_actors: list[str | None] = []
    responses = _FakeResponses(
        [
            SimpleNamespace(
                id="response-research",
                model=candidate.MODEL,
                status="completed",
                output_text="",
                output=[
                    {
                        "type": "function_call",
                        "id": "function-1",
                        "call_id": "call-1",
                        "name": "lookup",
                        "arguments": '{"query":"first"}',
                    },
                    {
                        "type": "function_call",
                        "id": "function-2",
                        "call_id": "call-2",
                        "name": "lookup",
                        "arguments": '{"query":"second"}',
                    },
                ],
                usage=None,
                error=None,
                incomplete_details=None,
            ),
            _text_response(
                2,
                "Both parallel reads supplied evidence before synthesis.",
            ),
        ]
    )

    def invoke(
        _name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        invoked.append(payload["query"])
        observed_actors.append(actor_context.get())
        if len(invoked) == 2:
            now[0] = 8.0
        return {"value": payload["query"]}

    result = candidate.run_candidate_turn(
        "Use the evidence available within the elapsed-time envelope.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[{"type": "function", "name": "lookup"}],
        invoke_tool=invoke,
        turn_budget_seconds=10.0,
        final_synthesis_reserve_seconds=3.0,
        clock=lambda: now[0],
    )

    assert result["outcome"] == "answered"
    assert result["final_synthesis_reason"] == "research_time_exhausted"
    assert [item["status"] for item in result["tool_batches"][0]["results"]] == [
        "ok",
        "ok",
    ]
    assert set(invoked) == {"first", "second"}
    assert observed_actors == ["known-actor", "known-actor"]
    assert responses.calls[0]["timeout"] == 7.0
    assert responses.calls[1]["tools"] == [{"type": "function", "name": "lookup"}]
    assert responses.calls[1]["tool_choice"] == "none"
    assert responses.calls[1]["timeout"] == 2.0
    final_input = responses.calls[1]["input"]
    assert {item["call_id"] for item in final_input[-2:]} == {"call-1", "call-2"}


def test_submitted_turn_budget_includes_setup_time() -> None:
    now = [8.0]
    responses = _FakeResponses(
        [_text_response(1, "I used the time remaining after setup.")]
    )

    result = candidate.run_candidate_turn(
        "Answer within the submitted-turn envelope.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda _name, _payload: None,
        turn_budget_seconds=10.0,
        final_synthesis_reserve_seconds=3.0,
        clock=lambda: now[0],
        submitted_started_at=0.0,
    )

    assert result["outcome"] == "answered"
    assert result["final_synthesis_reason"] == "research_time_exhausted"
    assert responses.calls[0]["tools"] == []
    assert responses.calls[0]["tool_choice"] == "none"
    assert responses.calls[0]["timeout"] == 2.0
    assert result["turn_elapsed_ms"] == 8000.0


def test_slow_read_cannot_consume_final_synthesis_window() -> None:
    release_read = Event()
    read_finished = Event()
    responses = _FakeResponses(
        [
            _tool_response(1),
            _text_response(
                2, "The read was late, so this is an honest partial answer."
            ),
        ]
    )

    def invoke(_name: str, _payload: dict[str, Any]) -> dict[str, Any]:
        try:
            release_read.wait(timeout=1.0)
            return {"success": True, "value": "late evidence"}
        finally:
            read_finished.set()

    started = perf_counter()
    result = candidate.run_candidate_turn(
        "Preserve time to answer.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[{"type": "function", "name": "lookup"}],
        invoke_tool=invoke,
        turn_budget_seconds=0.3,
        final_synthesis_reserve_seconds=0.2,
    )
    elapsed = perf_counter() - started

    assert result["outcome"] == "answered"
    assert result["final_synthesis_reason"] == "research_time_exhausted"
    assert result["tool_batches"][0]["results"][0]["status"] == "deadline_exceeded"
    assert responses.calls[1]["tool_choice"] == "none"
    assert elapsed < 0.25

    release_read.set()
    assert read_finished.wait(timeout=0.5)


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
    responses = _FakeResponses(
        [response, _text_response(2, "I can still give a bounded partial answer.")]
    )
    invoked: list[str] = []

    result = candidate.run_candidate_turn(
        "Look something up.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda name, _payload: invoked.append(name),
    )

    assert result["outcome"] == "answered"
    assert invoked == []
    assert len(responses.calls) == 2
    assert responses.calls[1]["tools"] == []
    assert "tool_choice" not in responses.calls[1]


def test_provider_call_id_cannot_be_reused_across_rounds() -> None:
    first = _tool_response(1)
    reused = _tool_response(2)
    reused.output[0]["call_id"] = first.output[0]["call_id"]
    responses = _FakeResponses(
        [
            first,
            reused,
            _text_response(3, "The valid first read supports a partial answer."),
        ]
    )
    invoked: list[str] = []

    result = candidate.run_candidate_turn(
        "Use valid read evidence.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda name, _payload: invoked.append(name) or {"success": True},
    )

    assert result["outcome"] == "answered"
    assert invoked == ["lookup"]
    assert result["provider_protocol_failures"][0]["error"]["reused_call_ids"] == [
        "call-1"
    ]
    assert responses.calls[2]["tools"] == []
    assert "tool_choice" not in responses.calls[2]


def test_late_completed_answer_is_retained_and_marked() -> None:
    now = [0.0]

    class _LateResponses(_FakeResponses):
        def create(self, **kwargs: Any) -> Any:
            response = super().create(**kwargs)
            now[0] = 11.0
            return response

    responses = _LateResponses([_text_response(1, "A useful answer arrived late.")])

    result = candidate.run_candidate_turn(
        "Return useful work without hiding an overrun.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda _name, _payload: None,
        turn_budget_seconds=10.0,
        final_synthesis_reserve_seconds=3.0,
        clock=lambda: now[0],
    )

    assert result["outcome"] == "answered_after_deadline"
    assert result["final_text"] == "A useful answer arrived late."
    assert result["responses"][0]["phase_deadline_overrun_ms"] == 4000.0
    assert result["turn_budget_overrun_ms"] == 1000.0


def test_transient_research_model_failure_does_not_force_synthesis() -> None:
    class _TransientFailureResponses(_FakeResponses):
        def create(self, **kwargs: Any) -> Any:
            if not self.calls:
                self.calls.append(kwargs)
                raise TimeoutError("transient research failure")
            return super().create(**kwargs)

    responses = _TransientFailureResponses(
        [_text_response(2, "Research recovered without a count-based retry policy.")]
    )
    result = candidate.run_candidate_turn(
        "Recover while useful research time remains.",
        client=SimpleNamespace(responses=responses),
        provider_tools=[],
        invoke_tool=lambda _name, _payload: None,
    )

    assert result["outcome"] == "answered"
    assert result["final_text"].startswith("Research recovered")
    assert len(result["model_failures"]) == 1
    assert "final_synthesis_reason" not in result
    assert "tool_choice" not in responses.calls[1]
