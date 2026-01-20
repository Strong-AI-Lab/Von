from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

from src.backend.integrations.internal_mcp.gateway import MethodDefinition
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.integrations.internal_mcp.schemas import Schema


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


def test_tool_call_payload_coercion_allows_numeric_string() -> None:
    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))

    llm = _LLM(
        '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"test","top_k":"20"}}'
    )

    result = orchestrator.run(
        prompt="Search the knowledge base",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert result.tool_invocations
    assert gateway.invocations
    payload = gateway.invocations[0]["payload"]
    assert payload["top_k"] == 20


def test_tool_unavailable_returns_validation_error() -> None:
    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))

    llm = _LLM(
        '{"action":"call_tool","tool":"missing_tool","payload":{"query":"test"}}'
    )

    result = orchestrator.run(
        prompt="Try an unavailable tool",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert not gateway.invocations
    assert result.tool_invocations
    assert result.tool_invocations[-1]["tool"] == "__tool_call_validation_error__"
    assert "Unavailable tools" in result.response_text
