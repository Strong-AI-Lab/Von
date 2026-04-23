from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, cast

import pytest

from src.backend.integrations.internal_mcp.gateway import MethodDefinition
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.services.prompt_template_service import RenderedPrompt


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


_TEST_BASE_PROMPT = (
    "You have access to internal MCP tools.\n\n"
    "{auth_status}\n"
    "INTERNAL EXECUTION GUARDRAILS:\n"
    "- Do NOT mention budgets, caps, or internal limits unless the user explicitly asks for diagnostics.\n"
    "Available tools:\n"
    "{listing}"
)
_TEST_TOOL_CALL_REPAIR_PROMPT = (
    "Return ONLY repaired tool-call JSON.\n"
    "Available tools:\n"
    "{tool_list}\n"
    "Validation errors:\n"
    "{errors}\n"
    "Original:\n"
    "{raw_tool_call}\n"
)


@pytest.fixture(autouse=True)
def _stub_authoritative_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda self, preferred_language=None: (
            _TEST_BASE_PROMPT,
            "#V#test_base_prompt",
        ),
    )

    def _fake_render_prompt(
        _self,
        concept_ids,
        *,
        variables=None,
        fallback=None,
        max_chars=None,
    ):
        prompt_ids = tuple(concept_ids or ())
        if "#V#tool_call_repair_prompt" not in prompt_ids:
            prompt_id = "#V#test_stub_prompt"
            for raw_prompt_id in prompt_ids:
                if isinstance(raw_prompt_id, str) and raw_prompt_id.strip():
                    prompt_id = raw_prompt_id
                    break
            return RenderedPrompt(
                prompt_id=prompt_id,
                text=str(fallback or "Prompt stub"),
                variables=dict(variables or {}),
                truncated=False,
            )
        rendered = _TEST_TOOL_CALL_REPAIR_PROMPT
        for key, value in dict(variables or {}).items():
            rendered = rendered.replace("{" + key + "}", str(value))
        return RenderedPrompt(
            prompt_id="#V#tool_call_repair_prompt",
            text=rendered,
            variables=dict(variables or {}),
            truncated=False,
        )

    monkeypatch.setattr(
        "src.backend.services.prompt_template_service.PromptTemplateService.render_prompt",
        _fake_render_prompt,
    )


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
    assert gateway.invocations[0]["tool"] == "search_knowledge_base"
    assert isinstance(gateway.invocations[0]["payload"]["query"], str)
    assert gateway.invocations[0]["payload"]["query"].strip()
    assert isinstance(gateway.invocations[0]["payload"]["top_k"], int)
    assert "validation error" not in result.response_text.lower()


def test_tool_call_repair_recovers_unknown_tool_with_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))

    llm = _SequencedLLM(
        [
            '[{"tool":"#V_concept_search","params":{"query":"test"}}]',
            '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"test","top_k":5}}',
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
    assert gateway.invocations[0]["tool"] == "search_knowledge_base"
    assert isinstance(gateway.invocations[0]["payload"]["query"], str)
    assert gateway.invocations[0]["payload"]["query"].strip()
    assert isinstance(gateway.invocations[0]["payload"]["top_k"], int)
    assert "validation error" not in result.response_text.lower()


def test_attempt_tool_call_repair_emits_annotated_prompt_and_response_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    llm = _SequencedLLM(
        [
            '{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"fixed","top_k":7}}'
        ]
    )
    aux_llm_calls: list[Mapping[str, Any]] = []

    repaired_calls = orchestrator._attempt_tool_call_repair(
        current_response='{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"fixed","top_k":"bad"}}',
        errors=["payload.top_k must be int"],
        tool_list=["search_knowledge_base"],
        llm_client=llm,
        policy_state=cast(Any, None),
        default_model="test-model",
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        aux_llm_calls=aux_llm_calls,
        llm_calls_log=[],
        record_llm_call=None,
    )

    assert repaired_calls is not None
    assert repaired_calls[0]["tool"] == "search_knowledge_base"
    assert repaired_calls[0]["payload"]["top_k"] == 7

    repair_entries = [
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "tool_call_repair"
    ]
    assert len(repair_entries) == 2
    assert repair_entries[0].get("decision_class") == "tool_call_repair_prompt"
    assert repair_entries[0].get("decision_source") == "workflow_retry_prompt"
    assert repair_entries[1].get("decision_class") == "tool_call_repair_response"
    assert repair_entries[1].get("decision_source") == "workflow_retry_response"


def test_attempt_tool_call_repair_prefers_required_tools_for_unavailable_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    llm = _SequencedLLM(
        [
            '{"action":"call_tool","tool":"get_predicate_incidence","payload":{"concept_id":"#V#test_user"}}'
        ]
    )
    aux_llm_calls: list[Mapping[str, Any]] = []

    repaired_calls = orchestrator._attempt_tool_call_repair(
        current_response='{"action":"call_tool","tool":"get_all_predicates","payload":{"concept_id":"#V#test_user"}}',
        errors=["Tool 'get_all_predicates' is unavailable."],
        tool_list=[
            "get_predicate_incidence",
            "find_relations_with_argument",
            "search_knowledge_base",
        ],
        preferred_tools=[
            "get_predicate_incidence",
            "find_relations_with_argument",
        ],
        llm_client=llm,
        policy_state=cast(Any, None),
        default_model="test-model",
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        aux_llm_calls=aux_llm_calls,
        llm_calls_log=[],
        record_llm_call=None,
    )

    assert repaired_calls is not None
    assert repaired_calls[0]["tool"] == "get_predicate_incidence"
    repair_prompt = llm.calls[0]["prompt"]
    assert "- get_predicate_incidence" in repair_prompt
    assert "- find_relations_with_argument" in repair_prompt
    assert "- search_knowledge_base" not in repair_prompt

    repair_entries = [
        entry
        for entry in aux_llm_calls
        if isinstance(entry, dict) and entry.get("type") == "tool_call_repair"
    ]
    assert repair_entries
    assert repair_entries[0].get("repair_tool_list_strategy") == (
        "preferred_tools_only_for_unavailable_tool"
    )
    assert repair_entries[0].get("preferred_tools") == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]


def test_attempt_tool_call_repair_skips_when_authoritative_prompt_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _Gateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    llm = _SequencedLLM([])
    aux_llm_calls: list[Mapping[str, Any]] = []

    monkeypatch.setattr(
        orchestrator._prompt_templates,
        "render_prompt",
        lambda *args, **kwargs: None,
    )

    repaired_calls = orchestrator._attempt_tool_call_repair(
        current_response='{"action":"call_tool","tool":"search_knowledge_base","payload":{"query":"fixed","top_k":"bad"}}',
        errors=["payload.top_k must be int"],
        tool_list=["search_knowledge_base"],
        llm_client=llm,
        policy_state=cast(Any, None),
        default_model="test-model",
        registry_snapshot=None,
        user_concept_id=None,
        org_concept_id=None,
        aux_llm_calls=aux_llm_calls,
        llm_calls_log=[],
        record_llm_call=None,
    )

    assert repaired_calls is None
    assert llm.calls == []
    assert aux_llm_calls == []
