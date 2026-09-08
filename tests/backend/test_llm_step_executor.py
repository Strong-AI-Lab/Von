from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.backend.integrations.internal_mcp.orchestrator import (
    CancellationRequested,
    InternalMCPChatOrchestrator,
    _WorkflowModelPolicyState,
)
from src.backend.languagemodels.structured_tool_calling import (
    StructuredToolCapabilityRejectedError,
)
from src.backend.services import prompt_template_service as pts
from src.backend.workflows import llm_step_executor as lse
from src.backend.workflows.action_registry import (
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.conversation_turn_llm_timeout import (
    DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC,
)
from src.backend.workflows.llm_step_executor import (
    _compose_llm_prompt,
    _compose_llm_prompt_with_diagnostics,
    compute_llm_context_fields_sha256,
    execute_llm_step,
)
from src.backend.workflows.recovery_prompt_compaction import (
    RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET,
)
from src.backend.workflows import prompt_metadata_resolution as pmr


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


def test_active_only_no_tool_policy_bypasses_gateway_registry_setup(
    monkeypatch,
) -> None:
    llm_client = MagicMock()
    llm_client.generate.return_value = '{"status":"ok"}'
    gateway = MagicMock()
    gateway_runtime_calls = 0

    def fail_if_gateway_runtime_is_loaded(*_args, **_kwargs):
        nonlocal gateway_runtime_calls
        gateway_runtime_calls += 1
        raise AssertionError("active-only no-tool calls must not load gateway registry")

    monkeypatch.setattr(
        lse,
        "_build_gateway_runtime",
        fail_if_gateway_runtime_is_loaded,
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=llm_client,
            model="gpt-5.6-luna",
            gateway=gateway,
        ),
        data={"user_concept_id": "#V#user"},
        workflow_id="#V#bounded_digest_workflow",
        workflow_state_id="#V#bounded_digest_synthesis",
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "policy_stage": "bounded_digest",
            "selection_policy": "active_only",
            "tool_mode": "none",
            "max_output_tokens": 2048,
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert gateway_runtime_calls == 0
    llm_client.generate.assert_called_once()
    assert (
        llm_client.generate.call_args.kwargs["llm_params"]["max_output_tokens"] == 2048
    )
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["selection_policy"] == "active_only"
    assert envelope["selected_model"] == "gpt-5.6-luna"


def test_represented_llm_policy_suppresses_raw_io_sentinel_across_timeout_thread(
    monkeypatch,
    caplog,
) -> None:
    from src.backend.languagemodels import llm_interface

    raw_prompt_sentinel = "RAW-LLM-PROMPT-MUST-NOT-BE-LOGGED"
    raw_response_sentinel = "RAW-LLM-RESPONSE-MUST-NOT-BE-LOGGED"
    raw_io_decisions: list[bool] = []
    worker_thread_names: list[str] = []
    llm_client = MagicMock()

    def generate(prompt, **_kwargs):
        worker_thread_names.append(threading.current_thread().name)
        should_log = llm_interface._should_log_llm_io()
        raw_io_decisions.append(should_log)
        if should_log:
            logging.getLogger(llm_interface.__name__).debug(
                "prompt=%s response=%s",
                prompt,
                raw_response_sentinel,
            )
        return json.dumps({"status": raw_response_sentinel})

    llm_client.generate.side_effect = generate
    monkeypatch.setenv("VON_DEBUG_LLM_IO", "1")
    monkeypatch.setattr(
        lse,
        "record_workflow_llm_step_duration_observation",
        lambda **_kwargs: {},
    )
    caplog.set_level(logging.DEBUG, logger=llm_interface.__name__)
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=llm_client,
            model="gpt-5.6-luna",
        ),
        data={"conversation_turn_llm_timeout_override_sec": 1.0},
        workflow_id="#V#represented_policy_workflow",
        workflow_state_id="#V#represented_policy_step",
        prompt_contract={"prompt_text": raw_prompt_sentinel},
        llm_policy={
            "policy_stage": "represented_policy_step",
            "selection_policy": "active_only",
            "suppress_raw_io_logging": True,
            "tool_mode": "none",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success", result.outputs
    assert raw_io_decisions == [False]
    assert worker_thread_names == ["workflow-llm-step-call-represented_policy_step"]
    assert raw_prompt_sentinel not in caplog.text
    assert raw_response_sentinel not in caplog.text


def test_compose_llm_prompt_includes_declared_context_labels() -> None:
    prompt = _compose_llm_prompt(
        base_prompt="Use the workflow policy.",
        llm_policy={
            "context_fields": [
                {
                    "context_key": "prior_observations",
                    "label": "Prior observations",
                },
                {
                    "context_key": "known_constraints",
                    "label": "Known constraints",
                },
                {
                    "context_key": "open_questions",
                    "label": "Open questions",
                },
            ]
        },
        context={
            "prior_observations": [{"text": "The earlier read was incomplete."}],
            "known_constraints": [{"text": "Do not infer missing evidence."}],
            "open_questions": [{"text": "Which source is authoritative?"}],
        },
    )

    assert "Prior observations" in prompt
    assert "Known constraints" in prompt
    assert "Open questions" in prompt
    assert "The earlier read was incomplete." in prompt
    assert "Do not infer missing evidence." in prompt
    assert "Which source is authoritative?" in prompt


def test_context_field_lineage_is_sensitive_to_value_order_and_label() -> None:
    policy = {
        "context_fields": [
            {"context_key": "first", "label": "First evidence"},
            {"context_key": "second", "label": "Second evidence"},
        ]
    }
    context = {"first": {"value": 1}, "second": "two"}

    _prompt, diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt="Use the represented evidence.",
        llm_policy=policy,
        context=context,
    )
    digest = compute_llm_context_fields_sha256(
        llm_policy=policy,
        context=context,
    )

    assert digest is not None
    assert diagnostics["llm_context_fields_sha256"] == digest
    assert diagnostics["llm_context_fields_rendered_count"] == 2
    assert (
        len(
            {
                digest,
                compute_llm_context_fields_sha256(
                    llm_policy=policy,
                    context={**context, "second": "changed"},
                ),
                compute_llm_context_fields_sha256(
                    llm_policy={
                        "context_fields": list(reversed(policy["context_fields"]))
                    },
                    context=context,
                ),
                compute_llm_context_fields_sha256(
                    llm_policy={
                        "context_fields": [
                            {"context_key": "first", "label": "Renamed evidence"},
                            policy["context_fields"][1],
                        ]
                    },
                    context=context,
                ),
            }
        )
        == 4
    )


def test_context_field_lineage_distinguishes_authored_empty_from_missing() -> None:
    policy = {
        "context_fields": [
            {"context_key": "missing_value", "label": "Missing evidence"}
        ]
    }

    prompt, diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt="Use available evidence.",
        llm_policy=policy,
        context={},
    )

    assert prompt == "Use available evidence."
    assert diagnostics["llm_context_fields_rendered_count"] == 0
    assert diagnostics["llm_context_fields_sha256"] == (
        compute_llm_context_fields_sha256(llm_policy=policy, context={})
    )
    assert len(diagnostics["llm_context_fields_sha256"]) == 64
    assert compute_llm_context_fields_sha256(llm_policy={}, context={}) is None


