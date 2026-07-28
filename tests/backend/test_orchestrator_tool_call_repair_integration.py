from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, cast

import pytest

from src.backend.integrations.internal_mcp.gateway import MethodDefinition
from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
    OrchestratorResult,
)
from src.backend.integrations.internal_mcp.schemas import Schema
from src.backend.services.prompt_template_service import RenderedPrompt
from src.backend.services.synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
    SynthesiserContextFramingTemplate,
)
from src.backend.workflows.durable import synthesiser_context_prep_actions as synth_mod


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


class _JiraGateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []
        self._definition = MethodDefinition(
            name="jira_get_issue",
            handler=lambda **_kwargs: None,
            input_schema=Schema(
                required={"issue_key": str},
                optional={"fields": list, "expand": list, "namespace": str},
                allow_unknown=False,
                description="jira_get_issue input",
            ),
            category="read",
            description="Fetch a Jira issue by issue key",
        )

    def describe_methods(self) -> dict[str, Any]:
        return {
            "jira_get_issue": {
                "category": "read",
                "description": "Fetch a Jira issue by issue key",
                "input_schema": {
                    "required": {"issue_key": str},
                    "optional": {"fields": list, "expand": list, "namespace": str},
                    "allow_unknown": False,
                    "description": "jira_get_issue input",
                },
            }
        }

    def get_method_definition(self, method_name: str) -> MethodDefinition | None:
        if method_name == "jira_get_issue":
            return self._definition
        return None

    def invoke(self, tool_name: str, payload: Mapping[str, Any]) -> _TransportResult:
        self.invocations.append({"tool": tool_name, "payload": dict(payload)})
        return _TransportResult(
            payload={
                "key": payload.get("issue_key"),
                "summary": "Produce a use case for describing publication topics.",
                "status": "Done",
            },
            duration_ms=1.0,
        )


class _CreateConceptsGateway:
    enabled = True

    def __init__(self) -> None:
        self.invocations: list[dict[str, Any]] = []
        self._definition = MethodDefinition(
            name="create_concepts",
            handler=lambda **_kwargs: None,
            input_schema=Schema(
                required={"parent_id": str, "concepts": list},
                optional={"namespace": str},
                allow_unknown=False,
                description=(
                    "create_concepts input: parent_id (str, parent concept_id), "
                    "concepts (list of {name, kind?, description?, notes?})."
                ),
            ),
            category="write",
            description="Create Vontology concepts under a parent type.",
        )

    def describe_methods(self) -> dict[str, Any]:
        return {
            "create_concepts": {
                "category": "write",
                "description": "Create Vontology concepts under a parent type.",
                "input_schema": {
                    "required": {"parent_id": str, "concepts": list},
                    "optional": {"namespace": str},
                    "allow_unknown": False,
                    "description": (
                        "create_concepts input: parent_id (str, parent concept_id), "
                        "concepts (list of {name, kind?, description?, notes?})."
                    ),
                },
            }
        }

    def get_method_definition(self, method_name: str) -> MethodDefinition | None:
        if method_name == "create_concepts":
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


_TEST_TOOL_CALL_REPAIR_PROMPT = (
    "Return ONLY repaired tool-call JSON.\n"
    "Available tools:\n"
    "{tool_list}\n"
    "Validation errors:\n"
    "{errors}\n"
    "Original:\n"
    "{raw_tool_call}\n"
)


