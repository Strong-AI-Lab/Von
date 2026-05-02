from __future__ import annotations

from unittest.mock import MagicMock

from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.conversation_turn_llm_timeout import (
    DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC,
)
from src.backend.workflows.llm_step_executor import execute_llm_step
from src.backend.workflows.llm_step_executor import _compose_llm_prompt


def _build_request(*, llm_response: str) -> WorkflowActionRequest:
    llm_client = MagicMock()
    llm_client.generate.return_value = llm_response
    return WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={"invitation_text": "Please meet on Monday at 10am."},
        prompt_contract={
            "prompt_text": "Return JSON only for the meeting invitation.",
        },
        validation_policy={"output_format": "json_value"},
    )


def test_compose_llm_prompt_includes_workflow_experience_guidance_labels() -> None:
    prompt = _compose_llm_prompt(
        base_prompt="Use the workflow policy.",
        llm_policy={
            "context_fields": [
                {
                    "context_key": "workflow_success_guidance_history",
                    "label": (
                        "Historical successful-run guidance: soft hints from "
                        "prior successful executions."
                    ),
                },
                {
                    "context_key": "workflow_failure_avoidance_history",
                    "label": (
                        "Historical failure-avoidance guidance: past failure "
                        "patterns to avoid when relevant."
                    ),
                },
                {
                    "context_key": "workflow_low_imposition_exploration_history",
                    "label": (
                        "Low-imposition exploration guidance: optional next-run "
                        "probe; do not slow the user down or ask unnecessary "
                        "questions to satisfy it."
                    ),
                },
            ]
        },
        context={
            "workflow_success_guidance_history": [
                {"text": "workflow_experience_guidance.v1\nbody:\nReuse evidence."}
            ],
            "workflow_failure_avoidance_history": [
                {"text": "workflow_experience_guidance.v1\nbody:\nAvoid guessing."}
            ],
            "workflow_low_imposition_exploration_history": [
                {
                    "text": (
                        "workflow_experience_guidance.v1\nbody:\n"
                        "Inspect telemetry before asking the user."
                    )
                }
            ],
        },
    )

    assert "Historical successful-run guidance: soft hints" in prompt
    assert "Historical failure-avoidance guidance: past failure patterns" in prompt
    assert "Low-imposition exploration guidance: optional next-run probe" in prompt
    assert "Reuse evidence." in prompt
    assert "Avoid guessing." in prompt
    assert "Inspect telemetry before asking the user." in prompt


def test_execute_llm_step_parses_json_value_output() -> None:
    request = _build_request(
        llm_response='{"meeting_type":"project_meeting","title":"Roadmap sync"}'
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {
        "meeting_type": "project_meeting",
        "title": "Roadmap sync",
    }
    assert result.outputs["validated_json_parse_mode"] in {
        "strict_json",
        "direct_json",
    }
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "success"
    assert envelope["validation"]["output_format"] == "json_value"


def test_execute_llm_step_recovers_embedded_json_value_output() -> None:
    request = _build_request(
        llm_response=(
            'Analysis: an example shape could be {"ignored": true}.\n'
            "Final JSON:\n"
            '{"meeting_type":"project_meeting","title":"Roadmap sync"}'
        )
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {
        "meeting_type": "project_meeting",
        "title": "Roadmap sync",
    }
    assert result.outputs["validated_json_parse_mode"] == "embedded_json"


def test_execute_llm_step_normalises_expected_outcome_json_contract_aliases() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock()),
        data={
            "expected_outcome_prompt_id": (
                "#V#prompt_turn_execution_expected_outcome_inference"
            ),
            "expected_outcome_prompt_text": "Return the expected-outcome JSON.",
        },
        prompt_contract={},
        llm_policy={
            "prompt_id_context_key": "expected_outcome_prompt_id",
            "prompt_text_context_key": "expected_outcome_prompt_text",
        },
        validation_policy={"output_format": "json_value"},
    )
    request.environment.llm_client.generate.return_value = (
        "```json\n"
        '{"expected_outcome_summary":"List the grounded predicates.",'
        '"grounding_relation":"Use retrieved ontology evidence.",'
        '"precision_policy":"Only list confirmed predicates.",'
        '"selector_guidance":"Search the concept, then inspect predicates.",'
        '"answering_guidance":"Group the retrieved predicates.",'
        '"reasoning":"This is a schema discovery request.",'
        '"required_tools":["search_concepts","get_predicate_extent"]}'
        "\n```"
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"]["grounding_requirement"] == (
        "Use retrieved ontology evidence."
    )
    assert result.outputs["validated_json"]["required_tools"] == [
        "search_concepts",
        "get_predicate_incidence",
    ]


