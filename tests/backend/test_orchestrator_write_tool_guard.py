"""Regression tests for guarding write-category tools in the internal orchestrator."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)


class _StubGateway:
    def __init__(self) -> None:
        self.enabled = True
        self.invoked: list[tuple[str, Mapping[str, Any]]] = []

    def describe_methods(self) -> dict[str, Any]:
        # Match InternalMCPGateway.describe_methods() shape closely enough for
        # _tool_listing() and the write-guard category lookup.
        return {
            "create_concepts": {
                "category": "write",
                "description": "Create one or more Vontology concepts.",
                "input_schema": {
                    "required": ["parent_id", "concepts"],
                    "optional": [],
                    "allow_unknown": False,
                    "description": None,
                },
                "output_schema": None,
            },
            "search_concepts": {
                "category": "read",
                "description": "Search concepts by name.",
                "input_schema": {
                    "required": ["name"],
                    "optional": [],
                    "allow_unknown": False,
                    "description": None,
                },
                "output_schema": None,
            },
        }

    def invoke(self, tool_name: str, payload: Mapping[str, Any]):
        self.invoked.append((tool_name, dict(payload)))
        raise AssertionError("Gateway should not be invoked for blocked tools")


class _CapturingLLM:
    def __init__(self, responses: Sequence[str]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        prompt: str,
        context: Optional[Sequence[Mapping[str, Any]]] = None,
        model=None,
    ):
        self.calls.append(
            {"prompt": prompt, "context": list(context or []), "model": model}
        )
        if not self._responses:
            raise AssertionError("LLM called more times than expected")
        return self._responses.pop(0)


def test_read_only_prompt_blocks_write_tool_call() -> None:
    gateway = cast(Any, _StubGateway())
    orchestrator = InternalMCPChatOrchestrator(gateway=gateway, max_tool_invocations=1)

    llm = _CapturingLLM(
        [
            '{"action": "call_tool", "tool": "create_concepts", "payload": {"parent_id": "#V#thing", "concepts": [{"name": "qiming"}]}}',
            "OK, I won't create anything. What name should I search for?",
        ]
    )

    result = orchestrator.run(
        prompt="Search for Qiming in the Vontology (do not create anything)",
        context=None,
        llm_client=llm,
        model=None,
        user_namespace="#V#user",
    )

    assert gateway.invoked == []
    assert any(
        inv.get("tool") == "create_concepts" and inv.get("blocked") is True
        for inv in result.tool_invocations
        if isinstance(inv, Mapping)
    )
