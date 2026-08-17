"""Contract validation tests for workflow definition identity service."""

from __future__ import annotations

from src.backend.workflows.engine import (
    WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC,
    WORKFLOW_STEP_EXECUTION_MODE_LLM,
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowStateSpec,
    WorkflowTransitionSpec,
)
from src.backend.workflows.subworkflow_contracts import (
    WORKFLOW_SUBWORKFLOW_ACTION_ID,
    build_subworkflow_contract,
)
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_BREAK_ID,
    WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
    WORKFLOW_CONTROL_ACTION_FORK_ID,
    WORKFLOW_CONTROL_ACTION_JOIN_ID,
)
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


def test_validate_contract_allows_terminal_failed_sink_without_actions() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#test_terminal_sink_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id="test.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="failed",
                        condition=lambda _ctx: True,
                        reason="forced_failure",
                    ),
                ),
            ),
            "failed": WorkflowStateSpec(state_id="failed", terminal=True),
        },
        termination_states=("failed",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids={"test.action"},
        enforce_supported_actions=True,
    )

    assert validation["valid"] is True
    assert "workflow_step_contract_vacuous" not in (validation.get("errors") or [])
    assert validation.get("vacuous_state_ids") == []


def test_validate_contract_rejects_non_terminal_vacuous_state() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#test_vacuous_state_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="complete",
                        condition=lambda _ctx: True,
                        reason="noop",
                    ),
                ),
            ),
            "complete": WorkflowStateSpec(state_id="complete", terminal=True),
        },
        termination_states=("complete",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_step_contract_vacuous" in (validation.get("errors") or [])
    assert validation.get("vacuous_state_ids") == ["start"]


def test_validate_contract_rejects_prompt_sentence_like_workflow_id() -> None:
    definition = WorkflowDefinition(
        workflow_id=(
            "#V#the_enrichment_workflow_isn_t_the_right_one_we_need_a_new_"
            "vontology_search_workflow_i_suspect_manually_retrieve_the_"
            "v_timothy_pistotti_concept_and_then_look_at_its_types_and_"
            "relations_in_particular_ones_about_supervision_workflow"
        ),
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_id_invalid" in (validation.get("errors") or [])
    reason_codes = {
        issue.get("reason_code") for issue in validation.get("workflow_id_issues", [])
    }
    assert "workflow_id_slug_too_long" in reason_codes
    assert "workflow_id_too_many_tokens" in reason_codes
    assert "workflow_id_prompt_sentence_like" in reason_codes


def test_validate_contract_rejects_actionless_output_only_contract() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#test_output_only_state_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                metadata={
                    "writes_context_keys": [
                        "#V#workflow_context_key_validated_type_name"
                    ]
                },
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="complete",
                        condition=lambda _ctx: True,
                        reason="noop",
                    ),
                ),
            ),
            "complete": WorkflowStateSpec(state_id="complete", terminal=True),
        },
        termination_states=("complete",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_step_contract_vacuous" in (validation.get("errors") or [])
    assert validation.get("vacuous_state_ids") == ["start"]


def test_validate_contract_rejects_transient_execution_defaults_in_static_inputs() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#transient_input_invalid_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id="candidate.execute",
                        inputs={
                            "candidate_request_text": "Run this candidate.",
                            "prompt_concept_id": "#V#candidate_execution_prompt",
                        },
                    ),
                ),
                terminal=True,
            ),
        },
        termination_states=("start",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_transient_execution_input_invalid" in (
        validation.get("errors") or []
    )
    assert validation.get("transient_execution_input_issues") == [
        {
            "state_id": "start",
            "action_id": "candidate.execute",
            "tool_param": "candidate_request_text",
            "reason_code": "transient_request_text_default_persisted",
        }
    ]


