import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


class DummyGateway:
    def __init__(self):
        self.enabled = True
        self.calls = []

    def describe_methods(self):
        return {
            "gmail_list_messages": {
                "description": "",
                "input_schema": {"required": ["profile"], "optional": {}},
            }
        }

    def invoke(self, method_name, payload):
        self.calls.append((method_name, dict(payload)))

        class Result:
            def __init__(self):
                self.payload = {"ok": True}
                self.duration_ms = 1.0

        return Result()


class DummyLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, prompt, context=None, model=None):
        if not self.responses:
            raise RuntimeError("No responses left in DummyLLM")
        return self.responses.pop(0)


def test_injects_default_gmail_profile_into_payload():
    gateway = DummyGateway()
    llm = DummyLLM(
        [
            '{"action": "call_tool", "tool": "gmail_list_messages", "payload": {}}',
            "Final response",
        ]
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
        default_gmail_profile="service-profile",
    )

    result = orchestrator.run(
        prompt="hello",
        context=None,
        llm_client=llm,
        model="test-model",
        user_namespace="#V#user",
    )

    assert gateway.calls, "Gateway should have been invoked"
    method_name, payload = gateway.calls[0]
    assert method_name == "gmail_list_messages"
    assert payload["profile"] == "service-profile"
    assert result.response_text == "Final response"


def test_gmail_profile_prefers_request_over_default():
    gateway = DummyGateway()
    llm = DummyLLM(
        [
            '{"action": "call_tool", "tool": "gmail_list_messages", "payload": {}}',
            "All good",
        ]
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
        default_gmail_profile="default-profile",
    )

    result = orchestrator.run(
        prompt="hi",
        context=None,
        llm_client=llm,
        model="test-model",
        user_namespace="#V#user",
        gmail_profile="user-picked",
    )

    assert gateway.calls, "Gateway should have been invoked"
    _, payload = gateway.calls[0]
    assert payload["profile"] == "user-picked"
    assert result.response_text == "All good"
