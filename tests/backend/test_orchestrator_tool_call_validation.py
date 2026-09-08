from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence

from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.integrations.internal_mcp import orchestrator as orchestrator_mod
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)


@dataclass(frozen=True)
class _TransportResult:
    payload: Any
    duration_ms: float


class _Gateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []
        self._definition = MethodDefinition(
            name="search_knowledge_base",
            handler=lambda **_kwargs: None,
            input_schema=Schema(
                required={"query": str},
                optional={"top_k": int},
                allow_unknown=False,
                description="search params",
            ),
            category="read",
            description="Search the knowledge base",
        )

    def describe_methods(self) -> dict[str, Any]:
        return {
            "search_knowledge_base": {
                "category": "read",
                "description": "Search the knowledge base",
                "input_schema": {
                    "required": {"query": str},
                    "optional": {"top_k": int},
                    "allow_unknown": False,
                    "description": "search params",
                },
            }
        }

    def get_method_definition(self, method_name: str) -> MethodDefinition | None:
        if method_name == "search_knowledge_base":
            return self._definition
        return None

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> _TransportResult:
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        return _TransportResult(payload={"ok": True}, duration_ms=1.0)


class _LLM:
    def __init__(self, response: str):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ) -> str:
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        return self._response




def test_tool_calling_loop_records_terminal_timeout_not_late_success(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        orchestrator_mod,
        "validate_tool_target_contract",
        lambda **_kwargs: SimpleNamespace(ok=True, resolution_evidence=()),
    )
    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="synthetic_slow_read",
            handler=lambda: time.sleep(0.15),
            input_schema=Schema(required={}, optional={}, allow_unknown=False),
            output_schema=None,
            category="read",
            description="Synthetic bounded read used to verify deadline propagation.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(
            read_timeout_sec=0.03,
            read_advisory_timeout_sec=0.01,
        ),
        enabled=True,
    )
    orchestrator = InternalMCPChatOrchestrator(
        gateway=gateway,
        max_tool_invocations=1,
    )
    request = WorkflowActionRequest(
        action_id="tool_calling.execute",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=None,
            gateway=gateway,
            user_namespace="#V#user",
            max_tool_invocations=1,
        ),
        data={
            "prompt": "Use the synthetic bounded read.",
            "response": "",
            "augmented_context": [],
            "tool_calls": [
                {
                    "action": "call_tool",
                    "tool": "synthetic_slow_read",
                    "payload": {},
                    "_call_id": "call-synthetic-timeout-1",
                }
            ],
            "method_catalogue": gateway.describe_methods(),
            "tool_categories": {"synthetic_slow_read": "read"},
            "iteration_count": 0,
            "aux_llm_calls": [],
            "llm_calls": [],
        },
    )

    result = orchestrator._action_tool_calling_execute(request)

    assert result.outputs["tool_execution_complete"] is True
    invocation = request.data["invocations"][0]
    # Keep the deadline assertion on the transport span.  The first
    # orchestration action in a fresh process may also initialise unrelated
    # evidence/workflow support modules, which is not handler monopolisation.
    assert invocation["transport"]["duration_ms"] < 200.0
    assert invocation["status"] == "timeout"
    assert invocation["error_code"] == "tool_timeout"
    assert invocation["effective_payload"]["success"] is False
    assert invocation["effective_payload"]["retryable"] is True
    assert invocation["transport"]["outcome"] == "timed_out"
    assert invocation["transport"]["late_result_policy"] == "discard_from_turn"
    assert invocation["handler_duration_ms"] is None
    assert invocation["handler_elapsed_ms"] is not None
    assert (
        InternalMCPChatOrchestrator._tool_invocation_completed_successfully(
            invocation
        )
        is False
    )


def test_workflow_tool_images_follow_correlated_tool_batch(monkeypatch):
    """Durable LLM steps need native image evidence, not only descriptor JSON."""
    monkeypatch.setattr(orchestrator_mod, 'validate_tool_target_contract',
                        lambda **kwargs: SimpleNamespace(ok=True,resolution_evidence=()))
    descriptor = {'concept_id':'#V#slide_image','sha256':'synthetic'}
    catalogue = MethodCatalogue()
    catalogue.register(MethodDefinition(name='synthetic_slide_read',
        handler=lambda **kwargs: {'success':True,'image_attachments':[descriptor]},
        input_schema=Schema(required={}, optional={}, allow_unknown=False),
        category='read'))
    gateway = InternalMCPGateway(catalogue=catalogue,transport=InternalMCPTransport(),enabled=True)
    orchestrator = InternalMCPChatOrchestrator(gateway=gateway,max_tool_invocations=2)
    request = WorkflowActionRequest(action_id='tool_calling.execute',inputs={},
        environment=WorkflowEnvironment(llm_client=None,gateway=gateway,user_namespace='#V#user',max_tool_invocations=2),
        data={'prompt':'Read the slide images','response':'','augmented_context':[],
              'tool_calls':[{'action':'call_tool','tool':'synthetic_slide_read','payload':{},'_call_id':f'call-{i}'} for i in range(2)],
              'method_catalogue':gateway.describe_methods(), 'tool_categories':{'synthetic_slide_read':'read'},
              'iteration_count':0,'aux_llm_calls':[],'llm_calls':[]})
    orchestrator._action_tool_calling_execute(request)
    messages = request.data['tool_messages']
    assert [m['role'] for m in messages] == ['tool','tool','user','user']
    assert [m['tool_call_id'] for m in messages[:2]] == ['call-0','call-1']
    assert messages[2]['image_attachments'] == [descriptor]
    assert 'tool_call_id' not in messages[2]
    assert request.data['augmented_context'][-2:] == messages[-2:]
    assert 'base64' not in str(messages)