def test_validate_contract_allows_transient_execution_keys_when_context_mapped() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#transient_input_context_mapped_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(
                    WorkflowActionInvocation(
                        action_id="candidate.execute",
                        inputs={
                            "candidate_request_text": {
                                "$context_key": "candidate_request_text"
                            },
                            "candidate_base_response_text": {
                                "$context_key": "candidate_base_response_text"
                            },
                        },
                    ),
                ),
                terminal=True,
            ),
        },
        termination_states=("start",),
        purpose="test",
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert validation.get("transient_execution_input_issues") == []


def _child_workflow_definition(
    *,
    workflow_id: str = "#V#child_workflow",
    required_inputs: tuple[str, ...] = ("child_input",),
    produced_outputs: tuple[str, ...] = ("child_output",),
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=workflow_id,
        initial_state="child_start",
        states={
            "child_start": WorkflowStateSpec(
                state_id="child_start",
                actions=(WorkflowActionInvocation(action_id="child.action"),),
                terminal=True,
                metadata={
                    "reads_context_keys": list(required_inputs),
                    "writes_context_keys": list(produced_outputs),
                },
            ),
        },
        termination_states=("child_start",),
        purpose="child",
    )


def _parent_with_subworkflow_contract(
    *,
    child_workflow_id: str = "#V#child_workflow",
    input_mappings: list[dict[str, str]] | None = None,
    output_mappings: list[dict[str, str]] | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="#V#parent_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID),),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id=child_workflow_id,
                        input_mappings=input_mappings
                        or [
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=output_mappings
                        or [
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            ),
        },
        termination_states=("start",),
        purpose="parent",
    )


def test_validate_contract_accepts_compatible_subworkflow_contract() -> None:
    child = _child_workflow_definition()
    parent = _parent_with_subworkflow_contract()

    validation = validate_workflow_definition_contract(
        definition=parent,
        known_workflow_ids={child.workflow_id},
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is True
    assert validation["subworkflow_contract_issues"] == []
    assert "workflow_subworkflow_unresolved" not in (validation.get("errors") or [])


def test_validate_contract_accepts_nested_path_below_declared_subworkflow_output() -> None:
    child = _child_workflow_definition(produced_outputs=("candidate_context",))
    parent = _parent_with_subworkflow_contract(
        output_mappings=[
            {
                "child_output_field": (
                    "candidate_context.candidate_snapshot.parent_release.release_sha256"
                ),
                "parent_context_key": "expected_baseline_release_sha256",
            }
        ]
    )

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is True
    assert validation["subworkflow_contract_issues"] == []


def test_validate_contract_accepts_dynamic_subworkflow_contract() -> None:
    dynamic_parent = WorkflowDefinition(
        workflow_id="#V#dynamic_parent_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID),),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id_context_key="selected_workflow_id",
                        input_mappings=[
                            {
                                "child_input_key": "workflow_id",
                                "parent_context_key": "selected_workflow_id",
                            },
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            },
                        ],
                        output_mappings=[
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                    )
                },
            ),
        },
        termination_states=("start",),
        purpose="dynamic parent",
    )

    validation = validate_workflow_definition_contract(definition=dynamic_parent)

    assert validation["valid"] is True
    assert validation["subworkflow_contract_issues"] == []
    assert "workflow_subworkflow_contract_invalid" not in (
        validation.get("errors") or []
    )


def test_validate_contract_accepts_static_subworkflow_inputs_as_provided_inputs() -> None:
    child = _child_workflow_definition(
        required_inputs=("child_input", "verification_profile")
    )
    parent = WorkflowDefinition(
        workflow_id="#V#static_parent_workflow",
        initial_state="start",
        states={
            "start": WorkflowStateSpec(
                state_id="start",
                actions=(WorkflowActionInvocation(action_id=WORKFLOW_SUBWORKFLOW_ACTION_ID),),
                terminal=True,
                metadata={
                    "subworkflow_contract": build_subworkflow_contract(
                        workflow_id=child.workflow_id,
                        input_mappings=[
                            {
                                "child_input_key": "child_input",
                                "parent_context_key": "parent_input",
                            }
                        ],
                        output_mappings=[
                            {
                                "child_output_field": "child_output",
                                "parent_context_key": "parent_output",
                            }
                        ],
                        static_input_keys=["verification_profile"],
                    )
                },
            ),
        },
        termination_states=("start",),
        purpose="static parent",
    )

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is True
    assert validation["subworkflow_contract_issues"] == []


