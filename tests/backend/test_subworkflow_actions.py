from __future__ import annotations

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.subworkflow_actions import (
    register_subworkflow_actions,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
)
from src.backend.workflows.trace_model import WorkflowExecutionTrace


def _always_true(_context):
    return True


def _child_success_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#child_success",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="child.emit"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
    )


def _child_failure_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#child_failure",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="child.fail"),),
                terminal=True,
            ),
        },
        termination_states=("start",),
    )


def test_subworkflow_action_executes_child_and_emits_trace_chain() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit",
            handler=lambda request: WorkflowActionResult(
                status="success",
                outputs={"answer": request.data.get("value")},
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_success",
                            "value": 41,
                            "__parent_workflow_id": "#V#parent_workflow",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
                metadata={
                    "tool_output_context_mappings": [
                        {
                            "tool_output_field": "answer",
                            "context_key": "child_answer",
                        }
                    ]
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    trace = WorkflowExecutionTrace(workflow_id="#V#parent_workflow")
    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
        trace=trace,
    )

    assert result.completed is True
    assert result.data["child_answer"] == 41
    subworkflow_events = trace.metadata.get("subworkflow_invocations")
    assert isinstance(subworkflow_events, list)
    assert len(subworkflow_events) == 1
    event = subworkflow_events[0]
    assert event["parent_workflow_id"] == "#V#parent_workflow"
    assert event["child_workflow_id"] == "#V#child_success"
    assert event["child_completed"] is True
    assert event["invocation_chain"] == ["#V#parent_workflow", "#V#child_success"]


def test_subworkflow_action_applies_launch_contract_ambient_exclusions() -> None:
    registry = ActionRegistry()
    captured_child_data: dict[str, object] = {}
    registry.register(
        ActionSpec(
            action_id="child.capture",
            handler=lambda request: (
                captured_child_data.update(dict(request.data))
                or WorkflowActionResult(status="success", outputs={"ok": True})
            ),
        )
    )
    child_definition = WorkflowDefinition(
        workflow_id="#V#child_launch_contract",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="child.capture"),),
                terminal=True,
            )
        },
        termination_states=("start",),
        metadata={
            "launch_input_contract": {
                "schema_version": "workflow_launch_input_contract.v1",
                "required_inputs": ["prompt"],
                "excluded_ambient_input_keys": [
                    "arxiv_id",
                    "source_uri",
                    "paper_concept_id",
                    "file_copy_concept_id",
                ],
                "input_mappings": [
                    {
                        "target_context_key": "prompt",
                        "source_expression": "inputs.prompt",
                        "required": True,
                    },
                    {
                        "target_context_key": "arxiv_id",
                        "source_expression": "inputs.arxiv_id",
                    },
                    {
                        "target_context_key": "arxiv_id",
                        "source_expression": "inputs.prompt",
                        "extractor": "arxiv_id",
                    },
                    {
                        "target_context_key": "arxiv_ids",
                        "source_expression": "inputs.prompt",
                        "extractor": "arxiv_id_list",
                    },
                ],
            },
            "launch_input_contract_source": "test_contract",
        },
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            child_definition if workflow_id == "#V#child_launch_contract" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_launch_contract",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_launch_contract",
                            "prompt": (
                                "Is this paper represented? "
                                "https://arxiv.org/pdf/2603.22519v2"
                            ),
                            "paper_concept_id": "#V#prior_paper",
                            "file_copy_concept_id": "#V#prior_file_copy",
                            "arxiv_id": "2406.15341",
                            "source_uri": "https://arxiv.org/abs/2406.15341",
                            "__workflow_ambient_input_keys": [
                                "arxiv_id",
                                "source_uri",
                            ],
                            "__parent_workflow_id": "#V#parent_launch_contract",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
                terminal=True,
            )
        },
        termination_states=("start",),
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert "paper_concept_id" not in captured_child_data
    assert "file_copy_concept_id" not in captured_child_data
    assert "source_uri" not in captured_child_data
    assert captured_child_data["arxiv_id"] == "2603.22519"
    assert captured_child_data["arxiv_ids"] == ["2603.22519"]
    launch_resolution = captured_child_data.get("workflow_launch_input_resolution")
    assert isinstance(launch_resolution, dict)
    assert launch_resolution.get("excluded_ambient_inputs_applied") == [
        "arxiv_id",
        "file_copy_concept_id",
        "paper_concept_id",
        "source_uri",
    ]


def test_subworkflow_action_propagates_child_failure_by_default() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.fail",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="child_boom",
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_failure_definition() if workflow_id == "#V#child_failure" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_failure",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_failure",
                            "__parent_workflow_id": "#V#parent_failure",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
            )
        },
    )

    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is False
    assert result.error == "subworkflow_failed:#V#child_failure:child_boom"