def test_compose_llm_prompt_compacts_recovery_context_with_diagnostics() -> None:
    huge_description = "candidate detail " * 800
    huge_internal_payload = "internal routing payload " * 1200
    prompt, diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt="Return the recovery JSON.",
        llm_policy={
            "context_fields": [
                {
                    "context_key": "selected_workflow_trace",
                    "label": "Selected Workflow Outcome",
                },
                {
                    "context_key": "workflow_discovery_result",
                    "label": "Workflow Discovery Result",
                },
                {
                    "context_key": "completion_gate_evidence_payload",
                    "label": "Completion Gate Evidence",
                },
                {
                    "context_key": "completion_gate_loop_attempts",
                    "label": "Recovery Loop Attempts",
                },
                {
                    "context_key": "extra_payload",
                    "label": "Extra Payload",
                },
            ]
        },
        context={
            "selected_workflow_trace": {
                "workflow_id": "#V#tool_calling_workflow",
                "failure_detail": "gmail_list_messages failed: invalid_grant",
                "workflow_discovery_result": {
                    "duplicated_bulk": huge_internal_payload,
                },
                "stage_timings": {"prefill": huge_internal_payload},
            },
            "workflow_discovery_result": {
                "candidates": [
                    {
                        "workflow_id": f"#V#candidate_{index}",
                        "description": huge_description,
                        "routing_index_metadata": {"bulk": huge_internal_payload},
                    }
                    for index in range(12)
                ]
            },
            "completion_gate_evidence_payload": {
                "blocking_failure_codes": ["invalid_grant"],
                "unresolved_preconditions": [
                    {
                        "status_reason": (
                            "gmail_list_messages returned invalid_grant for the "
                            "authenticated Gmail profile"
                        ),
                    }
                ],
                "long_internal_note": huge_internal_payload,
            },
            "completion_gate_loop_attempts": 4,
            "extra_payload": {
                f"payload_{index}": huge_internal_payload for index in range(12)
            },
        },
        workflow_state_id="recovery_decision",
    )

    assert len(prompt) <= RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET + 200
    assert "gmail_list_messages failed: invalid_grant" in prompt
    assert "Recovery Loop Attempts:\n4" in prompt
    assert "duplicated_bulk" not in prompt
    assert '"stage_timings"' not in prompt

    recovery = diagnostics["recovery_context_compaction"]
    assert recovery["enabled"] is True
    assert recovery["rendered_context_chars"] <= RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET
    assert recovery["rendered_context_field_count"] >= 4
    field_diagnostics = {field["context_key"]: field for field in recovery["fields"]}
    assert (
        field_diagnostics["selected_workflow_trace"]["dropped_keys"][
            "workflow_discovery_result"
        ]
        == 1
    )
    assert (
        field_diagnostics["workflow_discovery_result"]["truncated_list_omitted_items"]
        == 6
    )
    assert field_diagnostics["extra_payload"]["body_truncated_by_char_budget"] is True
    assert len(diagnostics["llm_context_fields_sha256"]) == 64
    assert diagnostics["llm_context_fields_rendered_count"] >= 4


def test_compose_llm_prompt_leaves_non_recovery_context_uncompacted() -> None:
    long_value = "full context " * 700
    prompt, diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt="Use the selector policy.",
        llm_policy={
            "context_fields": [
                {"context_key": "workflow_discovery_result", "label": "Discovery"}
            ]
        },
        context={
            "workflow_discovery_result": {
                "workflow_discovery_result": {"nested": long_value},
                "description": long_value,
            }
        },
        workflow_state_id="selector_decision",
    )

    assert long_value in prompt
    assert diagnostics["llm_context_fields_rendered_count"] == 1
    assert len(diagnostics["llm_context_fields_sha256"]) == 64


def test_context_field_lineage_is_sensitive_to_recovery_compaction() -> None:
    policy = {
        "context_fields": [
            {"context_key": "selected_workflow_trace", "label": "Trace evidence"}
        ]
    }
    context = {
        "selected_workflow_trace": {
            "workflow_id": "#V#represented_workflow",
            "failure_detail": "typed blocker",
            "stage_timings": {"discarded_bulk": "x" * 10_000},
        }
    }

    uncompacted = compute_llm_context_fields_sha256(
        llm_policy=policy,
        context=context,
        workflow_state_id="selector_decision",
    )
    compacted = compute_llm_context_fields_sha256(
        llm_policy=policy,
        context=context,
        workflow_state_id="recovery_decision",
    )
    _prompt, diagnostics = _compose_llm_prompt_with_diagnostics(
        base_prompt="Use represented recovery evidence.",
        llm_policy=policy,
        context=context,
        workflow_state_id="recovery_decision",
    )

    assert uncompacted is not None
    assert compacted is not None
    assert compacted != uncompacted
    assert diagnostics["llm_context_fields_sha256"] == compacted


def test_context_field_lineage_includes_model_visible_recovery_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = {
        "context_fields": [
            {"context_key": f"field_{index}", "label": "Evidence"}
            for index in range(31)
        ]
        + [{"context_key": "last", "label": "Final evidence " * 100}]
    }
    context = {f"field_{index}": "bulk evidence " * 2_000 for index in range(31)}
    context["last"] = "omitted"

    monkeypatch.setattr(lse, "TOTAL_TRUNCATION_MARKER", "[marker one]")
    first = compute_llm_context_fields_sha256(
        llm_policy=policy,
        context=context,
        workflow_state_id="recovery_decision",
    )
    monkeypatch.setattr(lse, "TOTAL_TRUNCATION_MARKER", "[marker two]")
    second = compute_llm_context_fields_sha256(
        llm_policy=policy,
        context=context,
        workflow_state_id="recovery_decision",
    )

    assert first is not None
    assert second is not None
    assert first != second


def test_runtime_tool_ids_are_hydrated_only_for_authored_context_field() -> None:
    gateway = MagicMock()
    gateway.describe_methods.return_value = {
        "find_relations_with_argument": {},
        "get_predicate_incidence": {},
    }
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), gateway=gateway),
        data={"available_internal_tool_ids": {"status": "stale", "tool_ids": []}},
    )

    diagnostics = lse._hydrate_authored_runtime_context_fields(
        request=request,
        llm_policy={
            "context_fields": [
                {
                    "context_key": "available_internal_tool_ids",
                    "label": "Available internal tool IDs",
                }
            ]
        },
    )

    assert request.data["available_internal_tool_ids"] == {
        "status": "available",
        "tool_ids": [
            "find_relations_with_argument",
            "get_predicate_incidence",
        ],
    }
    assert diagnostics == {
        "context_key": "available_internal_tool_ids",
        "source": "workflow_environment.gateway.describe_methods",
        "status": "available",
        "tool_count": 2,
    }

    untouched_request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), gateway=gateway),
        data={},
    )
    assert (
        lse._hydrate_authored_runtime_context_fields(
            request=untouched_request,
            llm_policy={"context_fields": [{"context_key": "user_prompt"}]},
        )
        is None
    )
    assert "available_internal_tool_ids" not in untouched_request.data


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


def test_internal_decision_step_can_suppress_raw_response_projection() -> None:
    llm_client = MagicMock()
    llm_client.generate.return_value = '{"decision":"retry"}'
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={},
        prompt_contract={"prompt_text": "Return decision JSON only."},
        llm_policy={"project_raw_response_to_final_response": False},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {"decision": "retry"}
    assert result.outputs["llm_step_response"] == '{"decision":"retry"}'
    assert "final_response" not in result.outputs
    assert (
        result.outputs["llm_step_envelope"]["raw_response_projected_to_final_response"]
        is False
    )


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


def test_execute_llm_step_recovers_json_value_with_raw_newlines_in_string() -> None:
    llm_client = MagicMock()
    llm_client.generate.return_value = (
        '{"response_text":"Here are the messages:\n\n'
        "1. From: A, Subject: One\n"
        '   Summary: First summary."}'
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={},
        prompt_contract={"prompt_text": "Return response JSON only."},
        validation_policy={
            "output_format": "json_value",
            "required_json_fields": ["response_text"],
        },
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"] == {
        "response_text": (
            "Here are the messages:\n\n"
            "1. From: A, Subject: One\n"
            "   Summary: First summary."
        )
    }
    assert result.outputs["validated_json_parse_mode"] == "relaxed_json_control_chars"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "success"