def test_validate_contract_reports_unresolved_subworkflow_workflow() -> None:
    parent = _parent_with_subworkflow_contract(child_workflow_id="#V#missing_child")

    validation = validate_workflow_definition_contract(
        definition=parent,
        known_workflow_ids={"#V#known_child"},
        workflow_definition_loader=lambda _workflow_id: None,
    )

    assert validation["valid"] is False
    assert "workflow_subworkflow_unresolved" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "subworkflow_workflow_not_found"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_reports_subworkflow_input_contract_mismatch() -> None:
    child = _child_workflow_definition(required_inputs=("required_child_input",))
    parent = _parent_with_subworkflow_contract(
        input_mappings=[
            {
                "child_input_key": "other_input",
                "parent_context_key": "parent_input",
            }
        ]
    )

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is False
    assert "workflow_subworkflow_contract_mismatch" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "subworkflow_input_contract_mismatch"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_reports_subworkflow_output_contract_mismatch() -> None:
    child = _child_workflow_definition(produced_outputs=("known_child_output",))
    parent = _parent_with_subworkflow_contract(
        output_mappings=[
            {
                "child_output_field": "unknown_output",
                "parent_context_key": "parent_output",
            }
        ]
    )

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: (
            child if workflow_id == child.workflow_id else None
        ),
    )

    assert validation["valid"] is False
    assert "workflow_subworkflow_contract_mismatch" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "subworkflow_output_contract_mismatch"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_rejects_recursive_subworkflow_reference() -> None:
    parent = _parent_with_subworkflow_contract(child_workflow_id="#V#parent_workflow")

    validation = validate_workflow_definition_contract(definition=parent)

    assert validation["valid"] is False
    assert any(
        issue.get("reason_code") == "subworkflow_recursive_self_reference"
        for issue in validation["subworkflow_contract_issues"]
    )


def test_validate_contract_rejects_break_without_loop_scope_and_route() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#break_invalid_workflow",
        initial_state="loop",
        states={
            "loop": WorkflowStateSpec(
                state_id="loop",
                actions=(
                    WorkflowActionInvocation(action_id=WORKFLOW_CONTROL_ACTION_BREAK_ID),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_control_signal_invalid" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "break_outside_loop_scope"
        for issue in validation.get("control_signal_issues", [])
    )
    assert any(
        issue.get("reason_code") == "break_transition_missing"
        for issue in validation.get("control_signal_issues", [])
    )


def test_validate_contract_rejects_join_without_matching_fork() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#join_invalid_workflow",
        initial_state="join_state",
        states={
            "join_state": WorkflowStateSpec(
                state_id="join_state",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_CONTROL_ACTION_JOIN_ID,
                        inputs={"fork_id": "missing_fork"},
                    ),
                ),
                terminal=True,
            )
        },
        termination_states=("join_state",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_fork_join_invalid" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "join_without_matching_fork"
        for issue in validation.get("fork_join_issues", [])
    )


def test_validate_contract_reports_prompt_contract_errors() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#prompt_contract_invalid_workflow",
        initial_state="prompted",
        states={
            "prompted": WorkflowStateSpec(
                state_id="prompted",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        inputs={
                            "__prompt_resolution_diagnostics": {
                                "errors": ["prompt_text_missing_or_empty"],
                                "warnings": [],
                            }
                        },
                    ),
                ),
                terminal=True,
                metadata={
                    "prompt_contract": {
                        "validation_policy": "fail",
                        "requested_prompt_concept_ids": ["#V#missing_prompt"],
                        "resolved_prompt_concept_id": "#V#missing_prompt",
                    }
                },
            ),
        },
        termination_states=("prompted",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_prompt_contract_invalid" in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "prompt_text_missing_or_empty"
        and issue.get("severity") == "error"
        for issue in validation.get("prompt_contract_issues", [])
    )


