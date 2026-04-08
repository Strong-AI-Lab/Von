import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.workflows.durable.registry_factory import (
    invalidate_shared_workflow_registry_read_only,
)
from workflow_test_support import bootstrap_authoritative_conversation_turn_workflows
class DummyGateway:
    def __init__(self):
        self.enabled = True
        self.calls = []

    def describe_methods(self):
        return {
            "gmail_list_messages": {
                "description": "List Gmail messages for a profile.",
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


@pytest.fixture(autouse=True)
def _bootstrap_conversation_turn_authority() -> None:
    invalidate_shared_workflow_registry_read_only()
    bootstrap_authoritative_conversation_turn_workflows()
    invalidate_shared_workflow_registry_read_only()
    yield
    invalidate_shared_workflow_registry_read_only()


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
    )

    assert gateway.calls, "Gateway should have been invoked"
    gmail_calls = [
        (method_name, payload)
        for method_name, payload in gateway.calls
        if method_name == "gmail_list_messages"
    ]
    assert gmail_calls, "gmail_list_messages should have been invoked"
    method_name, payload = gmail_calls[0]
    assert method_name == "gmail_list_messages"
    assert payload["profile"] == "service-profile"
    assert isinstance(result.response_text, str)
    assert result.response_text.strip()


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
        gmail_profile="user-picked",
    )

    assert gateway.calls, "Gateway should have been invoked"
    gmail_calls = [
        payload
        for method_name, payload in gateway.calls
        if method_name == "gmail_list_messages"
    ]
    assert gmail_calls, "gmail_list_messages should have been invoked"
    payload = gmail_calls[0]
    assert payload["profile"] == "user-picked"
    assert isinstance(result.response_text, str)
    assert result.response_text.strip()