def _tool_calling_request(
    *,
    llm: Any,
    prompt: str,
    gateway: Any,
) -> SimpleNamespace:
    data: dict[str, Any] = {
        "prompt": prompt,
        "prompt_for_requirements": prompt,
        "augmented_context": [],
        "policy_state": SimpleNamespace(enabled=False, policy=None),
        "registry_snapshot": {},
        "user_concept_id": "#V#user",
        "org_concept_id": None,
        "model_for_stage": lambda _stage: None,
        "record_llm_call": lambda **_kwargs: None,
        "aux_llm_calls": [],
        "llm_calls": [],
        "invocations": [],
        "tool_messages": [],
        "iteration_count": 0,
        "allowed_write_tools": set(),
        "missing_tool_call_retry_attempts": 0,
        "missing_tool_call_retry_budget": 1,
        "tool_call_repair_attempts": 0,
        "tool_call_repair_budget": 1,
        "llm_allowed_tools": ["jira_get_issue"],
        "method_catalogue": gateway.describe_methods(),
        "build_validation_error_result": lambda errors, warnings, unavailable, **kwargs: OrchestratorResult(
            response_text=(
                "Tool call was not executed due to a validation error.\n"
                + "\n".join(str(error) for error in errors)
            ),
            extra_messages=(),
            tool_invocations=(
                {
                    "tool": "__tool_call_validation_error__",
                    "payload": {
                        "errors": list(errors),
                        "warnings": list(warnings),
                        "tool_unavailable": list(unavailable),
                        "raw_tool_call": kwargs.get("raw_tool_call"),
                    },
                    "error": "; ".join(errors),
                },
            ),
            aux_llm_calls=(),
        ),
    }
    return SimpleNamespace(
        action_id="tool_calling.respond",
        data=data,
        environment=SimpleNamespace(
            llm_client=llm,
            gateway=gateway,
            model=None,
            user_namespace="#V#user",
            user_concept_id="#V#user",
            org_concept_id=None,
            max_tool_invocations=4,
            max_tool_result_chars=4000,
            max_tool_result_field_chars=2000,
            default_gmail_profile=None,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="plan",
        workflow_state_metadata={},
    )


def _validation_request(
    *,
    gateway: Any,
) -> SimpleNamespace:
    data: dict[str, Any] = {
        "tool_calls": [
            {
                "tool": "create_concepts",
                "payload": {
                    "concepts": [{"name": "Assistant label", "kind": "type"}],
                    "namespace": "#V#user",
                },
            }
        ],
        "required_prompt_tools": ["create_concepts", "add_relationship"],
        "missing_prompt_tools": [],
        "prompt": "Create the represented ontology artefacts.",
        "prompt_for_requirements": "Create the represented ontology artefacts.",
        "policy_state": SimpleNamespace(enabled=False, policy=None),
        "registry_snapshot": {},
        "model_for_stage": lambda _stage: None,
        "record_llm_call": lambda **_kwargs: None,
        "aux_llm_calls": [],
        "llm_calls": [],
        "invocations": [],
        "tool_messages": [],
        "tool_call_repair_attempts": 0,
        "tool_call_repair_budget": 0,
        "llm_allowed_tools": ["create_concepts", "add_relationship"],
        "method_catalogue": gateway.describe_methods(),
        "build_validation_error_result": lambda errors, warnings, unavailable, **kwargs: OrchestratorResult(
            response_text="Tool validation failed.",
            extra_messages=(),
            tool_invocations=(),
            aux_llm_calls=(),
        ),
    }
    return SimpleNamespace(
        action_id="tool_calling.validate",
        data=data,
        environment=SimpleNamespace(
            llm_client=_SequencedLLM([]),
            gateway=gateway,
            model=None,
            user_namespace="#V#user",
            user_concept_id="#V#user",
            org_concept_id=None,
            max_tool_invocations=4,
            max_tool_result_chars=4000,
            max_tool_result_field_chars=2000,
            default_gmail_profile=None,
        ),
        trace=None,
        workflow_id="#V#tool_calling_workflow",
        workflow_state_id="validate",
        workflow_state_metadata={},
    )


@pytest.fixture(autouse=True)
def _stub_authoritative_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    template = SynthesiserContextFramingTemplate(
        prompt_concept_id="#V#test_synthesiser_context_framing_prompt",
        loaded_prompt_concept_id="#V#test_synthesiser_context_framing_prompt",
        schema_version=SYNTHESISER_CONTEXT_FRAMING_TEMPLATE_SCHEMA,
        active_request_template="Active request: {active_user_message}",
        tool_hints_template="Tool {tool_concept_id}:\n{hint_sections}",
        collection_presentation_hint_template=(
            "Collection: {collection_presentation_hint}"
        ),
        item_summary_hint_template="Item: {item_summary_hint}",
        diagnostics={"source": "represented_test_template"},
    )
    monkeypatch.setattr(
        synth_mod,
        "resolve_synthesiser_context_framing_template",
        lambda **_kwargs: (template, dict(template.diagnostics)),
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



def test_required_tool_validation_failure_surfaces_missing_required_tool() -> None:
    gateway = _CreateConceptsGateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))

    action_result = orchestrator._action_tool_calling_validate(
        _validation_request(gateway=gateway)
    )

    assert action_result.status == "success"
    assert action_result.outputs["tool_calls_validated"] is False
    assert action_result.outputs["tool_call_validation_required_failed_tools"] == [
        "create_concepts"
    ]
    assert action_result.outputs["missing_prompt_tools"] == [
        "create_concepts",
        "add_relationship",
    ]
    required_errors = action_result.outputs[
        "tool_call_validation_required_tool_errors"
    ]
    assert required_errors[0]["tool"] == "create_concepts"
    assert "parent_id" in required_errors[0]["message"]
    assert gateway.invocations == []