def test_execute_llm_step_records_recovery_prompt_compaction_diagnostics() -> None:
    llm_client = MagicMock()
    llm_client.generate.return_value = (
        '{"turn_next_action":{"action_type":"respond_with_follow_up",'
        '"target_workflow_id":null,'
        '"response_text":"Gmail retrieval failed; please reconnect Gmail.",'
        '"tool_calls":null},'
        '"reasoning":"The Gmail tool failed with invalid_grant."}'
    )
    bulky_trace = {
        "workflow_id": "#V#tool_calling_workflow",
        "failure_detail": "gmail_list_messages failed: invalid_grant",
        "workflow_discovery_result": {"bulk": "duplicated discovery " * 2000},
        "stage_timings": {"prefill": "timing detail " * 2000},
    }
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client, model="qwen3:8b"),
        data={
            "selected_workflow_trace": bulky_trace,
            "completion_gate_evidence_payload": {
                "blocking_failure_codes": ["invalid_grant"],
                "unresolved_preconditions": [
                    {"status_reason": "gmail_list_messages failed: invalid_grant"}
                ],
            },
            "completion_gate_loop_attempts": 4,
        },
        prompt_contract={"prompt_text": "Return recovery JSON only."},
        llm_policy={
            "context_fields": [
                {
                    "context_key": "selected_workflow_trace",
                    "label": "Selected Workflow Outcome",
                },
                {
                    "context_key": "completion_gate_evidence_payload",
                    "label": "Completion Gate Evidence",
                },
                {
                    "context_key": "completion_gate_loop_attempts",
                    "label": "Recovery Loop Attempts",
                },
            ]
        },
        validation_policy={"output_format": "json_value"},
        workflow_state_id="recovery_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    rendered_prompt = llm_client.generate.call_args.args[0]
    assert "gmail_list_messages failed: invalid_grant" in rendered_prompt
    assert "duplicated discovery" not in rendered_prompt
    diagnostics = result.outputs["prompt_context_diagnostics"]
    recovery = diagnostics["recovery_context_compaction"]
    assert recovery["enabled"] is True
    assert recovery["workflow_state_id"] == "recovery_decision"
    assert recovery["rendered_context_chars"] <= RECOVERY_CONTEXT_TOTAL_CHAR_BUDGET
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["prompt_context_diagnostics"] == diagnostics
    assert result.outputs["llm_calls"][0]["prompt_context_diagnostics"] == diagnostics


def test_execute_llm_step_records_duration_baseline_for_direct_workflow_step(
    monkeypatch,
) -> None:
    recorded_calls: list[dict[str, object]] = []

    def fake_record_duration(**kwargs):
        recorded_calls.append(dict(kwargs))
        return {
            "historical_observation_count": 3,
            "historical_mean_duration_ms": 250.0,
            "historical_stddev_duration_ms": 50.0,
            "duration_deviation_classification": "within_usual_range",
        }

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor."
        "record_workflow_llm_step_duration_observation",
        fake_record_duration,
    )
    llm_client = MagicMock()
    llm_client.generate.return_value = "Plain response"
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client, model="gpt-test"),
        data={"request_id": "req-duration-baseline"},
        prompt_contract={"prompt_text": "Answer plainly."},
        workflow_id="#V#workflow",
        workflow_state_id="#V#step",
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert recorded_calls
    assert recorded_calls[0]["workflow_id"] == "#V#workflow"
    assert recorded_calls[0]["workflow_state_id"] == "#V#step"
    assert recorded_calls[0]["workflow_stage_id"] == "#V#step"
    assert recorded_calls[0]["model_name"] == "gpt-test"
    assert recorded_calls[0]["request_id"] == "req-duration-baseline"
    llm_entry = result.outputs["llm_calls"][0]
    assert llm_entry["historical_observation_count"] == 3
    assert llm_entry["historical_mean_duration_ms"] == 250.0


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
        '"required_tools":["search_concepts","get_predicate_extent"],'
        '"conditional_required_tools":["workflow_execute"],'
        '"target_workflow_id":"#V#predicate_schema_lookup_workflow"}'
        "\n```"
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert result.outputs["validated_json"]["grounding_requirement"] == (
        "Use retrieved ontology evidence."
    )
    assert result.outputs["validated_json"]["required_tools"] == [
        "search_concepts",
        "get_predicate_extent",
    ]
    assert result.outputs["validated_json"]["conditional_required_tools"] == [
        "workflow_execute"
    ]
    assert result.outputs["validated_json"]["workflow_concept_ids"] == [
        "#V#predicate_schema_lookup_workflow"
    ]


def test_execute_llm_step_does_not_activate_conditional_tool_from_domain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Gateway:
        enabled = True

        def describe_methods(self) -> dict[str, Any]:
            return {
                "gmail_list_profiles": {},
                "gmail_list_messages": {},
                "gmail_get_message": {},
                "workflow_execute": {},
            }

    def _fake_plan(
        _self: InternalMCPChatOrchestrator,
        request: WorkflowActionRequest,
    ) -> WorkflowActionResult:
        request.data["invocations"].extend(
            [
                {
                    "tool": "gmail_list_profiles",
                    "status": "ok",
                    "success": True,
                    "result_summary": "1 profiles",
                },
                {
                    "tool": "gmail_list_messages",
                    "status": "ok",
                    "success": True,
                    "result_summary": "Found 10 messages",
                },
                {
                    "tool": "gmail_get_message",
                    "status": "ok",
                    "success": True,
                    "result_summary": "Message: https://arxiv.org/abs/2509.14786",
                },
            ]
        )
        return WorkflowActionResult(
            outputs={
                "tool_calls_present": False,
                "current_response": "Ready to represent arXiv:2509.14786.",
                "final_response": "Ready to represent arXiv:2509.14786.",
            }
        )

    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_action_tool_calling_plan",
        _fake_plan,
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_Gateway(),
            model="test-model",
        ),
        data={
            "policy_state": _WorkflowModelPolicyState(
                enabled=False,
                policy=None,
                policy_id=None,
                predicate_id=None,
                errors=(),
            ),
            "registry_snapshot": {"models": []},
            "turn_expected_outcome_contract_state": {
                "required_tools": [
                    "gmail_list_profiles",
                    "gmail_list_messages",
                    "gmail_get_message",
                ],
                "conditional_required_tools": ["workflow_execute"],
                "workflow_concept_ids": ["#V#arxiv_paper_representation_workflow"],
            },
            "invocations": [],
        },
        prompt_contract={"prompt_text": "Use tools."},
        llm_policy={
            "tool_mode": "allowed",
            "allowed_tools": [
                "gmail_list_profiles",
                "gmail_list_messages",
                "gmail_get_message",
                "workflow_execute",
            ],
        },
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert "workflow_execute" not in result.outputs["required_prompt_tools"]
    ledger = result.outputs["required_tool_obligation_ledger"]
    assert "workflow_execute" not in ledger["unsatisfied_required_tools"]
    assert "workflow_execute" not in result.outputs["missing_prompt_tools"]


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
        "get_predicates_for_class"
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


