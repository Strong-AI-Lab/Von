import json
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


class _StubResult:
    def __init__(self, payload, duration_ms=1.0):
        self.payload = payload
        self.duration_ms = duration_ms


class _CapturingGateway:
    enabled = True

    def __init__(self):
        self.invocations = []

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        self.invocations.append({"tool": tool_name, "payload": payload})
        return _StubResult({"ok": True})


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, prompt, *, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if self._responses:
            return self._responses.pop(0)
        return "ok"


def test_orchestrator_executes_all_tool_calls_in_single_list_response():
    """Regression test for JVNAUTOSCI-941.

    Previously, if the model emitted a list of >4 tool calls, the orchestrator
    executed only the first batch (cap=4) and then asked for a final answer.
    The model could respond with a summary that *assumed* all calls ran.

    We now execute the remaining tool calls from the same list before prompting
    for a final answer.
    """

    gateway = cast(Any, _CapturingGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=20,
        max_context_chars=80_000,
    )

    tool_calls = [
        {"action": "call_tool", "tool": "dummy", "payload": {"i": i}} for i in range(9)
    ]

    llm = _CapturingLLM([json.dumps(tool_calls), "done"])

    result = orchestrator.run(
        prompt="link authors", context=[], llm_client=llm, model=None
    )

    assert result.response_text == "done"
    assert len(result.tool_invocations) == 9
    assert [inv.get("payload", {}).get("i") for inv in result.tool_invocations] == list(
        range(9)
    )
    assert len(gateway.invocations) == 9
    # Initial tool call list + single follow-up answer.
    assert len(llm.calls) == 2