def test_tool_call_repair_recovers_mixed_jira_plan_with_invalid_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _JiraGateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    raw_mixed_plan = "\n".join(
        [
            '{"action": "call_tool", "tool": "jira_get_issue", "payload": {"issue_key": "JVNAUTOSCI-150"}}',
            '{"action": "call_tool", "tool": "jira_get_issue", "payload": {"key": "JVNAUTOSCI-150"}}',
            "I'm unable to give a grounded summary because Jira did not return an issue payload.",
        ]
    )
    llm = _SequencedLLM(
        [
            raw_mixed_plan,
            '{"action": "call_tool", "tool": "jira_get_issue", "payload": {"issue_key": "JVNAUTOSCI-150"}}',
            "JVNAUTOSCI-150 is Done.",
        ]
    )

    action_result = orchestrator._action_tool_calling_respond(
        _tool_calling_request(
            llm=llm,
            prompt="Tell me about JVNAUTOSCI-150 in JIRA",
            gateway=gateway,
        )
    )

    assert action_result.status == "success"
    assert gateway.invocations == [
        {
            "tool": "jira_get_issue",
            "payload": {"issue_key": "JVNAUTOSCI-150", "namespace": "#V#user"},
        }
    ]
    aux_llm_calls = action_result.outputs.get("aux_llm_calls") or []
    repair_events = [
        entry
        for entry in aux_llm_calls
        if isinstance(entry, Mapping) and entry.get("type") == "tool_call_repair"
    ]
    assert len(repair_events) == 2
    assert action_result.outputs["tool_call_repair_attempts"] == 1
    assert action_result.outputs["tool_call_repair_succeeded"] is True
    assert "validation error" not in str(
        action_result.outputs.get("response_text", "")
    ).lower()


def test_tool_call_repair_is_one_shot_before_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VON_TOOL_CALL_REPAIR_ENABLE", "1")

    gateway = _JiraGateway()
    orchestrator = InternalMCPChatOrchestrator(gateway=cast(Any, gateway))
    llm = _SequencedLLM(
        [
            '{"action": "call_tool", "tool": "jira_get_issue", "payload": {"key": "JVNAUTOSCI-150"}}',
            '{"action": "call_tool", "tool": "jira_get_issue", "payload": {"key": "JVNAUTOSCI-150"}}',
        ]
    )

    action_result = orchestrator._action_tool_calling_respond(
        _tool_calling_request(
            llm=llm,
            prompt="Tell me about JVNAUTOSCI-150 in JIRA",
            gateway=gateway,
        )
    )

    assert gateway.invocations == []
    assert len(llm.calls) == 2
    assert action_result.outputs["tool_call_repair_attempts"] == 1
    assert action_result.outputs["tool_call_repair_succeeded"] is False
    response_text = str(
        action_result.outputs.get("response_text")
        or action_result.outputs.get("final_response")
        or ""
    )
    assert "validation error" in response_text.lower()
    validation_invocations = [
        invocation
        for invocation in action_result.outputs["invocations"]
        if isinstance(invocation, Mapping)
        and invocation.get("tool") == "__tool_call_validation_error__"
    ]
    assert len(validation_invocations) == 1


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