def test_gateway_llm_step_reports_validation_failure_after_fallback_exhaustion(
    monkeypatch,
) -> None:
    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            response_validator = kwargs.get("response_validator")
            assert callable(response_validator)
            validation_failure = response_validator(
                '{"summary":"missing the required mode"}',
                "qwen3:8b",
                {"provider": "ollama", "model": "qwen3:8b"},
            )
            assert validation_failure is not None
            assert validation_failure["reason"] == "json_required_fields_missing"
            raise RuntimeError(
                "all_model_candidates_failed:stage=context_adjudication:"
                "ollama:qwen3:8b=response_validation_failed"
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), {}, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="qwen3:8b",
        ),
        data={},
        prompt_contract={"prompt_text": "Return context adjudication JSON."},
        llm_policy={"policy_stage": "context_adjudication"},
        validation_policy={
            "output_format": "json_value",
            "required_json_fields": ["mode"],
        },
        workflow_state_id="context_adjudication_decision",
    )

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert result.error == "json_required_fields_missing"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["missing_required_fields"] == ["mode"]
    assert envelope["validation_fallback_exhausted"] is True
    assert "response_validation_failed" in envelope["validation_fallback_error"]


def test_gateway_llm_step_does_not_mask_mixed_fallback_failures(
    monkeypatch,
) -> None:
    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            response_validator = kwargs.get("response_validator")
            assert callable(response_validator)
            validation_failure = response_validator(
                '{"summary":"missing the required mode"}',
                "qwen3:8b",
                {"provider": "ollama", "model": "qwen3:8b"},
            )
            assert validation_failure is not None
            raise RuntimeError(
                "all_model_candidates_failed:stage=context_adjudication:"
                "ollama:qwen3:8b=response_validation_failed,"
                "ollama:granite3.3:2b=provider_unreachable"
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), {}, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="qwen3:8b",
        ),
        data={},
        prompt_contract={"prompt_text": "Return context adjudication JSON."},
        llm_policy={"policy_stage": "context_adjudication"},
        validation_policy={
            "output_format": "json_value",
            "required_json_fields": ["mode"],
        },
        workflow_state_id="context_adjudication_decision",
    )

    with pytest.raises(RuntimeError, match="provider_unreachable"):
        execute_llm_step(request)


def test_execute_llm_step_fails_closed_when_json_value_is_invalid() -> None:
    request = _build_request(llm_response="This is not valid JSON.")

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert "json_parse_failed" in str(result.error or "")
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "failed"
    assert "json_parse_failed" in str(envelope["validation"]["reason"] or "")


def test_execute_llm_step_falls_back_to_defaults_when_json_unparseable_and_defaults_exist() -> (
    None
):
    """When the model returns unparseable prose but all required fields have defaults,
    the step should succeed using those defaults rather than failing the turn."""
    llm_client = MagicMock()
    llm_client.generate.return_value = "Sure, I can help with that!"  # not JSON
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=llm_client),
        data={},
        prompt_contract={"prompt_text": "Return the expected-outcome JSON."},
        validation_policy={
            "output_format": "json_value",
            "json_field_defaults": {
                "expected_outcome_summary": "Answer the user's request accurately.",
                "grounding_requirement": "Use available context.",
                "required_tools": [],
            },
            "required_json_fields": [
                "expected_outcome_summary",
                "grounding_requirement",
                "required_tools",
            ],
        },
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    validated = result.outputs["validated_json"]
    assert (
        validated["expected_outcome_summary"] == "Answer the user's request accurately."
    )
    assert validated["grounding_requirement"] == "Use available context."
    assert validated["required_tools"] == []
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validation"]["status"] == "success"
    assert envelope["validation"].get("json_object_defaulted_from_non_object") is True


def test_execute_llm_step_uses_model_family_prompt_variant(monkeypatch) -> None:
    texts = {
        "#V#tool_prompt": [
            {"predicate": "#V#hasContent", "text": "Base tool prompt for {task}"},
        ],
        "#V#ollama_tool_prompt": [
            {
                "predicate": "#V#hasContent",
                "text": "Ollama tool prompt for {task}",
            },
            {"predicate": "#V#forModelFamily", "text": "ollama"},
        ],
    }
    docs = {
        "#V#tool_prompt": {
            "concept_id": "#V#tool_prompt",
            "relationships": {
                "#V#hasModelPromptVariant": ["#V#ollama_tool_prompt"],
            },
        },
        "#V#ollama_tool_prompt": {
            "concept_id": "#V#ollama_tool_prompt",
            "relationships": {},
        },
    }

    def _fake_find_one(query, projection=None):
        concept_id = query.get("concept_id") if isinstance(query, dict) else None
        if concept_id not in docs:
            return None
        if projection == {"_id": 1}:
            return {"_id": concept_id}
        return docs[concept_id]

    monkeypatch.setattr(pmr.ConceptsRepository, "find_one", _fake_find_one)
    monkeypatch.setattr(
        pmr,
        "get_texts_for_concept",
        lambda concept_id, *args, **kwargs: texts.get(concept_id, []),
    )
    monkeypatch.setattr(
        pts,
        "get_texts_for_concept",
        lambda concept_id, *args, **kwargs: texts.get(concept_id, []),
    )
    llm_client = MagicMock()
    llm_client.generate.return_value = '{"ok": true}'

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=llm_client,
            model="local-reasoner:7b",
        ),
        data={"task": "workflow tools", "requested_client_type": "ollama"},
        prompt_contract={"requested_prompt_concept_ids": ["#V#tool_prompt"]},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    llm_client.generate.assert_called_once()
    assert (
        llm_client.generate.call_args.args[0] == "Ollama tool prompt for workflow tools"
    )
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["base_prompt_id"] == "#V#tool_prompt"
    assert envelope["selected_prompt_id"] == "#V#ollama_tool_prompt"
    assert envelope["selected_prompt_source"] == "prompt_contract:model_variant"
    assert envelope["prompt_variant_selection"]["match_reason"] == "model_family"
    assert envelope["prompt_variant_selection"]["fallback_reason"] is None


def test_prompt_variant_model_context_uses_explicit_request_without_gateway_setup(
    monkeypatch,
) -> None:
    def _unexpected_gateway_setup(_request):
        raise AssertionError("explicit requested model should avoid gateway setup")

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        _unexpected_gateway_setup,
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="gpt-4.1-mini",
        ),
        data={"requested_model": "gpt-4.1-mini", "requested_client_type": "openai"},
    )

    selected_model, selected_candidate, registry_snapshot, diagnostics = (
        lse._select_model_context_for_prompt_variant(
            request=request,
            stage="context_adjudication",
        )
    )

    assert selected_model == "gpt-4.1-mini"
    assert selected_candidate == {"provider": "openai"}
    assert registry_snapshot is None
    assert diagnostics["source"] == "explicit_request_model"


def test_prompt_variant_model_context_infers_provider_from_environment_client(
    monkeypatch,
) -> None:
    class OpenAIClient:
        pass

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda _request: (_ for _ in ()).throw(
            AssertionError("explicit requested model should avoid gateway setup")
        ),
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=OpenAIClient(),
            gateway=object(),
            model="gpt-4.1-mini",
        ),
        data={"requested_model": "gpt-4.1-mini"},
    )

    selected_model, selected_candidate, registry_snapshot, diagnostics = (
        lse._select_model_context_for_prompt_variant(
            request=request,
            stage="context_adjudication",
        )
    )

    assert selected_model == "gpt-4.1-mini"
    assert selected_candidate == {"provider": "openai"}
    assert registry_snapshot is None
    assert diagnostics["source"] == "explicit_request_model"


def test_context_messages_policy_budget_preserves_tail_with_diagnostics() -> None:
    messages, diagnostics = lse._bounded_context_messages_for_policy(
        raw_value=[
            {"role": "user", "content": "older context " * 20},
            {"role": "assistant", "content": "middle context " * 20},
            {"role": "user", "content": "Tell me about JVNAUTOSCI-150 in JIRA"},
        ],
        llm_policy_map={
            "context_messages_max_chars": 80,
            "context_messages_truncation_strategy": "tail",
        },
        context_messages_key="conversation_context",
    )

    assert messages
    assert messages[-1]["content"] == "Tell me about JVNAUTOSCI-150 in JIRA"
    assert diagnostics is not None
    assert diagnostics["context_messages_context_key"] == "conversation_context"
    assert diagnostics["truncated"] is True
    assert diagnostics["raw_message_count"] == 3
    assert diagnostics["rendered_chars"] <= 80
    assert diagnostics["omitted_message_count"] >= 1


