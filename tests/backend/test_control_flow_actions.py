from __future__ import annotations

from types import SimpleNamespace

from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
)
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_KEY,
    WORKFLOW_CHECKPOINT_PAUSE_REQUEST_SCHEMA_VERSION,
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_SET_ID,
    WORKFLOW_CONTROL_ACTION_CONTEXT_TEMPLATE_ID,
    WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
    WORKFLOW_CONTROL_ACTION_KR_MATERIALISATION_GUARD_ID,
    WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_READBACK_ID,
    WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
    WORKFLOW_CONTROL_ACTION_PAUSE_AT_CHECKPOINT_ID,
    WORKFLOW_CONTROL_ACTION_RELATIONSHIP_EFFECT_READBACK_ID,
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


def test_optional_kr_materialisation_guard_is_registered_and_noop_when_absent() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_KR_MATERIALISATION_GUARD_ID,
        inputs={"phase": "plan"},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["guard_applied"] is False
    assert result.outputs["guard_passed"] is True


def _verified_kr_concept_results() -> list[dict[str, object]]:
    return [
        {
            "completed": True,
            "item": {"key": "trip"},
            "result": {
                "kr_concept_key": "trip",
                "kr_concept_id": "#V#trip_1",
                "kr_readback_concept_id": "#V#trip_1",
            },
        },
        {
            "completed": True,
            "item": {"key": "flight"},
            "result": {
                "kr_concept_key": "flight",
                "kr_concept_id": "#V#flight_1",
                "kr_readback_concept_id": "#V#flight_1",
            },
        },
    ]


