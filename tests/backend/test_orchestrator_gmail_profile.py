from collections.abc import Iterator
from types import SimpleNamespace

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
                "input_schema": {
                    "required": ["profile"],
                    "optional": {
                        "query": str,
                        "label_ids": list,
                        "max_results": int,
                        "maxResults": int,
                    },
                    "aliases": {
                        "profile_id": "profile",
                        "identity": "profile",
                        "user_id": "profile",
                        "q": "query",
                    },
                    "batch_propagated_fields": ["profile"],
                },
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
def _bootstrap_conversation_turn_authority() -> Iterator[None]:
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


def test_gmail_payload_aliases_are_normalised_before_validation():
    gateway = DummyGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=1,
    )
    tool_calls = orchestrator._extract_tool_calls(
        (
            '{"action":"call_tool","tool":"gmail_list_messages",'
            '"payload":{"identity":"zhan-gmail","q":"in:inbox",'
            '"max_results":10}}'
        )
    )

    preflight = orchestrator._preflight_tool_calls(
        tool_calls or [],
        gateway.describe_methods(),
        allowed_tool_names=None,
        user_namespace=None,
        selected_gmail_profile=None,
    )

    assert preflight.errors == []
    assert tool_calls is not None
    assert tool_calls[0]["payload"] == {
        "profile": "zhan-gmail",
        "query": "in:inbox",
        "max_results": 10,
    }


def test_gmail_batch_uses_alias_profile_hint_for_missing_profile():
    gateway = DummyGateway()
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,  # type: ignore[arg-type]
        max_tool_invocations=2,
    )
    tool_calls = orchestrator._extract_tool_calls(
        (
            '{"action":"call_tool","tool":"gmail_list_messages",'
            '"payload":{"identity":"zhan-gmail","q":"in:inbox",'
            '"max_results":10}}\n'
            '{"action":"call_tool","tool":"gmail_list_messages",'
            '"payload":{"q":"in:inbox","maxResults":10}}'
        )
    )

    preflight = orchestrator._preflight_tool_calls(
        tool_calls or [],
        gateway.describe_methods(),
        allowed_tool_names=None,
        user_namespace=None,
        selected_gmail_profile=None,
    )

    assert preflight.errors == []
    assert tool_calls is not None
    assert [dict(call["payload"]) for call in tool_calls] == [
        {
            "profile": "zhan-gmail",
            "query": "in:inbox",
            "max_results": 10,
        },
        {
            "profile": "zhan-gmail",
            "query": "in:inbox",
            "maxResults": 10,
        },
    ]


def test_explicit_prompt_tool_requirements_include_named_fetch_tool():
    requirements = (
        InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
            "If the listing lacks metadata, fetch details with gmail_get_message.",
            method_catalogue={
                "gmail_list_messages": {},
                "gmail_get_message": {},
            },
        )
    )

    assert requirements == ["gmail_get_message"]

    negated = InternalMCPChatOrchestrator._extract_explicit_prompt_tool_requirements(
        "Use gmail_list_messages, but do not use gmail_get_message.",
        method_catalogue={
            "gmail_list_messages": {},
            "gmail_get_message": {},
        },
    )

    assert negated == ["gmail_list_messages"]