def test_conversation_turn_fallback_guard_preserves_small_test_budgets() -> None:
    assert lse._conversation_turn_llm_fallback_guard_timeout_sec(None) is None
    assert lse._conversation_turn_llm_fallback_guard_timeout_sec(1.0) == 2.0
    assert lse._conversation_turn_llm_fallback_guard_timeout_sec(120.0) == 210.0
    assert lse._conversation_turn_llm_step_guard_timeout_sec(None) is None
    assert lse._conversation_turn_llm_step_guard_timeout_sec(1.0) == 2.0
    assert lse._conversation_turn_llm_step_guard_timeout_sec(120.0) == 300.0


def test_execute_llm_step_passes_context_lineage_to_gateway_llm(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["context"] = kwargs.get("context")
            captured["context_telemetry"] = kwargs.get("context_telemetry")
            captured["prefer_default_model"] = kwargs.get("prefer_default_model")
            captured["check_cancellation"] = kwargs.get("check_cancellation")
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
    assert captured["check_cancellation"] is None


def test_execute_llm_step_preserves_gateway_llm_call_metadata(monkeypatch) -> None:
    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            record_llm_call = kwargs.get("record_llm_call")
            assert callable(record_llm_call)
            record_llm_call(
                call_type="llm_call",
                model_name="selected-model",
                requested_model_name="requested-model",
                effective_model_name="provider-model",
                model_identity_source="provider_response",
                duration_ms=12.0,
                stage="selector",
                provider="openai",
                call_id="llm-test:attempt:1",
            )
            return ('{"ok": true}', "selected-model", None)

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
            model="requested-model",
        ),
        data={},
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"stage": "selector"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    llm_call = result.outputs["llm_calls"][0]
    assert llm_call["call_id"] == "llm-test:attempt:1"
    assert llm_call["requested_model"] == "requested-model"
    assert llm_call["selected_model"] == "selected-model"
    assert llm_call["effective_model"] == "provider-model"
    assert llm_call["model_identity_source"] == "provider_response"
    assert (
        result.outputs["llm_step_envelope"]["llm_calls"][0]["call_id"]
        == "llm-test:attempt:1"
    )
    assert (
        result.outputs["llm_step_envelope"]["llm_calls"] == result.outputs["llm_calls"]
    )


def test_execute_llm_step_passes_cancellation_to_gateway_llm(
    monkeypatch,
) -> None:
    def check_cancellation() -> None:
        raise CancellationRequested(task_id="task-cancelled")

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            check = kwargs.get("check_cancellation")
            assert callable(check)
            check()
            raise AssertionError("cancellation check should raise")

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), {}, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
        ),
        data={"check_cancellation": check_cancellation},
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={},
        validation_policy={"output_format": "json_value"},
    )

    with pytest.raises(CancellationRequested):
        execute_llm_step(request)


def test_gateway_runtime_reuses_parent_policy_and_registry_snapshot(
    monkeypatch,
) -> None:
    policy_state = object()
    registry_snapshot = {"source": "parent_turn_snapshot", "models": []}

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            self._turn_model_failures_local = threading.local()

        def _load_workflow_model_policy(self, _preferred_language):
            raise AssertionError("parent policy_state should be reused")

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        lambda: (_ for _ in ()).throw(
            AssertionError("parent registry_snapshot should be reused")
        ),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), gateway=object()),
        data={
            "policy_state": policy_state,
            "registry_snapshot": registry_snapshot,
            "turn_model_failures": {},
        },
        prompt_contract={"prompt_text": "Return JSON only."},
    )

    orchestrator, resolved_policy, resolved_registry, _, _ = lse._build_gateway_runtime(
        request
    )

    assert isinstance(orchestrator, _StubOrchestrator)
    assert resolved_policy is policy_state
    assert resolved_registry is registry_snapshot


def test_gateway_runtime_refreshes_inherited_policy_timeout(monkeypatch) -> None:
    inherited_policy = _WorkflowModelPolicyState(
        enabled=True,
        policy=None,
        policy_id=None,
        predicate_id=None,
        errors=("workflow_model_policy_timeout",),
    )
    refreshed_policy = _WorkflowModelPolicyState(
        enabled=True,
        policy={"stages": {"context_adjudication": {"primary": "active_llm"}}},
        policy_id="#V#default_workflow_model_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )
    registry_snapshot = {"source": "parent_turn_snapshot", "models": []}

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            self._turn_model_failures_local = threading.local()

        def _load_workflow_model_policy(self, _preferred_language):
            return (
                refreshed_policy,
                {
                    "type": "workflow_model_policy",
                    "loaded": True,
                    "policy_source": "graph",
                },
            )

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), gateway=object()),
        data={
            "policy_state": inherited_policy,
            "registry_snapshot": registry_snapshot,
        },
        prompt_contract={"prompt_text": "Return JSON only."},
    )

    _orchestrator, resolved_policy, resolved_registry, _, _ = (
        lse._build_gateway_runtime(request)
    )

    assert resolved_policy is refreshed_policy
    assert resolved_registry is registry_snapshot
    diagnostics = request.data["aux_llm_calls"]
    assert any(
        entry.get("type") == "workflow_model_policy"
        and entry.get("loaded") is True
        and entry.get("refresh_reason") == "inherited_policy_timeout"
        for entry in diagnostics
    )


def test_gateway_runtime_retains_policy_loaded_after_advisory(monkeypatch) -> None:
    registry_snapshot = {"source": "parent_turn_snapshot", "models": []}
    late_policy = _WorkflowModelPolicyState(
        enabled=True,
        policy={"stages": {}},
        policy_id="#V#late_policy",
        predicate_id="#V#has_model_policy_json",
        errors=(),
    )

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            self._turn_model_failures_local = threading.local()

        def _load_workflow_model_policy(self, _preferred_language):
            time.sleep(0.03)
            return late_policy, {"type": "workflow_model_policy", "loaded": True}

    monkeypatch.setenv("VON_WORKFLOW_MODEL_POLICY_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), gateway=object()),
        data={"registry_snapshot": registry_snapshot},
        prompt_contract={"prompt_text": "Return JSON only."},
    )

    _orchestrator, resolved_policy, resolved_registry, _, _ = (
        lse._build_gateway_runtime(request)
    )

    assert resolved_policy is late_policy
    assert resolved_registry is registry_snapshot
    diagnostics = request.data["aux_llm_calls"]
    assert any(
        entry.get("type") == "workflow_llm_step_setup"
        and entry.get("step_id") == "workflow_model_policy"
        and entry.get("status") == "advisory_exceeded_continuing"
        and entry.get("hard_timeout_seconds") is None
        for entry in diagnostics
    )
    assert any(
        entry.get("type") == "workflow_model_policy"
        and entry.get("loaded") is True
        for entry in diagnostics
    )