def test_execute_llm_step_normalises_expected_outcome_by_workflow_state() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock()),
        data={},
        prompt_contract={
            "prompt_text": "Return the expected-outcome JSON.",
        },
        validation_policy={"output_format": "json_value"},
        workflow_state_id=(
            "#V#workflow_step_conversation_turn_execution_workflow_"
            "expected_outcome_inference"
        ),
    )
    request.environment.llm_client.generate.return_value = (
        '{"summary":"List grounded scientific-paper predicates.",'
        '"evidence_standard":"Use Vontology relation evidence.",'
        '"required_tools":["get_predicates_for_class"]}'
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"]["grounding_requirement"] == (
        "Use Vontology relation evidence."
    )
    assert result.outputs["validated_json"]["required_tools"] == [
        "get_predicate_incidence"
    ]


def test_execute_llm_step_applies_json_field_defaults_from_validation_policy() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock()),
        data={},
        prompt_contract={
            "prompt_text": "Return the expected-outcome JSON.",
        },
        validation_policy={
            "output_format": "json_value",
            "json_field_defaults": {
                "expected_outcome_summary": "Answer from grounded evidence.",
                "grounding_requirement": "Use authoritative context.",
                "precision_policy": "State uncertainty when needed.",
                "required_tools": [],
            },
            "required_json_fields": [
                "expected_outcome_summary",
                "grounding_requirement",
                "precision_policy",
                "required_tools",
            ],
        },
        workflow_state_id="expected_outcome_inference",
    )
    request.environment.llm_client.generate.return_value = "[]"

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {
        "expected_outcome_summary": "Answer from grounded evidence.",
        "grounding_requirement": "Use authoritative context.",
        "precision_policy": "State uncertainty when needed.",
        "required_tools": [],
    }
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["json_object_defaulted_from_non_object"] is True
    assert envelope["validation"]["json_defaults_applied"] == [
        "expected_outcome_summary",
        "grounding_requirement",
        "precision_policy",
        "required_tools",
    ]


def test_execute_llm_step_fails_json_value_when_required_fields_missing() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock()),
        data={},
        prompt_contract={
            "prompt_text": "Return a JSON object with the required fields.",
        },
        validation_policy={
            "output_format": "json_value",
            "required_json_fields": ["must_exist"],
        },
    )
    request.environment.llm_client.generate.return_value = '{"other": true}'

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert result.error == "json_required_fields_missing"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["missing_required_fields"] == ["must_exist"]


def test_execute_llm_step_fails_closed_when_json_value_is_invalid() -> None:
    request = _build_request(llm_response="This is not valid JSON.")

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert "json_parse_failed" in str(result.error or "")
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "failed"
    assert "json_parse_failed" in str(envelope["validation"]["reason"] or "")