def test_kr_relationship_resolution_maps_verified_keys_and_preserves_exact_ids() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
        inputs={},
        context={
            "kr_concept_iteration_results": _verified_kr_concept_results(),
            "kr_relationship_specs": [
                {
                    "key": "trip_flight",
                    "source_key": "trip",
                    "predicate_id": "#V#has_trip_component",
                    "target_key": "flight",
                    "rationale": "The booked flight is part of the trip.",
                },
                {
                    "key": "trip_owner",
                    "source_key": "trip",
                    "predicate": "#V#owned_by",
                    "target_id": "#V#person_1",
                },
            ],
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs == {
        "decision": "assert",
        "blocking_reason": None,
        "resolved_relationship_specs": [
            {
                "key": "trip_flight",
                "source_id": "#V#trip_1",
                "predicate": "#V#has_trip_component",
                "target_id": "#V#flight_1",
                "rationale": "The booked flight is part of the trip.",
            },
            {
                "key": "trip_owner",
                "source_id": "#V#trip_1",
                "predicate": "#V#owned_by",
                "target_id": "#V#person_1",
                "rationale": "",
            },
        ],
    }


def test_kr_relationship_resolution_blocks_mixed_and_duplicate_references() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)
    context = {
        "kr_concept_iteration_results": _verified_kr_concept_results(),
    }

    mixed = registry.execute(
        WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
        inputs={
            "relationship_specs": [
                {
                    "source_key": "trip",
                    "source_id": "#V#trip_1",
                    "predicate": "#V#has_trip_component",
                    "target_key": "flight",
                }
            ]
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )
    duplicate = registry.execute(
        WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
        inputs={
            "relationship_specs": [
                {
                    "source_key": "trip",
                    "predicate": "#V#has_trip_component",
                    "target_key": "flight",
                },
                {
                    "source_id": "#V#trip_1",
                    "predicate_id": "#V#has_trip_component",
                    "target_id": "#V#flight_1",
                },
            ]
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )
    over_bound = registry.execute(
        WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_RESOLUTION_ID,
        inputs={
            "relationship_specs": [
                {
                    "source_key": "trip",
                    "predicate": f"#V#predicate_{index}",
                    "target_key": "flight",
                }
                for index in range(41)
            ]
        },
        context=context,
        env=WorkflowEnvironment(llm_client=None),
    )

    assert mixed.outputs["decision"] == "block"
    assert mixed.outputs["blocking_reason"] == (
        "kr_relationship_resolution_source_reference_invalid"
    )
    assert duplicate.outputs["decision"] == "block"
    assert duplicate.outputs["blocking_reason"] == (
        "kr_relationship_resolution_duplicate_relationship"
    )
    assert over_bound.outputs["decision"] == "block"
    assert over_bound.outputs["blocking_reason"] == (
        "kr_relationship_resolution_specs_bound_exceeded"
    )


def test_kr_relationship_readback_action_uses_exact_canonical_evidence() -> None:
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_KR_RELATIONSHIP_READBACK_ID,
        inputs={},
        context={
            "kr_relationship_source_id": "#V#trip_1",
            "kr_relationship_predicate": "#V#has_trip_component",
            "kr_relationship_target_id": "#V#flight_1",
            "kr_relationship_assert_success": True,
            "kr_relationship_source_readback_id": "#V#trip_1",
            "kr_relationship_source_readback_relationships": {
                "#V#has_trip_component": ["#V#flight_1"]
            },
            "kr_relationship_target_readback_id": "#V#flight_1",
        },
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["verified"] is True
    assert result.outputs["verified_relationship"] == {
        "source_id": "#V#trip_1",
        "predicate_id": "#V#has_trip_component",
        "target_id": "#V#flight_1",
    }


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
    assert result.outputs.get("for_each_final_state_counts") == {"start": 2}
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


def test_for_each_action_emits_empty_invocations_for_empty_input() -> None:
    registry = ActionRegistry()
    definitions = {
        "#V#child_each": _child_definition("#V#child_each", "child.emit_item"),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={"workflow_id": "#V#child_each", "items": []},
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["invocations"] == []


def test_for_each_action_respects_partial_success_policy() -> None:
    registry = ActionRegistry()

    def _conditionally_fail(request):
        if request.data.get("current_item") == "bad":
            return WorkflowActionResult(
                status="failed",
                error="child_failed",
                outputs={"failure_reason": "source read-back failed"},
            )
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
    assert result.outputs.get("for_each_final_state_counts") == {"start": 2}
    assert result.outputs.get("successful_results") == [{"item_value": "good"}]
    assert result.outputs.get("iteration_errors") == [
        {
            "index": 1,
            "item": "bad",
            "final_state": "start",
            "error": "child_failed",
            "result": {"failure_reason": "source read-back failed"},
        }
    ]


def test_for_each_action_stops_after_first_error_when_requested() -> None:
    registry = ActionRegistry()
    seen_items: list[str] = []

    def _conditionally_fail(request):
        item = request.data.get("current_item")
        seen_items.append(item)
        if item == "bad":
            return WorkflowActionResult(status="failed", error="child_failed")
        return WorkflowActionResult(outputs={"item_value": item})

    registry.register(
        ActionSpec(action_id="child.maybe_fail", handler=_conditionally_fail)
    )
    definitions = {
        "#V#child_each_fail_fast": _child_definition(
            "#V#child_each_fail_fast",
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
            "workflow_id": "#V#child_each_fail_fast",
            "items": ["good", "bad", "not-attempted"],
            "max_concurrency": 1,
            "success_policy": "all_must_succeed",
            "stop_on_error": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert result.error == "for_each_item_failed:#V#child_each_fail_fast"
    assert seen_items == ["good", "bad"]
    assert result.outputs.get("for_each_item_count") == 2
    assert result.outputs.get("for_each_selected_item_count") == 3
    assert result.outputs.get("for_each_unattempted_count") == 1
    assert result.outputs.get("for_each_success_count") == 1
    assert result.outputs.get("for_each_error_count") == 1
    assert result.outputs.get("for_each_stop_on_error") is True
    assert result.outputs.get("for_each_stopped_on_error") is True
    assert result.outputs.get("for_each_stopped_early") is True
    assert [item["index"] for item in result.outputs["iteration_results"]] == [0, 1]


def test_for_each_action_default_continues_after_child_error() -> None:
    registry = ActionRegistry()
    seen_items: list[str] = []

    def _conditionally_fail(request):
        item = request.data.get("current_item")
        seen_items.append(item)
        if item == "bad":
            return WorkflowActionResult(status="failed", error="child_failed")
        return WorkflowActionResult(outputs={"item_value": item})

    registry.register(
        ActionSpec(action_id="child.maybe_fail", handler=_conditionally_fail)
    )
    definitions = {
        "#V#child_each_default": _child_definition(
            "#V#child_each_default",
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
            "workflow_id": "#V#child_each_default",
            "items": ["good", "bad", "still-attempted"],
            "max_concurrency": 1,
            "success_policy": "all_must_succeed",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert seen_items == ["good", "bad", "still-attempted"]
    assert result.outputs.get("for_each_item_count") == 3
    assert result.outputs.get("for_each_unattempted_count") == 0
    assert result.outputs.get("for_each_stop_on_error") is False
    assert result.outputs.get("for_each_stopped_on_error") is False
    assert result.outputs.get("for_each_stopped_early") is False


def test_for_each_action_rejects_stop_on_error_with_partial_success() -> None:
    registry = ActionRegistry()

    def _unexpected_definition_load(_workflow_id):
        raise AssertionError("invalid fail-fast policy must fail before child lookup")

    register_control_flow_actions(
        registry,
        definition_loader=_unexpected_definition_load,
    )

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={
            "workflow_id": "#V#child_each_invalid_policy",
            "items": ["A", "B"],
            "max_concurrency": 1,
            "success_policy": "allow_partial",
            "stop_on_error": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert result.error == "for_each_stop_on_error_requires_all_must_succeed"
    assert result.outputs.get("for_each_item_count") == 0
    assert result.outputs.get("for_each_success_policy") == "allow_partial"
    assert result.outputs.get("for_each_stop_on_error") is True


def test_for_each_action_rejects_fail_fast_with_concurrent_execution() -> None:
    registry = ActionRegistry()
    seen_items: list[str] = []

    def _emit_item(request):
        seen_items.append(request.data.get("current_item"))
        return WorkflowActionResult(
            outputs={"item_value": request.data.get("current_item")}
        )

    registry.register(ActionSpec(action_id="child.emit_item", handler=_emit_item))
    definitions = {
        "#V#child_each_concurrent": _child_definition(
            "#V#child_each_concurrent",
            "child.emit_item",
        ),
    }
    register_control_flow_actions(
        registry,
        definition_loader=lambda workflow_id: definitions.get(workflow_id),
    )

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
        inputs={
            "workflow_id": "#V#child_each_concurrent",
            "items": ["A", "B"],
            "max_concurrency": 2,
            "stop_on_error": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "failed"
    assert result.error == "for_each_stop_on_error_requires_sequential_execution"
    assert seen_items == []
    assert result.outputs.get("for_each_item_count") == 0
    assert result.outputs.get("for_each_selected_item_count") == 2
    assert result.outputs.get("for_each_unattempted_count") == 2
    assert result.outputs.get("for_each_stopped_on_error") is False


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


def test_context_project_preserves_empty_collections_only_when_requested() -> None:
    registry = _build_context_set_registry()

    default_result = registry.execute(
        "workflow_control.context_project",
        inputs={
            "target_key": "projected_item",
            "required_fields": ["candidates"],
            "field_sources": {"candidates": []},
            "include_missing_fields": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )
    explicit_result = registry.execute(
        "workflow_control.context_project",
        inputs={
            "target_key": "projected_item",
            "required_fields": ["candidates"],
            "field_sources": {"candidates": []},
            "include_missing_fields": True,
            "include_empty_fields": True,
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert default_result.outputs["projected_item"] == {
        "_missing_fields": ["candidates"]
    }
    assert explicit_result.outputs["projected_item"] == {"candidates": []}
    projection = explicit_result.outputs["projected_item_projection"]
    assert projection["selected_fields"] == ["candidates"]
    assert projection["missing_fields"] == []
    assert projection["include_empty_fields"] is True


def _relationship_effect_invocation(
    *,
    target: str = "#V#meeting_1",
    success: bool = True,
    source: str = "#V#file_copy_1",
    predicate: str = "#V#documentary_evidence_for",
    added: bool = True,
    changed: bool = True,
) -> dict[str, object]:
    return {
        "tool": "add_relationship",
        "status": "ok" if success else "failed",
        "payload": {
            "source_id": source,
            "predicate": predicate,
            "target": target,
        },
        "effective_arguments": {
            "source_id": source,
            "predicate": predicate,
            "target": target,
        },
        "effective_payload": {
            "success": success,
            "effect_status": "succeeded" if success else "failed",
            "relationship_type": "concept_relation",
            "source_id": source,
            "predicate": predicate,
            "predicate_input": predicate,
            "target": target,
            "added": added,
            "changed": changed,
        },
    }


def _relationship_effect_hit(
    *,
    target: str = "#V#meeting_1",
    source: str = "#V#file_copy_1",
    predicate: str = "#V#documentary_evidence_for",
) -> dict[str, object]:
    return {
        "access_granted": True,
        "is_asserted": True,
        "relation_state": "asserted",
        "source_concept_id": source,
        "predicate_concept_id": predicate,
        "target_value": target,
        "relation_kind": "binary",
        "argument_indexes": [1],
        "canonical_publication": True,
        "relation_metadata": {
            "canonical_publication": True,
            "relation_id": "struct::exact",
        },
    }


def _relationship_effect_readback_result(
    *,
    invocations: list[dict[str, object]] | None,
    hits: list[dict[str, object]],
    relationship_effect_receipt: dict[str, object] | None = None,
    readback_concept_id: str = "#V#file_copy_1",
    readback_total_hits: int | None = None,
    readback_total_hits_is_lower_bound: bool = False,
    allow_multiple_targets: bool | None = None,
    minimum_unique_targets: int | None = None,
):
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)
    inputs = {
        "mutation_tool_name": "add_relationship",
        "expected_source_id": "#V#file_copy_1",
        "expected_predicate_id": "#V#documentary_evidence_for",
        "expected_relation_kind": "binary",
        "tool_invocations": invocations,
        "relationship_effect_receipt": relationship_effect_receipt,
        "readback_concept_id": readback_concept_id,
        "readback_total_hits": (
            len(hits) if readback_total_hits is None else readback_total_hits
        ),
        "readback_total_hits_is_lower_bound": readback_total_hits_is_lower_bound,
        "readback_hits": hits,
    }
    if allow_multiple_targets is not None:
        inputs["allow_multiple_targets"] = allow_multiple_targets
    if minimum_unique_targets is not None:
        inputs["minimum_unique_targets"] = minimum_unique_targets
    return registry.execute(
        WORKFLOW_CONTROL_ACTION_RELATIONSHIP_EFFECT_READBACK_ID,
        inputs=inputs,
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )


def test_relationship_effect_readback_binds_successful_write_to_exact_hit() -> None:
    result = _relationship_effect_readback_result(
        invocations=[
            _relationship_effect_invocation(success=False),
            _relationship_effect_invocation(),
        ],
        hits=[
            _relationship_effect_hit(target="#V#different_meeting"),
            _relationship_effect_hit(),
        ],
    )

    assert result.status == "success"
    assert result.outputs["relationship_effect_readback_verified"] is True
    assert result.outputs["relationship_effect_readback_failure_code"] is None
    assert result.outputs["represented_target_concept_id"] == "#V#meeting_1"
    assert result.outputs["verified_relationship"] == {
        "source_id": "#V#file_copy_1",
        "predicate_id": "#V#documentary_evidence_for",
        "target_id": "#V#meeting_1",
        "relation_kind": "binary",
        "relation_id": "struct::exact",
    }
    assert result.outputs["verified_relationships"] == [
        result.outputs["verified_relationship"]
    ]


def test_relationship_effect_readback_action_accepts_deterministic_receipt() -> None:
    result = _relationship_effect_readback_result(
        invocations=None,
        relationship_effect_receipt=_relationship_effect_invocation(),
        hits=[_relationship_effect_hit()],
    )

    assert result.status == "success"
    assert result.outputs["relationship_effect_readback_verified"] is True
    assert result.outputs["represented_target_concept_id"] == "#V#meeting_1"


def test_relationship_effect_readback_rejects_wrong_target_only() -> None:
    result = _relationship_effect_readback_result(
        invocations=[_relationship_effect_invocation()],
        hits=[_relationship_effect_hit(target="#V#different_meeting")],
    )

    assert result.status == "success"
    assert result.outputs["relationship_effect_readback_verified"] is False
    assert (
        result.outputs["relationship_effect_readback_failure_code"]
        == "relationship_effect_exact_readback_missing"
    )


def test_relationship_effect_readback_rejects_ambiguous_successful_targets() -> None:
    result = _relationship_effect_readback_result(
        invocations=[
            _relationship_effect_invocation(target="#V#meeting_1"),
            _relationship_effect_invocation(target="#V#meeting_2"),
        ],
        hits=[
            _relationship_effect_hit(target="#V#meeting_1"),
            _relationship_effect_hit(target="#V#meeting_2"),
        ],
    )

    assert result.status == "success"
    assert result.outputs["relationship_effect_readback_verified"] is False
    assert (
        result.outputs["relationship_effect_readback_failure_code"]
        == "relationship_effect_successful_mutation_ambiguous"
    )


def test_relationship_effect_readback_action_supports_explicit_multi_target_mode() -> (
    None
):
    result = _relationship_effect_readback_result(
        invocations=[
            _relationship_effect_invocation(target="#V#person_1"),
            _relationship_effect_invocation(target="#V#person_2"),
        ],
        hits=[
            _relationship_effect_hit(target="#V#person_2"),
            _relationship_effect_hit(target="#V#person_1"),
        ],
        allow_multiple_targets=True,
        minimum_unique_targets=2,
    )

    assert result.status == "success"
    assert result.outputs["relationship_effect_readback_verified"] is True
    assert result.outputs["represented_target_concept_id"] is None
    assert result.outputs["verified_relationship"] is None
    assert [row["target_id"] for row in result.outputs["verified_relationships"]] == [
        "#V#person_1",
        "#V#person_2",
    ]


def test_relationship_effect_readback_accepts_idempotent_existing_edge_receipt() -> (
    None
):
    fresh_write = _relationship_effect_readback_result(
        invocations=[_relationship_effect_invocation()],
        hits=[_relationship_effect_hit()],
    )
    idempotent_rerun = _relationship_effect_readback_result(
        invocations=[
            _relationship_effect_invocation(added=False, changed=False),
        ],
        hits=[_relationship_effect_hit()],
    )

    assert fresh_write.outputs["relationship_effect_readback_verified"] is True
    assert idempotent_rerun.outputs == fresh_write.outputs


def test_relationship_effect_readback_does_not_treat_empty_query_as_effect() -> None:
    result = _relationship_effect_readback_result(
        invocations=[_relationship_effect_invocation()],
        hits=[],
    )

    assert result.status == "success"
    assert result.outputs["relationship_effect_readback_verified"] is False
    assert (
        result.outputs["relationship_effect_readback_failure_code"]
        == "relationship_effect_exact_readback_missing"
    )
    assert result.outputs["represented_target_concept_id"] == "#V#meeting_1"


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