def test_subworkflow_action_can_capture_child_failure() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.fail",
            handler=lambda _request: WorkflowActionResult(
                status="failed",
                error="child_boom",
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_failure_definition() if workflow_id == "#V#child_failure" else None
        ),
    )

    parent = WorkflowDefinition(
        workflow_id="#V#parent_capture",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID,
                        inputs={
                            "workflow_id": "#V#child_failure",
                            "failure_mode": WORKFLOW_SUBWORKFLOW_FAILURE_MODE_CAPTURE,
                            "__parent_workflow_id": "#V#parent_capture",
                            "__parent_state_id": "start",
                        },
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=_always_true,
                        reason="next_step",
                        condition_spec={"kind": "always"},
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )
    result = WorkflowExecutor(registry=registry, max_transitions=10).run(
        parent,
        environment=WorkflowEnvironment(llm_client=None),
        data={},
    )

    assert result.completed is True
    assert result.data["child_workflow_failed"] is True
    assert result.data["subworkflow_error"] == "subworkflow_failed:#V#child_failure:child_boom"


def test_subworkflow_action_rejects_recursive_self_invocation() -> None:
    registry = ActionRegistry()
    register_subworkflow_actions(
        registry,
        definition_loader=lambda _workflow_id: _child_success_definition(),
    )

    context: dict[str, object] = {}
    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#loop",
            "__parent_workflow_id": "#V#loop",
            "__parent_state_id": "start",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "failed"
    assert "subworkflow_recursive_invocation" in str(execution.error or "")


def test_subworkflow_action_is_idempotent_for_same_inputs() -> None:
    registry = ActionRegistry()

    def _emit_value(request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            status="success",
            outputs={"answer": request.data.get("value")},
        )

    registry.register(ActionSpec(action_id="child.emit", handler=_emit_value))
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    context_a: dict[str, object] = {}
    context_b: dict[str, object] = {}
    first = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "value": "stable",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context=context_a,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )
    second = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "value": "stable",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context=context_b,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert first.status == "success"
    assert second.status == "success"
    assert first.outputs["result"]["answer"] == "stable"
    assert second.outputs["result"]["answer"] == "stable"
    assert first.outputs["result"]["answer"] == second.outputs["result"]["answer"]


def test_subworkflow_action_enforces_invocation_budget(monkeypatch) -> None:
    registry = ActionRegistry()
    register_subworkflow_actions(
        registry,
        definition_loader=lambda _workflow_id: _child_success_definition(),
    )
    monkeypatch.setenv("VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS", "1")

    context: dict[str, object] = {"__workflow_subworkflow_invocation_count": 1}
    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "failed"
    assert "subworkflow_invocation_budget_exceeded" in str(execution.error or "")