def test_execute_llm_step_passes_context_lineage_to_gateway_llm(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["context"] = kwargs.get("context")
            captured["context_telemetry"] = kwargs.get("context_telemetry")
            captured["prefer_default_model"] = kwargs.get("prefer_default_model")
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "requested_model": "gemma4:26b",
            "selector_context_messages": [
                {"role": "system", "content": "Selector prompt"},
                {"role": "user", "content": "Who am I?"},
            ],
            "selector_context_lineage": {
                "stage": "selector_decision",
                "base_context_source": "augmented_context",
                "stage_added_message_count": 1,
            },
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "context_messages_context_key": "selector_context_messages",
            "context_lineage_context_key": "selector_context_lineage",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["context"] == [
        {"role": "system", "content": "Selector prompt"},
        {"role": "user", "content": "Who am I?"},
    ]
    assert captured["context_telemetry"] == {
        "stage": "selector_decision",
        "base_context_source": "augmented_context",
        "stage_added_message_count": 1,
    }
    assert captured["prefer_default_model"] is True


def test_execute_llm_step_tool_mode_marks_user_model_preference(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubGateway:
        def describe_methods(self) -> dict[str, object]:
            return {}

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            captured["max_tool_invocations"] = _kwargs.get("max_tool_invocations")

        def _load_workflow_model_policy(self, _preferred_language):
            return object(), {}

        def _select_model_for_stage(self, **kwargs):
            captured["prefer_default_model"] = kwargs.get("prefer_default_model")
            return kwargs.get("default_model")

        def _action_tool_calling_plan(self, request):
            captured["shared_prefer_default_model"] = request.data.get(
                "prefer_default_model"
            )
            request.data["model_for_stage"]("tool_call")
            return type(
                "_Result",
                (),
                {
                    "status": "success",
                    "outputs": {
                        "tool_calls_present": False,
                        "orchestrator_result": {"response_text": '{"ok": true}'},
                    },
                },
            )()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: {},
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="gemma4:26b",
        ),
        data={
            "requested_model": "gemma4:26b",
            "requested_client_type": "ollama",
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"tool_mode": "allowed"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["shared_prefer_default_model"] is True
    assert captured["prefer_default_model"] is True


def test_execute_llm_step_tool_mode_filters_turn_contract_tools_to_allowed_workflow_tools(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubGateway:
        def describe_methods(self) -> dict[str, object]:
            return {}

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            captured["max_tool_invocations"] = _kwargs.get("max_tool_invocations")

        def _load_workflow_model_policy(self, _preferred_language):
            return object(), {}

        def _select_model_for_stage(self, **kwargs):
            return kwargs.get("default_model")

        def _action_tool_calling_plan(self, request):
            captured["required_prompt_tools"] = request.data.get(
                "required_prompt_tools"
            )
            captured["required_tool_obligation_ledger"] = request.data.get(
                "required_tool_obligation_ledger"
            )
            captured["tool_argument_defaults"] = request.data.get(
                "tool_argument_defaults"
            )
            captured["turn_expected_outcome_contract_state"] = request.data.get(
                "turn_expected_outcome_contract_state"
            )
            captured["workflow_discovery_timeout_seconds"] = request.data.get(
                "workflow_discovery_timeout_seconds"
            )
            return type(
                "_Result",
                (),
                {
                    "status": "success",
                    "outputs": {
                        "tool_calls_present": False,
                        "orchestrator_result": {"response_text": '{"ok": true}'},
                    },
                },
            )()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: {},
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="gemma4:26b",
        ),
        data={
            "required_prompt_tools": ["search_knowledge_base"],
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract.v1",
                "fields": {
                    "summary": "Identify the user and list grounded papers only.",
                },
                "required_tools": [
                    "fetch_concept",
                    "find_relations_with_argument",
                ],
            },
            "workflow_discovery_timeout_seconds": 5.0,
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "tool_mode": "allowed",
            "allowed_tools": [
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "required_tools": [
                "get_predicate_incidence",
                "find_relations_with_argument",
            ],
            "tool_argument_defaults": {
                "get_predicate_incidence": {
                    "argument_index": "subject",
                    "relation_kind": "binary",
                    "limit": 12,
                }
            },
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["required_prompt_tools"] == [
        "get_predicate_incidence",
        "find_relations_with_argument",
    ]
    ledger = captured["required_tool_obligation_ledger"]
    assert isinstance(ledger, dict)
    fetch_obligation = next(
        item for item in ledger["obligations"] if item["tool_name"] == "fetch_concept"
    )
    assert fetch_obligation["allowed_by_workflow_policy"] is False
    assert (
        fetch_obligation["blocking_reason"]
        == "contract_required_tool_not_allowed_by_workflow_policy"
    )
    assert captured["tool_argument_defaults"] == {
        "get_predicate_incidence": {
            "argument_index": "subject",
            "relation_kind": "binary",
            "limit": 12,
        }
    }
    assert captured["max_tool_invocations"] == 4
    assert captured["turn_expected_outcome_contract_state"] == {
        "schema_version": "turn_expected_outcome_contract.v1",
        "fields": {
            "summary": "Identify the user and list grounded papers only.",
        },
        "required_tools": [
            "fetch_concept",
            "find_relations_with_argument",
        ],
    }
    assert captured["workflow_discovery_timeout_seconds"] == 5.0


def test_execute_llm_step_tool_mode_infers_predicate_incidence_from_alias_contract(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubGateway:
        def describe_methods(self) -> dict[str, object]:
            return {
                "search_concepts": {},
                "fetch_concept": {},
                "get_predicate_incidence": {},
            }

    class _StubOrchestrator:
        @staticmethod
        def _infer_turn_contract_required_tools(**_kwargs):
            return (
                "search_concepts",
                "fetch_concept",
                "get_predicate_incidence",
            )

        def __init__(self, **_kwargs):
            captured["max_tool_invocations"] = _kwargs.get("max_tool_invocations")

        def _load_workflow_model_policy(self, _preferred_language):
            return object(), {}

        def _select_model_for_stage(self, **kwargs):
            return kwargs.get("default_model")

        def _action_tool_calling_plan(self, request):
            captured["required_prompt_tools"] = request.data.get(
                "required_prompt_tools"
            )
            return type(
                "_Result",
                (),
                {
                    "status": "success",
                    "outputs": {
                        "tool_calls_present": False,
                        "orchestrator_result": {"response_text": '{"ok": true}'},
                    },
                },
            )()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: {},
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="gemma4:26b",
        ),
        data={
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract.v1",
                "fields": {
                    "summary": "List the key predicates for scientific papers.",
                    "grounding_requirement": (
                        "Every predicate listed must be verified against the "
                        "actual Vontology schema using ontology inspection tools."
                    ),
                },
                "required_tools": [
                    "vontology_concept_search",
                    "fetch_concept",
                ],
            }
        },
        prompt_contract={"prompt_text": "Call tools."},
        llm_policy={
            "tool_mode": "allowed",
            "allowed_tools": [
                "search_concepts",
                "fetch_concept",
                "get_predicate_incidence",
            ],
        },
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["required_prompt_tools"] == [
        "search_concepts",
        "fetch_concept",
        "get_predicate_incidence",
    ]
    assert captured["max_tool_invocations"] == 4


def test_execute_llm_step_reserves_budget_for_kr_mutation_and_readback_contract(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    required_tools = [
        "search_concepts",
        "create_concepts",
        "add_relationship",
        "upsert_singleton_text_relation",
        "fetch_concept",
        "get_text_relations_summary",
    ]

    class _StubGateway:
        def describe_methods(self) -> dict[str, object]:
            return {tool_name: {} for tool_name in required_tools}

    class _StubOrchestrator:
        def __init__(self, **kwargs):
            captured["max_tool_invocations"] = kwargs.get("max_tool_invocations")

        def _load_workflow_model_policy(self, _preferred_language):
            return object(), {}

        def _select_model_for_stage(self, **kwargs):
            return kwargs.get("default_model")

        def _action_tool_calling_plan(self, request):
            captured["required_prompt_tools"] = request.data.get(
                "required_prompt_tools"
            )
            captured["required_tool_obligation_ledger"] = request.data.get(
                "required_tool_obligation_ledger"
            )
            return type(
                "_Result",
                (),
                {
                    "status": "success",
                    "outputs": {
                        "tool_calls_present": False,
                        "orchestrator_result": {"response_text": '{"ok": true}'},
                    },
                },
            )()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: {},
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="gemma4:26b",
        ),
        data={
            "turn_expected_outcome_contract_state": {
                "schema_version": "turn_expected_outcome_contract.v1",
                "fields": {
                    "summary": "Represent reusable Vontology labels and read them back.",
                },
                "required_tools": list(required_tools),
            }
        },
        prompt_contract={"prompt_text": "Call tools."},
        llm_policy={"tool_mode": "allowed", "allowed_tools": list(required_tools)},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["max_tool_invocations"] == len(required_tools) + 2
    assert captured["required_prompt_tools"] == required_tools
    ledger = result.outputs["required_tool_obligation_ledger"]
    assert ledger["unsatisfied_required_tools"] == required_tools
    assert ledger["blocking_failure_codes"] == ["required_tool_not_planned"]


def test_execute_llm_step_skips_completion_report_narration_when_tools_missing(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_ for _ in ()).throw(
            AssertionError("narration should not call the LLM gateway")
        ),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="gemma4:26b",
        ),
        data={
            "completion_report": {
                "missing_prompt_tools": [
                    "vontology_concept_search",
                    "fetch_concept",
                ],
            },
            "aux_llm_calls": [],
        },
        workflow_state_id="narration",
        prompt_contract={"prompt_text": "Narrate the completion report."},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["final_response"] == ""
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["completion_reason"] == "skipped_missing_required_prompt_tools"
    assert envelope["missing_prompt_tools"] == [
        "vontology_concept_search",
        "fetch_concept",
    ]
    assert result.outputs["aux_llm_calls"][-1]["reason"] == (
        "completion_report_missing_required_prompt_tools"
    )


def test_execute_llm_step_emits_phase_transition_for_conversation_turn_stage(
    monkeypatch,
) -> None:
    transitions: list[dict[str, object]] = []

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "emit_phase_transition": (
                lambda phase, *, extra=None: transitions.append(
                    {"phase": phase, "extra": extra}
                )
            )
        },
        workflow_state_id="selector_decision",
        prompt_contract={"prompt_text": "Return JSON only."},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert transitions == [
        {
            "phase": "selector_decision",
            "extra": {
                "result_summary": (
                    "Running the authoritative LLM reasoning step for this turn stage"
                ),
                "workflow_state_id": "selector_decision",
            },
        }
    ]