def test_validate_contract_keeps_prompt_contract_warnings_non_blocking() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#prompt_contract_warning_workflow",
        initial_state="prompted",
        states={
            "prompted": WorkflowStateSpec(
                state_id="prompted",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                        inputs={
                            "__prompt_resolution_diagnostics": {
                                "errors": [],
                                "warnings": [
                                    "prompt_allowed_tools_unavailable:missing.tool"
                                ],
                            }
                        },
                    ),
                ),
                terminal=True,
                metadata={
                    "prompt_contract": {
                        "validation_policy": "warn",
                        "requested_prompt_concept_ids": ["#V#prompt"],
                        "resolved_prompt_concept_id": "#V#prompt",
                    }
                },
            ),
        },
        termination_states=("prompted",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert "workflow_prompt_contract_invalid" not in (validation.get("errors") or [])
    assert any(
        issue.get("reason_code") == "prompt_allowed_tools_unavailable:missing.tool"
        and issue.get("severity") == "warning"
        for issue in validation.get("prompt_contract_issues", [])
    )


def test_validate_contract_allows_deterministic_prompt_authority_actions() -> None:
    prompt_contract = {
        "validation_policy": "fail",
        "requested_prompt_concept_ids": ["#V#template_prompt"],
        "resolved_prompt_concept_id": "#V#template_prompt",
    }
    definition = WorkflowDefinition(
        workflow_id="#V#deterministic_prompt_authority_workflow",
        initial_state="render_context",
        states={
            "render_context": WorkflowStateSpec(
                state_id="render_context",
                actions=(
                    WorkflowActionInvocation(
                        action_id="render.context.template",
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_DETERMINISTIC,
                        prompt_contract=prompt_contract,
                    ),
                ),
                terminal=True,
                metadata={"prompt_contract": prompt_contract},
            ),
        },
        termination_states=("render_context",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert "workflow_prompt_contract_invalid" not in (validation.get("errors") or [])
    assert validation.get("prompt_contract_issues") == []


def test_validate_contract_treats_llm_execution_mode_as_supported_without_registry_action() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#llm_step_workflow",
        initial_state="prompted",
        states={
            "prompted": WorkflowStateSpec(
                state_id="prompted",
                actions=(
                    WorkflowActionInvocation(
                        action_id="llm.action",
                        execution_mode=WORKFLOW_STEP_EXECUTION_MODE_LLM,
                    ),
                ),
                terminal=True,
                metadata={
                    "prompt_contract": {
                        "validation_policy": "warn",
                        "requested_prompt_concept_ids": ["#V#prompt"],
                        "resolved_prompt_concept_id": "#V#prompt",
                    }
                },
            ),
        },
        termination_states=("prompted",),
    )

    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=set(),
        enforce_supported_actions=True,
    )

    assert validation["valid"] is True
    assert validation.get("unsupported_action_ids") == []
    assert "unsupported_workflow_actions" not in (validation.get("errors") or [])


def test_validate_contract_reports_invalid_checkpoint_policy() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#checkpoint_policy_invalid_workflow",
        initial_state="dispatch",
        states={
            "dispatch": WorkflowStateSpec(
                state_id="dispatch",
                actions=(WorkflowActionInvocation(action_id="dispatch.action"),),
                terminal=True,
                metadata={
                    "checkpoint_policy": {
                        "schema_version": "workflow_step_checkpoint_policy.v1",
                        "plan_item_updates": [
                            {"item_id": "dispatch", "status": "invalid_status"}
                        ],
                    }
                },
            ),
        },
        termination_states=("dispatch",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_plan_state_policy_invalid" in (validation.get("errors") or [])
    assert any(
        issue.get("state_id") == "dispatch"
        and issue.get("reason_code") == "plan_item_update_status_0_invalid"
        for issue in validation.get("plan_state_issues", [])
    )


def test_validate_contract_reports_invalid_mutation_authority() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#mutation_authority_invalid_workflow",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="write.action"),),
                terminal=True,
                metadata={
                    "mutation_authority": {
                        "schema_version": "workflow_step_mutation_authority.v1",
                        "maximum_level": "unguarded_everything",
                    }
                },
            ),
        },
        termination_states=("write",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_runtime_policy_invalid" in (validation.get("errors") or [])
    assert any(
        issue.get("state_id") == "write"
        and issue.get("reason_code") == "mutation_authority_invalid"
        for issue in validation.get("runtime_policy_issues", [])
    )


def test_validate_contract_accepts_context_only_completion_gate() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#completion_gate_context_only_workflow",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        metadata={
            "completion_gate": {
                "schema_version": "workflow_completion_gate.v1",
                "required_context_keys": ["deliverable_ready"],
                "require_declared_plan_items_done": False,
            }
        },
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert validation.get("completion_gate_issues") == []