def test_gateway_runtime_retains_registry_snapshot_loaded_after_advisory(
    monkeypatch,
) -> None:
    policy_state = object()

    class _StubOrchestrator:
        def __init__(self, **_kwargs):
            self._turn_model_failures_local = threading.local()

        def _load_workflow_model_policy(self, _preferred_language):
            raise AssertionError("parent policy_state should be reused")

    def _blocking_registry_snapshot():
        time.sleep(0.03)
        return {"source": "late_registry", "models": []}

    monkeypatch.setenv("VON_MODEL_REGISTRY_SNAPSHOT_TIMEOUT_SECONDS", "0.01")
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.orchestrator.InternalMCPChatOrchestrator",
        _StubOrchestrator,
    )
    monkeypatch.setattr(
        "src.backend.services.model_registry_service.get_model_registry_snapshot",
        _blocking_registry_snapshot,
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), gateway=object()),
        data={"policy_state": policy_state},
        prompt_contract={"prompt_text": "Return JSON only."},
    )

    _orchestrator, resolved_policy, resolved_registry, _, _ = (
        lse._build_gateway_runtime(request)
    )

    assert resolved_policy is policy_state
    assert resolved_registry["source"] == "late_registry"
    diagnostics = request.data["aux_llm_calls"]
    assert any(
        entry.get("type") == "workflow_llm_step_setup"
        and entry.get("step_id") == "model_registry_snapshot"
        and entry.get("status") == "advisory_exceeded_continuing"
        and entry.get("hard_timeout_seconds") is None
        for entry in diagnostics
    )


def test_execute_llm_step_honours_explicit_prefer_default_model_flag(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
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
            "prefer_default_model": True,
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
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
            return "selected-model"

        def _action_tool_calling_plan(self, request):
            captured["workflow_context"] = (request.workflow_id, request.workflow_state_id, request.workflow_state_metadata)
            captured["shared_prefer_default_model"] = request.data.get(
                "prefer_default_model"
            )
            captured["workflow_step_output_contract"] = request.data.get(
                "workflow_step_output_contract"
            )
            request.data["record_llm_call"](
                call_type="llm.generate_with_tools",
                model_name="selected-model",
                requested_model_name="requested-model",
                effective_model_name="provider-model",
                model_identity_source="provider_response",
                duration_ms=12.0,
                stage="tool_call",
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
        workflow_id="#V#slides", workflow_state_id="interpret",
        workflow_state_metadata={"mutation_authority": "low"},
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="requested-model",
        ),
        data={
            "requested_model": "requested-model",
            "requested_client_type": "ollama",
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"tool_mode": "allowed"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["workflow_context"] == ("#V#slides", "interpret", {"mutation_authority": "low"})
    assert captured["shared_prefer_default_model"] is True
    assert captured["prefer_default_model"] is True
    llm_call = result.outputs["llm_calls"][0]
    assert llm_call["requested_model"] == "requested-model"
    assert llm_call["selected_model"] == "selected-model"
    assert llm_call["effective_model"] == "provider-model"
    assert llm_call["model_identity_source"] == "provider_response"
    assert (
        result.outputs["llm_step_envelope"]["llm_calls"] == result.outputs["llm_calls"]
    )
    assert captured["workflow_step_output_contract"] == {
        "schema_version": "workflow_step_output_contract.v1",
        "output_format": "json_value",
        "prompt_concept_id": None,
        "workflow_id": "#V#slides",
        "workflow_state_id": "interpret",
        "response_contract_text": None,
        "required_json_fields": [],
        "json_field_defaults": {},
    }


def test_llm_step_authored_tool_limit_is_bounded_by_environment_ceiling() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
            max_tool_invocations=300,
        ),
        data={},
    )

    resolved = lse._resolve_llm_step_max_tool_invocations(
        request=request,
        llm_policy={"max_tool_invocations": 5},
        required_prompt_tools=("fetch_concept",),
    )

    assert resolved == 5


def test_llm_step_environment_tool_limit_can_tighten_authored_limit() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
            max_tool_invocations=3,
        ),
        data={},
    )

    resolved = lse._resolve_llm_step_max_tool_invocations(
        request=request,
        llm_policy={"max_tool_invocations": 5},
        required_prompt_tools=("fetch_concept",),
    )

    assert resolved == 3


def test_llm_step_environment_zero_disables_authored_tool_limit() -> None:
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="test-model",
            max_tool_invocations=0,
        ),
        data={},
    )

    resolved = lse._resolve_llm_step_max_tool_invocations(
        request=request,
        llm_policy={"max_tool_invocations": 5},
        required_prompt_tools=("fetch_concept",),
    )

    assert resolved == 0


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


def test_execute_llm_step_uses_default_timeout_for_tool_planning_when_env_missing(
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
            "tool_plan_context_messages": [
                {"role": "system", "content": "Tool plan prompt"},
                {"role": "user", "content": "Find current evidence."},
            ]
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
    assert captured["timeout_override_sec"] == DEFAULT_CONVERSATION_TURN_LLM_TIMEOUT_SEC


def test_gateway_llm_step_does_not_inherit_model_parameters_for_explicit_model(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["default_model_parameters"] = kwargs.get(
                "default_model_parameters"
            )
            captured["emit_progress_callable"] = callable(kwargs.get("emit_progress"))
            return ('{"ok": true}', "gpt-4.1-mini", {"provider": "openai"})

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
            model="gpt-4.1-mini",
            model_parameters={"reasoning_effort": "medium"},
        ),
        data={
            "requested_model": "gpt-4.1-mini",
            "context_messages": [{"role": "user", "content": "Hello"}],
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"context_messages_context_key": "context_messages"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["default_model_parameters"] is None
    assert captured["emit_progress_callable"] is True


def test_gateway_llm_step_uses_explicit_requested_model_parameters(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            captured["default_model_parameters"] = kwargs.get(
                "default_model_parameters"
            )
            return ('{"ok": true}', "gpt-4.1-mini", {"provider": "openai"})

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
            model="gpt-4.1-mini",
            model_parameters={"reasoning_effort": "high"},
        ),
        data={
            "requested_model": "gpt-4.1-mini",
            "requested_model_parameters": {"reasoning_effort": "low"},
            "context_messages": [{"role": "user", "content": "Hello"}],
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"context_messages_context_key": "context_messages"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["default_model_parameters"] == {"reasoning_effort": "low"}


def test_gateway_llm_step_progress_forwarding_does_not_block_llm_call(
    monkeypatch,
) -> None:
    progress_entered = threading.Event()
    release_progress = threading.Event()

    class _StubOrchestrator:
        def _run_llm_with_fallbacks(self, **kwargs):
            emit_progress = kwargs.get("emit_progress")
            assert callable(emit_progress)
            emit_progress({"status": "heartbeat", "stage": "context_adjudication"})
            return ('{"ok": true}', "gpt-4.1-mini", {"provider": "openai"})

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), None, None, None),
    )

    def _blocking_progress(_payload):
        progress_entered.set()
        release_progress.wait(2.0)

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="gpt-4.1-mini",
        ),
        data={
            "emit_progress": _blocking_progress,
            "context_messages": [{"role": "user", "content": "Hello"}],
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"context_messages_context_key": "context_messages"},
        validation_policy={"output_format": "json_value"},
    )

    started_at = time.perf_counter()
    try:
        result = execute_llm_step(request)
    finally:
        release_progress.set()

    assert result.status == "success"
    assert (time.perf_counter() - started_at) < 1.0
    assert progress_entered.wait(1.0)