def test_missing_required_gmail_detail_tool_uses_list_follow_up_contract():
    orchestrator = InternalMCPChatOrchestrator(
        gateway=DummyGateway(),  # type: ignore[arg-type]
        max_tool_invocations=4,
    )

    calls = orchestrator._infer_missing_tool_call_retry_tool_calls(
        [],
        user_prompt=(
            "Use gmail_list_messages, then fetch details with gmail_get_message "
            "for listed messages missing sender, subject, date, or snippet."
        ),
        missing_required_tools=["gmail_get_message"],
        tool_invocations=[
            {
                "tool": "gmail_list_messages",
                "status": "ok",
                "payload": {
                    "profile": "zhan-gmail",
                    "query": "in:inbox",
                    "max_results": 2,
                },
                "effective_arguments": {
                    "profile": "zhan-gmail",
                    "query": "in:inbox",
                    "max_results": 2,
                },
                "effective_payload": {
                    "messages": [
                        {"id": "msg-1", "message_id": "msg-1"},
                        {
                            "id": "msg-2",
                            "message_id": "msg-2",
                            "subject": "Already listed",
                        },
                        {
                            "id": "msg-3",
                            "message_id": "msg-3",
                            "sender": "Sender",
                            "subject": "Complete",
                            "date": "Sat, 25 Apr 2026 09:00:00 +0000",
                            "snippet": "Preview",
                        },
                    ],
                    "_tool_follow_up": {
                        "schema_version": "mcp_tool_follow_up.v1",
                        "source_tool": "gmail_list_messages",
                        "item_array_field": "messages",
                        "item_id_field": "message_id",
                        "required_when_any_item_missing_fields": [
                            "sender",
                            "subject",
                            "date",
                            "snippet",
                        ],
                        "follow_up_tools": [
                            {
                                "tool": "gmail_get_message",
                                "input_bindings": {
                                    "profile": {
                                        "source": "request",
                                        "field": "profile",
                                    },
                                    "message_id": {
                                        "source": "item",
                                        "field": "message_id",
                                    },
                                },
                            }
                        ],
                    },
                },
            }
        ],
    )

    assert calls is not None
    assert [call["tool"] for call in calls] == [
        "gmail_get_message",
        "gmail_get_message",
    ]
    assert [dict(call["payload"]) for call in calls] == [
        {"profile": "zhan-gmail", "message_id": "msg-1"},
        {"profile": "zhan-gmail", "message_id": "msg-2"},
    ]
    assert all(
        call.get("_retry_binding_source") == "tool_follow_up_contract"
        for call in calls
    )


def test_backfill_drains_tool_declared_follow_up_before_summariser(monkeypatch):
    orchestrator = InternalMCPChatOrchestrator(
        gateway=DummyGateway(),  # type: ignore[arg-type]
        max_tool_invocations=5,
    )

    def _fail_if_summariser_runs(**_kwargs):
        raise AssertionError("summariser should not run before pending follow-ups")

    monkeypatch.setattr(orchestrator, "_run_llm_with_fallbacks", _fail_if_summariser_runs)

    follow_up_contract = {
        "schema_version": "mcp_tool_follow_up.v1",
        "source_tool": "gmail_list_messages",
        "item_array_field": "messages",
        "item_id_field": "message_id",
        "required_when_any_item_missing_fields": [
            "sender",
            "subject",
            "date",
            "snippet",
        ],
        "follow_up_tools": [
            {
                "tool": "gmail_get_message",
                "input_bindings": {
                    "profile": {"source": "request", "field": "profile"},
                    "message_id": {"source": "item", "field": "message_id"},
                },
            }
        ],
    }
    request = SimpleNamespace(
        data={
            "augmented_context": [],
            "policy_state": None,
            "registry_snapshot": None,
            "user_concept_id": "#V#test_user",
            "org_concept_id": "#V#test_org",
            "model_for_stage": lambda _stage: "gpt-test",
            "record_llm_call": lambda **_kwargs: None,
            "aux_llm_calls": [],
            "llm_calls": [],
            "iteration_count": 2,
            "remaining_tool_calls": [],
            "invocations": [
                {
                    "tool": "gmail_list_messages",
                    "status": "ok",
                    "effective_arguments": {
                        "profile": "zhan-gmail",
                        "query": "in:inbox",
                    },
                    "effective_payload": {
                        "messages": [
                            {"message_id": "msg-1"},
                            {"message_id": "msg-2"},
                        ],
                        "_tool_follow_up": follow_up_contract,
                    },
                },
                {
                    "tool": "gmail_get_message",
                    "status": "ok",
                    "effective_arguments": {
                        "profile": "zhan-gmail",
                        "message_id": "msg-1",
                    },
                    "effective_payload": {
                        "message_id": "msg-1",
                        "sender": "sender",
                        "subject": "subject",
                        "date": "date",
                        "snippet": "snippet",
                    },
                },
            ],
        },
        environment=SimpleNamespace(
            llm_client=object(),
            max_tool_invocations=5,
        ),
    )

    result = orchestrator._action_tool_calling_backfill(request)

    assert result.outputs["more_tool_calls"] is True
    assert result.outputs["tool_calls"] == [
        {
            "action": "call_tool",
            "tool": "gmail_get_message",
            "payload": {"profile": "zhan-gmail", "message_id": "msg-2"},
            "_retry_binding_source": "tool_follow_up_contract",
        }
    ]