def test_subworkflow_action_propagates_child_control_signal() -> None:
    registry = ActionRegistry()

    child_definition = WorkflowDefinition(
        workflow_id="#V#child_return",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="child.return"),),
                terminal=True,
            )
        },
        termination_states=("start",),
    )

    registry.register(
        ActionSpec(
            action_id="child.return",
            handler=lambda _request: WorkflowActionResult(
                outputs={
                    "control_signal": "return",
                    "return_payload": {"value": 7},
                }
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            child_definition if workflow_id == "#V#child_return" else None
        ),
    )

    context: dict[str, object] = {}
    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_return",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "success"
    assert execution.outputs.get("control_signal") == "return"
    nested = execution.outputs.get("subworkflow_result_envelope")
    assert isinstance(nested, dict)
    assert nested.get("control_signal") == "return"


def test_subworkflow_action_compacts_child_runtime_payload_before_propagating() -> None:
    registry = ActionRegistry()

    def _emit_business_result(_request: WorkflowActionRequest) -> WorkflowActionResult:
        return WorkflowActionResult(
            outputs={
                "answer": "stable",
                "validated_json": {"answer": "stable"},
                "llm_step_envelope": {"selected_model": "gpt-5-mini"},
                "tool_messages": [{"tool": "dummy"}],
            }
        )

    registry.register(ActionSpec(action_id="child.emit", handler=_emit_business_result))
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "success"
    result_payload = execution.outputs.get("result")
    assert isinstance(result_payload, dict)
    assert result_payload.get("answer") == "stable"
    assert result_payload.get("validated_json") == {"answer": "stable"}
    assert "llm_step_envelope" not in result_payload
    assert "tool_messages" not in result_payload
    assert "workflow_step_result_envelopes" not in result_payload
    assert "last_workflow_step_result_envelope" not in result_payload
    assert "workflow_result_envelope" not in result_payload


def test_subworkflow_action_preserves_compact_child_mcp_invocation_evidence() -> None:
    registry = ActionRegistry()
    child_definition = WorkflowDefinition(
        workflow_id="#V#child_mcp_tool",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="workflow_mcp.invoke_tool"),),
                terminal=True,
            )
        },
        termination_states=("start",),
    )

    registry.register(
        ActionSpec(
            action_id="workflow_mcp.invoke_tool",
            handler=lambda _request: WorkflowActionResult(
                outputs={
                    "mcp_requested_tool": "gmail_get_message",
                    "mcp_resolved_tool": "gmail_get_message",
                    "success": True,
                    "message_id": "msg-123",
                }
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            child_definition if workflow_id == "#V#child_mcp_tool" else None
        ),
    )

    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_mcp_tool",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "success"
    result_payload = execution.outputs.get("result")
    assert isinstance(result_payload, dict)
    invocations = result_payload.get("invocations")
    assert isinstance(invocations, list)
    assert invocations[0]["tool"] == "gmail_get_message"
    assert invocations[0]["workflow_action_id"] == "workflow_mcp.invoke_tool"
    assert invocations[0]["effective_payload"]["message_id"] == "msg-123"
    assert "workflow_step_result_envelopes" not in result_payload


def test_subworkflow_action_can_be_rebound_after_registry_merge() -> None:
    base_registry = ActionRegistry()
    register_subworkflow_actions(
        base_registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    merged_registry = ActionRegistry()
    merged_registry.merge(base_registry)
    merged_registry.register(
        ActionSpec(
            action_id="child.emit",
            handler=lambda _request: WorkflowActionResult(outputs={"answer": 42}),
        )
    )

    stale_execution = merged_registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )
    assert stale_execution.status == "failed"
    assert "action_not_registered:child.emit" in (stale_execution.error or "")

    register_subworkflow_actions(
        merged_registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
        overwrite=True,
    )
    rebound_execution = merged_registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert rebound_execution.status == "success"
    result_payload = rebound_execution.outputs.get("result")
    assert isinstance(result_payload, dict)
    assert result_payload["answer"] == 42


def test_subworkflow_action_can_inherit_parent_context_explicitly() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit",
            handler=lambda request: WorkflowActionResult(
                outputs={"answer": request.data.get("augmented_context")}
            ),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda workflow_id: (
            _child_success_definition() if workflow_id == "#V#child_success" else None
        ),
    )

    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "inherit_parent_context": True,
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context={
            "augmented_context": [{"role": "system", "content": "shared"}],
            "__workflow_subworkflow_invocation_count": 3,
        },
        env=WorkflowEnvironment(llm_client=None),
        trace=None,
    )

    assert execution.status == "success"
    result_payload = execution.outputs.get("result")
    assert isinstance(result_payload, dict)
    assert result_payload["answer"] == [{"role": "system", "content": "shared"}]
    assert "__workflow_subworkflow_invocation_count" not in result_payload


def test_subworkflow_action_uses_authority_resolver_with_actor_context(
    monkeypatch,
) -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit",
            handler=lambda _request: WorkflowActionResult(outputs={"answer": 42}),
        )
    )
    register_subworkflow_actions(
        registry,
        definition_loader=lambda _workflow_id: None,
    )
    captured: dict[str, object] = {}

    class _Resolution:
        definition = _child_success_definition()
        error_code = None

        @staticmethod
        def to_dict():
            return {
                "workflow_id": "#V#child_success",
                "success": True,
                "registration_source": "vontology",
            }

    def _resolve_from_authority(workflow_id: str, **kwargs):
        captured["workflow_id"] = workflow_id
        captured.update(kwargs)
        return _Resolution()

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory."
        "resolve_workflow_definition_from_authority",
        _resolve_from_authority,
    )

    execution = registry.execute(
        WORKFLOW_SUBWORKFLOW_ACTION_ID,
        inputs={
            "workflow_id": "#V#child_success",
            "__parent_workflow_id": "#V#parent",
            "__parent_state_id": "start",
        },
        context={},
        env=WorkflowEnvironment(
            llm_client=None,
            user_namespace=(
                "#V#michael_witbrock@university_of_auckland_strong_ai_lab"
            ),
        ),
        trace=None,
    )

    assert execution.status == "success"
    assert captured["workflow_id"] == "#V#child_success"
    assert captured["use_current_shared_registry"] is True
    assert "promote_to_registry" not in captured
    assert captured["actor_user_id"] == "#V#michael_witbrock"
    assert captured["actor_org_id"] == "#V#university_of_auckland_strong_ai_lab"
