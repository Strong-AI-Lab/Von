import json
from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from workflow_test_support import bootstrap_authoritative_conversation_turn_workflows


class _StubResult:
    def __init__(self, payload, duration_ms=1.0):
        self.payload = payload
        self.duration_ms = duration_ms


class _StubGateway:
    enabled = True

    def describe_methods(self):
        return {}

    def invoke(self, tool_name, payload=None):
        # Large payload to exercise truncation.
        return _StubResult({"content": "x" * 50_000, "ok": True})


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate(self, prompt, *, context=None, model=None):
        self.calls.append({"prompt": prompt, "context": context, "model": model})
        if self._responses:
            return self._responses.pop(0)
        return "ok"


def _bootstrap_authoritative_workflows() -> None:
    report = bootstrap_authoritative_conversation_turn_workflows()
    assert not report.get("graph_publication_errors")


def test_orchestrator_retries_when_model_claims_tool_action_but_emits_no_tool_call():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            (
                "Understood. I'll continue enriching the existing concept.\n\n"
                "Here is the actual ontology operation."
            ),
            json.dumps({"action": "call_tool", "tool": "dummy", "payload": {}}),
            "done",
        ]
    )

    result = orchestrator.run(
        prompt="enrich Prof Green", context=[], llm_client=llm, model=None
    )

    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
    assert "Here is the actual ontology operation." not in result.response_text
    assert result.tool_invocations
    assert result.tool_invocations[0]["tool"] == "dummy"
    # First response + retry-for-tool-call + at least one follow-up answer.
    assert len(llm.calls) >= 3


def test_orchestrator_retries_when_tool_call_json_in_fence_after_prose():
    """Regression test for JVNAUTOSCI-800: fenced JSON after prose doesn't execute.

    This matches the exact failure pattern from the user's transcript where the model
    outputs substantial prose followed by a fenced JSON tool call, which the strict
    extraction logic rejects.
    """
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_context_chars=80_000,
    )

    # Simulate the exact pattern: long prose + fenced JSON
    prose_then_fence = (
        "You're right to call that out — and your observation is correct.\n\n"
        "**`#V#alvaro_orsi` does not exist yet.**\n"
        "What I gave you previously was a **descriptive plan**, not a persisted ontology change.\n\n"
        "Let's fix that cleanly and explicitly now.\n\n"
        "## ✅ Creating the concept now\n\n"
        "I am executing a real ontology operation below.\n\n"
        "```json\n"
        '{"action": "call_tool", "tool": "create_concepts", "payload": {"parent_id": "#V#person", "concepts": [{"name": "Alvaro Orsi"}]}}\n'
        "```\n\n"
        "Once this returns successfully, we can enrich it further.\n"
    )

    llm = _CapturingLLM(
        [
            prose_then_fence,  # First response: prose + fenced JSON (extraction should fail)
            json.dumps(
                {
                    "action": "call_tool",
                    "tool": "create_concepts",
                    "payload": {
                        "parent_id": "#V#person",
                        "concepts": [{"name": "Alvaro Orsi"}],
                    },
                }
            ),  # Retry: pure JSON
            "Concept created successfully.",  # Follow-up natural language
        ]
    )

    result = orchestrator.run(
        prompt="Create Alvaro Orsi", context=[], llm_client=llm, model=None
    )

    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
    assert "#V#alvaro_orsi does not exist yet" not in result.response_text
    assert result.tool_invocations
    assert result.tool_invocations[0]["tool"] == "create_concepts"
    assert result.tool_invocations[0]["payload"]["concepts"][0]["name"] == "Alvaro Orsi"
    # First response (prose+fence) + retry + at least one follow-up answer.
    assert len(llm.calls) >= 3


def _total_context_chars(context):
    if not context:
        return 0
    total = 0
    for msg in context:
        content = msg.get("content")
        total += len(content) if isinstance(content, str) else len(str(content))
    return total


def test_orchestrator_limits_context_by_chars():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_context_chars=30_000,
    )

    # Create a very large chat history with unique contents so trimming is testable.
    context = [
        {"role": "user", "content": f"msg-{i}:" + ("a" * 2_000)} for i in range(100)
    ]

    llm = _CapturingLLM(["hello world"])
    result = orchestrator.run(prompt="hi", context=context, llm_client=llm, model=None)

    assert result.response_text == "hello world"
    assert len(llm.calls) == 1

    sent_context = llm.calls[0]["context"]
    assert sent_context and sent_context[0]["role"] == "system"

    # Should be trimmed well below the original 100 messages.
    assert len(sent_context) < len(context) + 1

    # Context budget is approximate because system message is always retained.
    assert _total_context_chars(sent_context) <= 30_000 + 20_000

    # The newest user message should be present; the oldest should be trimmed.
    assert sent_context[-1]["content"].startswith("msg-99:")
    # After trimming, the earliest preserved message index should be > 0.
    first_preserved = next(msg for msg in sent_context[1:] if msg.get("role") == "user")
    assert first_preserved["content"].startswith("msg-")
    first_idx = int(first_preserved["content"].split(":", 1)[0].split("-", 1)[1])
    assert first_idx > 0


def test_orchestrator_preserves_presenter_protocol_when_trimming_context():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_context_chars=8_000,
    )

    presenter_protocol = {
        "role": "system",
        "content": (
            "PRESENTER MODE PROTOCOL:\n"
            "- Output EXACTLY TWO tagged blocks and nothing else:\n"
            "  <spoken>...brief talk track...</spoken>\n"
            "  <screen>...full on-screen content...</screen>\n"
        ),
    }

    # Force trimming with many large messages; presenter protocol would normally
    # be at risk of being dropped if it were not merged into the retained system
    # instruction message.
    context = [presenter_protocol] + [
        {"role": "user", "content": f"msg-{i}:" + ("a" * 2_000)} for i in range(30)
    ]

    llm = _CapturingLLM(["ok"])
    result = orchestrator.run(prompt="hi", context=context, llm_client=llm, model=None)

    assert result.response_text == "ok"
    sent_context = llm.calls[0]["context"]
    assert sent_context and sent_context[0]["role"] == "system"
    assert "PRESENTER MODE PROTOCOL:" in sent_context[0]["content"]


def test_orchestrator_truncates_tool_payload_in_context():
    _bootstrap_authoritative_workflows()
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
        max_tool_result_chars=5_000,
        max_tool_result_field_chars=1_500,
        max_context_chars=80_000,
    )

    llm = _CapturingLLM(
        [
            json.dumps({"action": "call_tool", "tool": "dummy", "payload": {}}),
            "done",
        ]
    )

    result = orchestrator.run(prompt="extract", context=[], llm_client=llm, model=None)

    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
    assert len(result.extra_messages) == 1

    tool_msg = result.extra_messages[0]
    assert tool_msg["role"] == "tool"
    assert len(tool_msg["content"]) <= 5_500

    # Ensure the tool output is still valid JSON.
    parsed = json.loads(tool_msg["content"])
    assert parsed["tool"] == "dummy"
    assert parsed["status"] == "ok"