def test_execute_llm_step_applies_conversation_turn_timeout_override_to_gateway_llm(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["timeout_override_sec"] = kwargs.get("timeout_override_sec")
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )
    monkeypatch.setenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC", "42")

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "selector_context_messages": [
                {"role": "system", "content": "Selector prompt"},
                {"role": "user", "content": "Who am I?"},
            ]
        },
        workflow_state_id="selector_decision",
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "context_messages_context_key": "selector_context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["timeout_override_sec"] == 42.0


def test_execute_llm_step_uses_default_conversation_turn_timeout_when_env_missing(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["timeout_override_sec"] = kwargs.get("timeout_override_sec")
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )
    monkeypatch.delenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC", raising=False)

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "selector_context_messages": [
                {"role": "system", "content": "Selector prompt"},
                {"role": "user", "content": "Who am I?"},
            ]
        },
        workflow_state_id="selector_decision",
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "context_messages_context_key": "selector_context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["timeout_override_sec"] == DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC


def test_execute_llm_step_returns_failed_result_on_gateway_llm_timeout(
    monkeypatch,
) -> None:
    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **_kwargs):
            raise TimeoutError(
                "LLM call timed out after 12s (stage=classifier, model=gemma4:26b)"
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="gemma4:26b",
        ),
        data={
            "selector_context_messages": [
                {"role": "system", "content": "Selector prompt"},
                {"role": "user", "content": "Who am I?"},
            ]
        },
        workflow_state_id="selector_decision",
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "context_messages_context_key": "selector_context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert "workflow_llm_step_timeout:" in str(result.error or "")
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["completion_reason"] == "timeout"
    assert envelope["timeout_stage"] == "llm.action"
    assert "timed out after 12s" in envelope["timeout_detail"]


def test_execute_llm_step_uses_explicit_timeout_override_from_request_data(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["timeout_override_sec"] = kwargs.get("timeout_override_sec")
            return ('{"ok": true}', "test-model", None)

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={
            "tool_plan_context_messages": [
                {"role": "system", "content": "Tool plan prompt"},
                {"role": "user", "content": "Represent this paper."},
            ],
            "conversation_turn_llm_timeout_override_sec": 31,
        },
        workflow_state_id="tool_planning",
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "context_messages_context_key": "tool_plan_context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["timeout_override_sec"] == 31.0