def test_bounded_llm_step_progress_event_does_not_block_execution_guard(
    monkeypatch,
) -> None:
    progress_entered = threading.Event()
    release_progress = threading.Event()

    class _FastClient:
        def generate(self, *_args, **_kwargs):
            return '{"ok": true}'

    def _blocking_progress(_payload):
        progress_entered.set()
        release_progress.wait(2.0)

    monkeypatch.setenv("VON_CONVERSATION_TURN_LLM_TIMEOUT_SEC", "1")
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=_FastClient(),
            model="gpt-4.1-mini",
        ),
        data={
            "emit_progress": _blocking_progress,
            "context_messages": [{"role": "user", "content": "Hello"}],
        },
        workflow_state_id="context_adjudication",
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={
            "policy_stage": "context_adjudication",
            "context_messages_context_key": "context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    started_at = time.perf_counter()
    try:
        result = execute_llm_step(request)
    finally:
        release_progress.set()

    assert result.status == "success"
    assert (time.perf_counter() - started_at) < 1.0
    assert progress_entered.wait(1.0)


def test_direct_llm_step_keeps_elapsed_threshold_out_of_client_params() -> None:
    captured: dict[str, object] = {}

    class _CapturingClient:
        def generate(self, *_args, **kwargs):
            captured["llm_params"] = kwargs.get("llm_params")
            return '{"ok": true}'

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=_CapturingClient(),
            model="gpt-4.1-mini",
        ),
        data={
            "conversation_turn_llm_timeout_override_sec": 12,
            "context_messages": [{"role": "user", "content": "Hello"}],
        },
        prompt_contract={"prompt_text": "Return JSON only."},
        llm_policy={"context_messages_context_key": "context_messages"},
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    assert captured["llm_params"] is None


def test_llm_call_can_finish_after_advisory_budget_without_hard_guard() -> None:
    import time

    import src.backend.workflows.llm_step_executor as mod

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(llm_client=MagicMock(), model="gpt-test"),
        data={"aux_llm_calls": []},
        workflow_id="#V#workflow",
        workflow_state_id="#V#step",
    )

    result = mod._run_llm_call_with_timeout(
        request,
        operation=lambda: (time.sleep(0.3), "completed")[1],
        stage="test_stage",
        prompt_id="#V#prompt",
        selected_model="gpt-test",
        selected_candidate={"provider": "openai"},
        timeout_seconds=0.02,
        hard_guard_timeout_seconds=0.6,
        llm_calls=[],
    )

    assert result == "completed"
    advisory_entries = [
        entry
        for entry in request.data["aux_llm_calls"]
        if entry.get("type") == "workflow_llm_advisory_budget"
    ]
    assert len(advisory_entries) == 1
    assert advisory_entries[0]["status"] == "exceeded_continuing"
    assert advisory_entries[0]["advisory_timeout_seconds"] == 0.02
    assert advisory_entries[0]["hard_guard_timeout_seconds"] is None


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


def test_execute_llm_step_preserves_structured_transport_blocker_envelope(
    monkeypatch,
) -> None:
    decision = {
        "schema_version": "structured_tool_transport_decision.v1",
        "status": "compatible",
        "capability_class": "responses_required",
        "effective_api_surface": "responses",
        "capability_source": "vontology_graph",
        "profile_concept_id": "#V#synthetic_responses_profile",
        "advertised_alternatives": [],
        "llm_exchange_id": "llm-exchange-synthetic",
    }

    class _StubGateway:
        def describe_methods(self):
            return {}

    class _StubOrchestrator:
        def _select_model_for_stage(self, **_kwargs):
            return "synthetic-model"

        def _action_tool_calling_plan(self, request):
            request.data["llm_calls"].append(
                {
                    "stage": "tool_call",
                    "status": "failed",
                    "failure_kind": "structured_tool_capability_rejected",
                }
            )
            request.data["aux_llm_calls"].append(
                {
                    "type": "structured_tool_transport_blocker",
                    "transport": decision,
                }
            )
            raise StructuredToolCapabilityRejectedError(
                "The represented structured-tool surface was rejected.",
                decision=decision,
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request, **_kwargs: (
            _StubOrchestrator(),
            object(),
            {"models": []},
            None,
            None,
        ),
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="synthetic-model",
        ),
        data={
            "requested_client_type": "openai",
            "context_messages": [{"role": "user", "content": "Use a tool"}],
        },
        workflow_id="#V#synthetic_workflow",
        workflow_state_id="#V#synthetic_tool_step",
        prompt_contract={"prompt_text": "Use the represented tool."},
        llm_policy={
            "policy_stage": "tool_call",
            "tool_mode": "allowed",
            "allowed_tools": ["synthetic_lookup"],
            "context_messages_context_key": "context_messages",
        },
    )

    result = execute_llm_step(request)

    assert result.status == "failed"
    assert result.error == (
        "workflow_structured_tool_transport_blocked:structured_tool_capability_rejected"
    )
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["completion_reason"] == "structured_tool_transport_blocked"
    assert envelope["failure_kind"] == "structured_tool_capability_rejected"
    assert envelope["retryable"] is False
    assert envelope["fail_closed"] is True
    assert envelope["fallback_used"] is False
    assert envelope["tool_invocations"] == []
    assert envelope["transport_decision"] == decision
    assert envelope["recovery_affordances"] == {
        "advertised_api_surface_alternatives": [],
        "profile_concept_id": "#V#synthetic_responses_profile",
        "capability_source": "vontology_graph",
    }
    assert envelope["llm_calls"][0]["failure_kind"] == (
        "structured_tool_capability_rejected"
    )
    assert envelope["aux_llm_calls"][0]["type"] == ("structured_tool_transport_blocker")


def test_structured_transport_failure_after_tool_records_side_effect_accounting(
    monkeypatch,
) -> None:
    decision = {
        "schema_version": "structured_tool_transport_decision.v1",
        "status": "compatible",
        "effective_api_surface": "responses",
        "failure_kind": "structured_tool_protocol_error",
    }

    class _StubGateway:
        def describe_methods(self):
            return {}

    class _StubOrchestrator:
        def _select_model_for_stage(self, **_kwargs):
            return "synthetic-model"

        def _action_tool_calling_plan(self, _request):
            return WorkflowActionResult(
                outputs={
                    "tool_calls_present": True,
                    "tool_calls": [
                        {
                            "action": "call",
                            "tool": "synthetic_lookup",
                            "payload": {"query": "value"},
                            "_call_id": "call-provider-1",
                        }
                    ],
                }
            )

        def _action_tool_calling_validate(self, _request):
            return WorkflowActionResult(
                outputs={
                    "tool_calls_validated": True,
                    "tool_call_repair_required": False,
                }
            )

        def _action_tool_calling_execute(self, request):
            invocation = {
                "tool": "synthetic_lookup",
                "payload": {"query": "value"},
                "call_id": "call-provider-1",
                "success": True,
                "status": "ok",
            }
            tool_message = {
                "role": "tool",
                "tool_call_id": "call-provider-1",
                "content": '{"success":true}',
            }
            request.data["invocations"].append(invocation)
            request.data["tool_messages"].append(tool_message)
            return WorkflowActionResult(outputs={"iteration_count": 1})

        def _action_tool_calling_backfill(self, _request):
            raise StructuredToolCapabilityRejectedError(
                "Continuation failed after tool execution.",
                decision=decision,
            )

    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request, **_kwargs: (
            _StubOrchestrator(),
            object(),
            {"models": []},
            None,
            None,
        ),
    )
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="synthetic-model",
        ),
        data={
            "requested_client_type": "openai",
            "context_messages": [{"role": "user", "content": "Use a tool"}],
        },
        workflow_id="#V#synthetic_workflow",
        workflow_state_id="#V#synthetic_tool_step",
        prompt_contract={"prompt_text": "Use the represented tool."},
        llm_policy={
            "policy_stage": "tool_call",
            "tool_mode": "allowed",
            "allowed_tools": ["synthetic_lookup"],
            "context_messages_context_key": "context_messages",
        },
    )

    result = execute_llm_step(request)

    assert result.status == "failed"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["tool_invocations"] == [
        {
            "tool": "synthetic_lookup",
            "payload": {"query": "value"},
            "call_id": "call-provider-1",
            "success": True,
            "status": "ok",
        }
    ]
    assert envelope["tool_messages"] == [
        {
            "role": "tool",
            "tool_call_id": "call-provider-1",
            "content": '{"success":true}',
        }
    ]
    assert result.outputs["tool_invocations"] == envelope["tool_invocations"]
    assert result.outputs["tool_messages"] == envelope["tool_messages"]