def test_validate_contract_accepts_terminal_success_contract() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#terminal_success_contract_workflow",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        metadata={
            "terminal_success_contract": {
                "schema_version": "workflow_terminal_success_contract.v1",
                "success_statuses": ["completed"],
                "required_summary_fields": [
                    "workflow_id",
                    "terminal_status",
                    "final_state",
                    "completed",
                ],
            }
        },
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert validation.get("terminal_success_contract_issues") == []


def test_validate_contract_accepts_required_effects_contract() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#required_effects_contract_workflow",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        metadata={
            "required_effects_contract": {
                "schema_version": "workflow_required_effects_contract.v1",
                "contract_id": "conversation_diagnostics",
                "required_effects": [
                    {
                        "effect_id": "locator",
                        "effect_type": "diagnostic_evidence",
                        "required_tools": ["conversation_telemetry_get_locator"],
                    }
                ],
            }
        },
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert validation.get("required_effects_contract_issues") == []


def test_validate_contract_rejects_invalid_terminal_success_contract() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#terminal_success_contract_invalid_workflow",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        metadata={"terminal_success_contract": "invalid"},
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_terminal_success_contract_invalid" in (
        validation.get("errors") or []
    )


def test_validate_contract_rejects_invalid_failed_terminal_error_code() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#terminal_success_contract_invalid_error_code_workflow",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        metadata={
            "terminal_success_contract": {
                "schema_version": "workflow_terminal_success_contract.v1",
                "failed_terminal_error_code": {"not": "a string"},
            }
        },
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert validation["terminal_success_contract_issues"] == [
        {
            "scope": "workflow",
            "reason_code": (
                "workflow_terminal_success_contract_"
                "failed_terminal_error_code_invalid"
            ),
        }
    ]



def test_validate_contract_rejects_invalid_required_effects_contract() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#required_effects_contract_invalid_workflow",
        initial_state="done",
        states={
            "done": WorkflowStateSpec(
                state_id="done",
                actions=(WorkflowActionInvocation(action_id="mark.done"),),
                terminal=True,
            ),
        },
        termination_states=("done",),
        metadata={"required_effects_contract": "invalid"},
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_required_effects_contract_invalid" in (
        validation.get("errors") or []
    )


