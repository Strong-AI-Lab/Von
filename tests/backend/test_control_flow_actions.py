from __future__ import annotations

from types import SimpleNamespace

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import register_control_flow_actions
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
    WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
    WORKFLOW_CONTROL_ACTION_PAUSE_AT_CHECKPOINT_ID,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)


def _child_definition(workflow_id: str, action_id: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=action_id),),
                terminal=True,
            )
        },
        termination_states=("start",),
    )


def test_break_action_emits_control_signal() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    context: dict[str, object] = {}
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_BREAK_ID,
        inputs={"loop_scope_id": "main"},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("control_signal") == "break"
    assert result.outputs.get("control_scope") == "main"


def test_fork_join_actions_execute_and_merge_branch_results() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            action_id="child.emit_one",
            handler=lambda _request: WorkflowActionResult(outputs={"value_one": 1}),
        )
    )
    registry.register(
        ActionSpec(
            action_id="child.emit_two",
            handler=lambda _request: WorkflowActionResult(outputs={"value_two": 2}),
        )
    )
    definitions = {
        "#V#child_one": _child_definition("#V#child_one", "child.emit_one"),
        "#V#child_two": _child_definition("#V#child_two", "child.emit_two"),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    context: dict[str, object] = {}
    fork_result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FORK_ID,
        inputs={
            "fork_id": "main_fork",
            "failure_policy": "collect_errors",
            "branches": [
                {"branch_id": "a", "workflow_id": "#V#child_one"},
                {"branch_id": "b", "workflow_id": "#V#child_two"},
            ],
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert fork_result.status == "success"
    assert fork_result.outputs.get("fork_success_count") == 2
    join_result = registry.execute(
        WORKFLOW_CONTROL_ACTION_JOIN_ID,
        inputs={"fork_id": "main_fork"},
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert join_result.status == "success"
    assert join_result.outputs.get("fork_joined") is True
    merged = join_result.outputs.get("result")
    assert merged == {"value_one": 1, "value_two": 2}


def test_join_action_fails_without_matching_fork() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_JOIN_ID,
        inputs={"fork_id": "missing"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "join_without_matching_fork" in str(result.error or "")


def test_pause_at_checkpoint_fails_closed_without_fenced_durable_claim() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_PAUSE_AT_CHECKPOINT_ID,
        inputs={"reason_code": "certification_interruption"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
        trace=SimpleNamespace(instance_id="instance-1", metadata={}),
        workflow_id="#V#pause_probe",
        workflow_state_id="pause_here",
    )

    assert result.status == "failed"
    assert result.error == "workflow_checkpoint_pause_requires_durable_instance"


def test_pause_at_checkpoint_emits_bound_single_use_request() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_PAUSE_AT_CHECKPOINT_ID,
        inputs={"reason_code": "certification_interruption"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
        trace=SimpleNamespace(
            instance_id="instance-1",
            metadata={"durable_claim_fenced": True},
        ),
        workflow_id="#V#pause_probe",
        workflow_state_id="pause_here",
    )

    assert result.status == "success"
    request = result.outputs[WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY]
    assert request == {
        "schema_version": WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION,
        "instance_id": "instance-1",
        "workflow_id": "#V#pause_probe",
        "state_id": "pause_here",
        "reason_code": "certification_interruption",
        "request_sha256": request["request_sha256"],
        "resume_mode": "explicit_same_instance",
    }
    assert len(request["request_sha256"]) == 64


def test_for_each_action_executes_child_workflow_per_item() -> None:
    registry = ActionRegistry()

    def _emit_item(request):
        return WorkflowActionResult(
            outputs={
                "item_value": request.data.get("current_item"),
                "item_index": request.data.get("index"),
            }
        )

    registry.register(ActionSpec(action_id="child.emit_item", handler=_emit_item))
    definitions = {
        "#V#child_each": _child_definition("#V#child_each", "child.emit_item"),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    context: dict[str, object] = {"candidate_items": ["A", "B", "C"]}
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={
            "workflow_id": "#V#child_each",
            "items_context_key": "candidate_items",
            "max_items": 2,
            "max_concurrency": 2,
            "success_policy": "all_must_succeed",
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("for_each_item_count") == 2
    assert result.outputs.get("for_each_max_concurrency") == 2
    assert result.outputs.get("for_each_success_count") == 2
    assert result.outputs.get("for_each_error_count") == 0
    results = result.outputs.get("iteration_results")
    assert isinstance(results, list)
    assert results[0]["result"] == {"item_value": "A", "item_index": 0}
    assert results[1]["result"] == {"item_value": "B", "item_index": 1}
    assert result.outputs.get("successful_results") == [
        {"item_value": "A", "item_index": 0},
        {"item_value": "B", "item_index": 1},
    ]
    invocations = result.outputs.get("invocations")
    assert isinstance(invocations, list)
    assert [item.get("tool") for item in invocations] == [
        "child.emit_item",
        "child.emit_item",
    ]
    assert all(item.get("workflow_step_evidence") is True for item in invocations)
    assert results[0]["tool_invocations"][0]["payload"] == {
        "item_value": "A",
        "item_index": 0,
    }


def test_for_each_action_respects_partial_success_policy() -> None:
    registry = ActionRegistry()

    def _conditionally_fail(request):
        if request.data.get("current_item") == "bad":
            return WorkflowActionResult(status="failed", error="child_failed")
        return WorkflowActionResult(outputs={"item_value": request.data.get("current_item")})

    registry.register(
        ActionSpec(action_id="child.maybe_fail", handler=_conditionally_fail)
    )
    definitions = {
        "#V#child_each_partial": _child_definition(
            "#V#child_each_partial",
            "child.maybe_fail",
        ),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={
            "workflow_id": "#V#child_each_partial",
            "items": ["good", "bad"],
            "success_policy": "allow_partial",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs.get("for_each_success_count") == 1
    assert result.outputs.get("for_each_error_count") == 1
    assert result.outputs.get("for_each_partial_success") is True
    assert result.outputs.get("successful_results") == [{"item_value": "good"}]
    assert result.outputs.get("iteration_errors") == [
        {
            "index": 1,
            "item": "bad",
            "final_state": "start",
            "error": "child_failed",
        }
    ]


# ---------------------------------------------------------------------------
# context.set action tests (JVNAUTOSCI-1440)
# ---------------------------------------------------------------------------


def _build_context_set_registry() -> ActionRegistry:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _wid: None)
    return registry


def test_context_set_literal_values() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {"key": "flag_a", "value": True},
                {"key": "counter", "value": 42},
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["flag_a"] is True
    assert result.outputs["counter"] == 42
    assert result.outputs["_context_set_applied_keys"] == ["flag_a", "counter"]


def test_context_set_value_from_context() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {"key": "copied_val", "value_from_context": "source_key"},
            ]
        },
        context={"source_key": "hello"},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["copied_val"] == "hello"


def test_context_set_value_from_context_missing_source() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {"key": "dest", "value_from_context": "nonexistent"},
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["dest"] is None


def test_context_set_coalesces_first_present_context_path() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {
                    "key": "title",
                    "value_from_context_options": [
                        "title",
                        "paper_metadata.title",
                        "metadata.title",
                    ],
                    "skip_if_unresolved": True,
                }
            ]
        },
        context={
            "title": "",
            "paper_metadata": {"title": "Composable article metadata"},
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["title"] == "Composable article metadata"
    assert result.outputs["_context_set_applied_keys"] == ["title"]


def test_context_set_can_skip_unresolved_coalesce_assignment() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={
            "assignments": [
                {
                    "key": "title",
                    "value_from_context_options": ["missing", "metadata.title"],
                    "skip_if_unresolved": True,
                }
            ]
        },
        context={"metadata": {}},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert "title" not in result.outputs
    assert result.outputs["_context_set_applied_keys"] == []


def test_context_set_fails_when_assignments_missing() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "assignments_missing" in (result.error or "")


def test_context_set_fails_on_missing_key() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={"assignments": [{"value": "no_key"}]},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "missing_key" in (result.error or "")


def test_context_set_fails_on_non_mapping_assignment() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
        inputs={"assignments": ["not_a_dict"]},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert "not_mapping" in (result.error or "")


def test_context_project_uses_requirement_fields_without_domain_policy() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        "workflow_control.context_project",
        inputs={
            "target_key": "projected_item",
            "requirement": {
                "schema_version": "workflow_item_output_requirement.v1",
                "purpose": "mail_list_rendering",
                "required_fields": ["subject", "sender"],
                "excluded_fields": ["payload", "body"],
            },
            "always_include_fields": ["message_id"],
            "field_sources": {
                "message_id": "msg-1",
                "subject": "Subject line",
                "sender": "Sender <sender@example.test>",
                "payload": {"large": "not selected"},
            },
            "include_missing_fields": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["projected_item"] == {
        "message_id": "msg-1",
        "subject": "Subject line",
        "sender": "Sender <sender@example.test>",
    }
    projection = result.outputs["projected_item_projection"]
    assert projection["purpose"] == "mail_list_rendering"
    assert projection["requested_fields"] == ["message_id", "subject", "sender"]
    assert projection["missing_fields"] == []


def test_context_project_reports_missing_contract_fields() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        "workflow_control.context_project",
        inputs={
            "target_key": "projected_item",
            "required_fields": ["paper_url", "doi"],
            "field_sources": {
                "message_id": "msg-1",
                "paper_url": "https://arxiv.org/abs/2601.00001",
            },
            "include_missing_fields": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["projected_item"] == {
        "paper_url": "https://arxiv.org/abs/2601.00001",
        "_missing_fields": ["doi"],
    }
    assert result.outputs["projected_item_projection"]["missing_fields"] == ["doi"]


def test_context_project_preserves_explicit_null_only_when_requested() -> None:
    registry = _build_context_set_registry()

    default_result = registry.execute(
        "workflow_control.context_project",
        inputs={
            "target_key": "projected_item",
            "required_fields": ["nullable_parent"],
            "field_sources": {"nullable_parent": None},
            "include_missing_fields": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )
    nullable_result = registry.execute(
        "workflow_control.context_project",
        inputs={
            "target_key": "projected_item",
            "required_fields": ["nullable_parent"],
            "field_sources": {"nullable_parent": None},
            "include_missing_fields": True,
            "include_null_fields": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert default_result.status == "success"
    assert default_result.outputs["projected_item"] == {
        "_missing_fields": ["nullable_parent"]
    }
    assert nullable_result.status == "success"
    assert nullable_result.outputs["projected_item"] == {"nullable_parent": None}
    projection = nullable_result.outputs["projected_item_projection"]
    assert projection["selected_fields"] == ["nullable_parent"]
    assert projection["missing_fields"] == []
    assert projection["include_null_fields"] is True


def test_context_template_renders_context_request_and_json_values() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
        inputs={
            "assignments": [
                {
                    "key": "guidance_envelope",
                    "template": "workflow={workflow_id}; model={model}; refs={refs}",
                    "variables": {
                        "workflow_id": {"value_from_request": "workflow_id"},
                        "model": {"value_from_environment": "model"},
                        "refs": {
                            "value_from_context": "guidance.evidence_refs",
                            "format": "json",
                        },
                    },
                }
            ]
        },
        context={"guidance": {"evidence_refs": ["request:1", "trace:2"]}},
        env=WorkflowEnvironment(llm_client=None, model="test-model"),
        workflow_id="#V#example_workflow",
    )

    assert result.status == "success"
    assert result.outputs["guidance_envelope"] == (
        'workflow=#V#example_workflow; model=test-model; '
        'refs=["request:1", "trace:2"]'
    )
    assert result.outputs["_context_template_applied_keys"] == ["guidance_envelope"]


def test_context_template_renders_concept_id_from_template() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
        inputs={
            "assignments": [
                {
                    "key": "profile_id",
                    "template": "Workflow LLM experience profile: {workflow} / {model}",
                    "transform": "concept_id",
                    "variables": {
                        "workflow": {"value": "#V#conversation_turn_execution_workflow"},
                        "model": {"value": "GPT-5.2"},
                    },
                }
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["profile_id"] == (
        "#V#workflow_llm_experience_profile_v_conversation_turn_execution_workflow_gpt_5_2"
    )


def test_context_template_uses_defaults_for_missing_values() -> None:
    registry = _build_context_set_registry()
    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
        inputs={
            "assignments": [
                {
                    "key": "profile_name",
                    "template": "{workflow_id} / {model}",
                    "variables": {
                        "workflow_id": {
                            "value_from_context_options": ["missing", "also_missing"],
                            "default": "unknown_workflow",
                        },
                        "model": {
                            "value_from_environment": "model",
                            "default": "unknown_model",
                        },
                    },
                }
            ]
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["profile_name"] == "unknown_workflow / unknown_model"