def test_execute_llm_step_retains_slow_context_adjudication_gateway_result(
    monkeypatch,
) -> None:
    class _StubOrchestrator:
        def _select_model_for_stage(self, **_kwargs):
            return "gpt-4.1-mini"

        def _run_llm_with_fallbacks(self, **_kwargs):
            time.sleep(0.3)
            return ('{"ok": true}', "gpt-4.1-mini", {"provider": "openai"})

    monkeypatch.setattr(
        lse,
        "_conversation_turn_llm_timeout_override_sec",
        lambda _request: 0.02,
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request: (_StubOrchestrator(), object(), {"models": []}, None, None),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=object(),
            model="gpt-4.1-mini",
        ),
        data={
            "requested_client_type": "openai",
            "context_messages": [{"role": "user", "content": "Tell me about JVN"}],
        },
        workflow_id="#V#turn_prompt_context_adjudication_workflow",
        workflow_state_id=(
            "#V#workflow_step_turn_prompt_context_adjudication_workflow_"
            "context_adjudication_decision"
        ),
        prompt_contract={
            "prompt_text": "Return context adjudication JSON.",
            "resolved_prompt_concept_id": "#V#turn_prompt_context_adjudication_prompt",
        },
        llm_policy={
            "policy_stage": "context_adjudication",
            "context_messages_context_key": "context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validated_json"] == {"ok": True}
    assert not [
        entry for entry in envelope["llm_calls"] if entry.get("status") == "timed_out"
    ]
    assert any(
        entry.get("type") == "workflow_llm_advisory_budget"
        and entry.get("status") == "exceeded_continuing"
        for entry in result.outputs["aux_llm_calls"]
    )


def test_execute_llm_step_retains_slow_context_adjudication_direct_result(
    monkeypatch,
) -> None:
    class _BlockingClient:
        def generate(self, *_args, **_kwargs):
            time.sleep(0.3)
            return '{"ok": true}'

    monkeypatch.setattr(
        lse,
        "_conversation_turn_llm_timeout_override_sec",
        lambda _request: 0.02,
    )
    monkeypatch.setattr(
        lse,
        "resolve_model_prompt_variant",
        lambda **kwargs: pmr.WorkflowPromptVariantResolution(
            base_prompt_concept_id=kwargs.get("base_prompt_concept_id"),
            selected_prompt_concept_id=kwargs.get("base_prompt_concept_id"),
            prompt_text=kwargs.get("base_prompt_text"),
            rendered_variables=kwargs.get("variables") or {},
            match_reason="base_prompt",
            diagnostics={"match_reason": "base_prompt"},
        ),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=_BlockingClient(),
            model="gpt-4.1-mini",
        ),
        data={
            "requested_client_type": "openai",
            "context_messages": [{"role": "user", "content": "Tell me about JVN"}],
        },
        workflow_id="#V#turn_prompt_context_adjudication_workflow",
        workflow_state_id=(
            "#V#workflow_step_turn_prompt_context_adjudication_workflow_"
            "context_adjudication_decision"
        ),
        prompt_contract={
            "prompt_text": "Return context adjudication JSON.",
            "resolved_prompt_concept_id": "#V#turn_prompt_context_adjudication_prompt",
        },
        llm_policy={
            "policy_stage": "context_adjudication",
            "context_messages_context_key": "context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validated_json"] == {"ok": True}
    assert not [
        entry for entry in envelope["llm_calls"] if entry.get("status") == "timed_out"
    ]
    assert any(
        entry.get("type") == "workflow_llm_advisory_budget"
        for entry in result.outputs["aux_llm_calls"]
    )


def test_execute_llm_step_retains_slow_context_adjudication_tool_plan(
    monkeypatch,
) -> None:
    class _StubGateway:
        def describe_methods(self):
            return {}

    class _StubOrchestrator:
        def _select_model_for_stage(self, **_kwargs):
            return "gpt-4.1-mini"

        def _action_tool_calling_plan(self, _request):
            time.sleep(0.3)
            return WorkflowActionResult(
                outputs={
                    "tool_calls_present": False,
                    "final_response": '{"ok": true}',
                }
            )

    monkeypatch.setattr(
        lse,
        "_conversation_turn_llm_timeout_override_sec",
        lambda _request: 0.02,
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor._build_gateway_runtime",
        lambda request, **_kwargs: (
            _StubOrchestrator(),
            object(),
            {"models": []},
            None,
            None,
        ),
    )

    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=MagicMock(),
            gateway=_StubGateway(),
            model="gpt-4.1-mini",
        ),
        data={
            "requested_client_type": "openai",
            "context_messages": [{"role": "user", "content": "Tell me about JVN"}],
        },
        workflow_id="#V#turn_prompt_context_adjudication_workflow",
        workflow_state_id=(
            "#V#workflow_step_turn_prompt_context_adjudication_workflow_"
            "context_adjudication_decision"
        ),
        prompt_contract={
            "prompt_text": "Return context adjudication JSON.",
            "requested_prompt_concept_ids": [
                "#V#turn_prompt_context_adjudication_prompt"
            ],
        },
        llm_policy={
            "policy_stage": "context_adjudication",
            "tool_mode": "allowed",
            "allowed_tools": ["jira_get_issue"],
            "context_messages_context_key": "context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validated_json"] == {"ok": True}
    assert not [
        entry for entry in envelope["llm_calls"] if entry.get("status") == "timed_out"
    ]
    assert any(
        entry.get("type") == "workflow_llm_advisory_budget"
        for entry in result.outputs["aux_llm_calls"]
    )


def test_execute_llm_step_retains_result_after_slow_prompt_render(
    monkeypatch,
) -> None:
    class _BlockingPromptService:
        def __init__(self, **_kwargs):
            pass

        def render_prompt(self, *_args, **_kwargs):
            time.sleep(0.3)
            return pts.RenderedPrompt(
                prompt_id="#V#turn_prompt_context_adjudication_prompt",
                text='{"ok": true}',
                variables={},
            )

    monkeypatch.setattr(
        lse,
        "_conversation_turn_llm_timeout_override_sec",
        lambda _request: 0.02,
    )
    monkeypatch.setattr(
        "src.backend.workflows.llm_step_executor.PromptTemplateService",
        _BlockingPromptService,
    )

    client = MagicMock()
    client.generate.return_value = '{"ok": true}'
    request = WorkflowActionRequest(
        action_id="llm.action",
        inputs={},
        environment=WorkflowEnvironment(
            llm_client=client,
            model="gpt-4.1-mini",
        ),
        data={
            "requested_client_type": "openai",
            "context_messages": [{"role": "user", "content": "Tell me about JVN"}],
        },
        workflow_id="#V#turn_prompt_context_adjudication_workflow",
        workflow_state_id=(
            "#V#workflow_step_turn_prompt_context_adjudication_workflow_"
            "context_adjudication_decision"
        ),
        prompt_contract={
            "requested_prompt_concept_ids": [
                "#V#turn_prompt_context_adjudication_prompt"
            ],
        },
        llm_policy={
            "policy_stage": "context_adjudication",
            "context_messages_context_key": "context_messages",
        },
        validation_policy={"output_format": "json_value"},
    )

    result = execute_llm_step(request)

    assert result.status == "success"
    envelope = result.outputs["llm_step_envelope"]
    assert envelope["validated_json"] == {"ok": True}
    assert not [
        entry for entry in envelope["llm_calls"] if entry.get("status") == "timed_out"
    ]
    assert any(
        entry.get("type") == "workflow_llm_advisory_budget"
        for entry in result.outputs["aux_llm_calls"]
    )


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