def test_validate_contract_accepts_join_with_declared_fork() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#join_valid_workflow",
        initial_state="fork_state",
        states={
            "fork_state": WorkflowStateSpec(
                state_id="fork_state",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_CONTROL_ACTION_FORK_ID,
                        inputs={"fork_id": "main"},
                    ),
                ),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="join_state",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
            ),
            "join_state": WorkflowStateSpec(
                state_id="join_state",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_CONTROL_ACTION_JOIN_ID,
                        inputs={"fork_id": "main"},
                    ),
                ),
                terminal=True,
            ),
        },
        termination_states=("join_state",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is True
    assert validation.get("fork_join_issues") == []


def test_validate_contract_detects_recursive_subworkflow_cycle() -> None:
    parent = _parent_with_subworkflow_contract(child_workflow_id="#V#child")
    child = _parent_with_subworkflow_contract(child_workflow_id="#V#parent_workflow")
    child = WorkflowDefinition(
        workflow_id="#V#child",
        initial_state=child.initial_state,
        states=child.states,
        termination_states=child.termination_states,
        purpose="child",
    )
    loader_map = {
        "#V#child": child,
        "#V#parent_workflow": parent,
    }

    validation = validate_workflow_definition_contract(
        definition=parent,
        workflow_definition_loader=lambda workflow_id: loader_map.get(workflow_id),
    )

    assert validation["valid"] is False
    assert any(
        issue.get("reason_code") == "subworkflow_recursive_cycle"
        for issue in validation.get("subworkflow_contract_issues", [])
    )


def test_validate_contract_rejects_for_each_without_required_inputs() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#for_each_invalid_workflow",
        initial_state="fan_out",
        states={
            "fan_out": WorkflowStateSpec(
                state_id="fan_out",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
                        inputs={"success_policy": "all_must_succeed"},
                    ),
                ),
                terminal=True,
            )
        },
        termination_states=("fan_out",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_iterator_invalid" in (validation.get("errors") or [])
    iterator_issues = validation.get("iterator_issues") or []
    assert any(
        issue.get("reason_code") == "for_each_workflow_id_missing"
        for issue in iterator_issues
    )
    assert any(
        issue.get("reason_code") == "for_each_items_missing"
        for issue in iterator_issues
    )


def test_validate_contract_accepts_for_each_items_context_mapping() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#for_each_mapped_items_workflow",
        initial_state="fan_out",
        states={
            "fan_out": WorkflowStateSpec(
                state_id="fan_out",
                actions=(
                    WorkflowActionInvocation(
                        action_id=WORKFLOW_CONTROL_ACTION_FOR_EACH_ID,
                        inputs={
                            "workflow_id": "#V#child_workflow",
                            "items": {"$context_key": "candidate_records"},
                            "success_policy": "allow_partial",
                        },
                    ),
                ),
                terminal=True,
            )
        },
        termination_states=("fan_out",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert "workflow_iterator_invalid" not in (validation.get("errors") or [])
    assert validation.get("iterator_issues") == []


def test_validate_contract_rejects_approval_gate_without_route() -> None:
    definition = WorkflowDefinition(
        workflow_id="#V#approval_invalid_workflow",
        initial_state="write",
        states={
            "write": WorkflowStateSpec(
                state_id="write",
                actions=(WorkflowActionInvocation(action_id="write.action"),),
                transitions=(
                    WorkflowTransitionSpec(
                        to_state="done",
                        condition=lambda _ctx: True,
                        reason="next_step",
                    ),
                ),
                metadata={
                    "approval_gate": {
                        "schema_version": "workflow_step_approval_gate.v1",
                        "approval_context_key": "write_approved",
                    }
                },
            ),
            "done": WorkflowStateSpec(state_id="done", terminal=True),
        },
        termination_states=("done",),
    )

    validation = validate_workflow_definition_contract(definition=definition)

    assert validation["valid"] is False
    assert "workflow_approval_gate_invalid" in (validation.get("errors") or [])
    approval_gate_issues = validation.get("approval_gate_issues") or []
    assert any(
        issue.get("reason_code") == "approval_transition_missing"
        for issue in approval_gate_issues
    )
