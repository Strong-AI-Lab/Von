from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

import pytest

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


class _SequencedLLM:
    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
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
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def test_tool_call_repair_recovers_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))

    llm = _SequencedLLM(
        [
            '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"test","top_k":"bad"}}',
            '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"test","top_k":20}}',
            "All done.",
        ]
    )

    result = orchestrator.run(
        prompt="Search the knowledge base",
        context=[],
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert gateway.invocations
    assert gateway.invocations[0]["payload"]["top_k"] == 20
    assert "validation error" not in result.response_text.lower()
