from __future__ import annotations

import copy
from typing import Any

from src.backend.services.failure_case_intake_service import (
    FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
)
from src.backend.services.failure_case_prompt_replay_experiment_service import (
    FAILURE_CASE_PROMPT_REPLAY_FIXTURE_SCHEMA_VERSION,
    FAILURE_CASE_PROMPT_REPLAY_PREPARE_ACTION_ID,
    FAILURE_CASE_PROMPT_REPLAY_RECORD_OBSERVATIONS_ACTION_ID,
    prepare_failure_case_prompt_replay_experiment,
)
from src.backend.services.testing_workflow_contracts import (
    EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.failure_case_prompt_improvement_actions import (
    register_failure_case_prompt_improvement_actions,
)
from src.backend.workflows.engine import WorkflowExecutor
from src.backend.workflows.workflow_authoring_service import (
    build_workflow_definition_from_authoring_spec,
)
from src.backend.workflows.workflow_definition_identity_service import (
    validate_workflow_definition_contract,
)


def _sample_failure_case_intake() -> dict[str, Any]:
    return {
        "schema_version": "failure_case_intake.v1",
        "success": True,
        "failure_case_intake_collected": True,
        "request_id": "req-failure-1",
        "turn": {
            "request_id": "req-failure-1",
            "prompt": {"text": "Summarise the latest lab task."},
            "user_visible_response": {"text": "I could not find it."},
        },
        "model": {
            "target_model": "gpt-5.4-mini",
            "comparator_model": "gpt-5.4",
            "primary_model": "gpt-5.4-mini",
        },
        "workflow": {
            "selected_workflow_id": "#V#lab_task_review_workflow",
            "stage_id": "#V#lab_task_review_answer_prompt_stage",
        },
        "prompt_metadata": {
            "prompt_ids": ["#V#lab_task_review_answer_prompt"],
            "prompt_variant_selections": [
                {
                    "base_prompt_concept_id": "#V#lab_task_review_answer_prompt",
                    "selected_prompt_concept_id": "#V#lab_task_review_answer_prompt",
                }
            ],
        },
        "telemetry": {"evidence_refs": [{"tool": "turn_execution_get"}]},
        "policy_boundary": {
            "classification_performed": False,
            "prompt_hypothesis_generated": False,
            "promotion_recommendation_generated": False,
        },
    }


def test_prepare_failure_case_prompt_replay_experiment_builds_spec_inputs() -> None:
    payload = prepare_failure_case_prompt_replay_experiment(
        failure_case_intake=_sample_failure_case_intake(),
        prompt_variant_ids=[
            "#V#lab_task_review_answer_prompt_variant_a",
            "#V#lab_task_review_answer_prompt_variant_a",
            "#V#lab_task_review_answer_prompt_variant_b",
        ],
        replay_policy={"replay_set": "failure_case_single_turn"},
        promotion_policy={"requires": "human_review_after_replay_pass"},
        verdict_rules={"minimum_pass_count": 2},
        expected_outcomes=["Replay reproduces the failed request."],
        metadata={"authored_by_workflow": True},
    )

    assert payload["success"] is True
    assert payload["ready_for_prompt_variant_replay"] is True
    assert payload["missing_replay_inputs"] == []
    assert payload["base_prompt_id"] == "#V#lab_task_review_answer_prompt"
    assert payload["candidate_prompt_variant_ids"] == [
        "#V#lab_task_review_answer_prompt_variant_a",
        "#V#lab_task_review_answer_prompt_variant_b",
    ]
    assert [arm["candidate_prompt_variant_id"] for arm in payload["replay_arm_plan"]] == [
        None,
        "#V#lab_task_review_answer_prompt_variant_a",
        "#V#lab_task_review_answer_prompt_variant_b",
        None,
        "#V#lab_task_review_answer_prompt_variant_a",
        "#V#lab_task_review_answer_prompt_variant_b",
    ]
    spec_inputs = payload["experiment_create_spec_inputs"]
    assert spec_inputs["target_workflow_ids"] == ["#V#lab_task_review_workflow"]
    assert payload["experiment_turn_execution_request_ids"] == ["req-failure-1"]
    assert spec_inputs["fixture_payload"]["schema_version"] == (
        FAILURE_CASE_PROMPT_REPLAY_FIXTURE_SCHEMA_VERSION
    )
    assert spec_inputs["fixture_payload"]["failure_case_replay_case"][
        "failure_request_id"
    ] == "req-failure-1"
    assert spec_inputs["metadata"]["workflow_authored_metadata"] == {
        "authored_by_workflow": True
    }
    assert payload["policy_boundary"] == {
        "classification_performed": False,
        "prompt_hypothesis_generated": False,
        "prompt_body_generated": False,
        "replay_scored": False,
        "promotion_recommendation_generated": False,
        "experiment_spec_persisted": False,
        "reason": (
            "This helper only shapes replay experiment inputs. Failure "
            "classification, prompt-candidate authoring, scoring, and promotion "
            "remain represented workflow/prompt policy."
        ),
    }


def test_replay_uses_target_turn_applied_prompt_before_generic_prompt_metadata() -> (
    None
):
    intake = _sample_failure_case_intake()
    intake["prompt_metadata"]["applied_prompt_ids"] = [
        "#V#historically_applied_prompt"
    ]
    intake["prompt_metadata"]["prompt_variant_selections"] = []

    payload = prepare_failure_case_prompt_replay_experiment(
        failure_case_intake=intake,
        prompt_variant_ids=["#V#candidate_variant"],
    )

    assert payload["base_prompt_id"] == "#V#historically_applied_prompt"


def test_prompt_replay_prepare_action_uses_accumulated_failure_context() -> None:
    registry = ActionRegistry()
    register_failure_case_prompt_improvement_actions(registry)

    result = registry.execute(
        FAILURE_CASE_PROMPT_REPLAY_PREPARE_ACTION_ID,
        inputs={
            "prompt_variant_ids": ["#V#variant_a"],
            "promotion_policy": {"requires": "represented_gate"},
        },
        context=_sample_failure_case_intake(),
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["failure_request_id"] == "req-failure-1"
    assert result.outputs["experiment_promotion_policy"] == {
        "requires": "represented_gate"
    }
    assert result.outputs["policy_boundary"]["prompt_body_generated"] is False


def test_prompt_replay_record_observations_action_uses_represented_scores(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import failure_case_prompt_improvement_actions

    captured: dict[str, Any] = {}

    def fake_record_experiment_observations(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "success": True,
            "run_id": kwargs["run_id"],
            "recorded_observation_count": 1,
            "turn_execution_request_ids": ["req-arm-1"],
        }

    monkeypatch.setattr(
        failure_case_prompt_improvement_actions,
        "record_experiment_observations",
        fake_record_experiment_observations,
    )
    registry = ActionRegistry()
    register_failure_case_prompt_improvement_actions(registry)

    result = registry.execute(
        FAILURE_CASE_PROMPT_REPLAY_RECORD_OBSERVATIONS_ACTION_ID,
        inputs={"require_represented_evaluation": True},
        context={
            "run_id": "#V#run_1",
            "replay_set_id": "#V#replay_set",
            "prompt_replay_arm_summaries": [
                {
                    "arm": {"arm_id": "arm_1"},
                    "represented_replay_evaluation": {"verdict": "pass"},
                }
            ],
        },
        env=WorkflowEnvironment(llm_client=None, gateway="fake-gateway"),
    )

    assert result.status == "success"
    assert result.outputs["prompt_replay_observations_recorded"] is True
    assert result.outputs["policy_boundary"] == {
        "prompt_body_generated": False,
        "replay_scored": False,
        "promotion_recommendation_generated": False,
        "experiment_observation_persisted": True,
        "reason": (
            "This action converts already-scored replay arm summaries into "
            "experiment observations. Prompt hypotheses, represented replay "
            "evaluation, and promotion decisions remain workflow/Vontology "
            "authority."
        ),
    }
    assert captured["run_id"] == "#V#run_1"
    assert captured["default_replay_set_id"] == "#V#replay_set"
    assert captured["gateway"] == "fake-gateway"
    assert captured["require_represented_evaluation"] is True
    assert captured["arm_summaries"][0]["arm"]["arm_id"] == "arm_1"


def test_failure_case_workflow_can_start_record_and_score_prompt_replay_spec(
    monkeypatch,
) -> None:
    from src.backend.workflows.durable import failure_case_prompt_improvement_actions

    sample_intake = _sample_failure_case_intake()

    def fake_collect_failure_case_intake(**_kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(sample_intake)

    monkeypatch.setattr(
        failure_case_prompt_improvement_actions,
        "collect_failure_case_intake",
        fake_collect_failure_case_intake,
    )

    spec = {
        "workflow_id": "#V#failure_case_prompt_improvement_workflow",
        "workflow_name": "Failure case prompt improvement workflow",
        "workflow_description": (
            "Collects failure-case evidence, prepares replay-backed prompt "
            "variant experiment inputs, persists the represented experiment "
            "spec, records represented replay observations, and emits a "
            "learning signal through canonical testing workflow surfaces."
        ),
        "parent_type_id": "#V#durable_workflow",
        "initial_state_key": "collect_failure_case_intake",
        "steps": [
            {
                "state_id": "collect_failure_case_intake",
                "state_key": "collect_failure_case_intake",
                "action_id": FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
                "execution_mode": "deterministic",
                "static_input_bindings": [
                    {"tool_param": "reference_mode", "value": "latest_prior_failure"}
                ],
                "next_state_key": "prepare_prompt_replay_experiment",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "prepare_prompt_replay_experiment",
                "state_key": "prepare_prompt_replay_experiment",
                "action_id": FAILURE_CASE_PROMPT_REPLAY_PREPARE_ACTION_ID,
                "execution_mode": "deterministic",
                "static_input_bindings": [
                    {
                        "tool_param": "prompt_variant_ids",
                        "value": [
                            "#V#lab_task_review_answer_prompt_variant_a",
                            "#V#lab_task_review_answer_prompt_variant_b",
                        ],
                    },
                    {
                        "tool_param": "verdict_rules",
                        "value": {"minimum_pass_count": 2},
                    },
                    {
                        "tool_param": "promotion_policy",
                        "value": {"requires": "represented_replay_gate"},
                    },
                ],
                "writes_context_keys": [
                    "experiment_name",
                    "experiment_spec_id",
                    "experiment_fixture_payload",
                    "experiment_verdict_rules",
                    "experiment_promotion_policy",
                    "replay_arm_plan",
                    "policy_boundary",
                ],
                "next_state_key": "create_experiment_spec",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "create_experiment_spec",
                "state_key": "create_experiment_spec",
                "action_id": EXPERIMENT_CREATE_SPEC_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {"tool_param": "name", "context_key": "experiment_name"},
                    {
                        "tool_param": "experiment_spec_id",
                        "context_key": "experiment_spec_id",
                    },
                    {
                        "tool_param": "description",
                        "context_key": "experiment_description",
                    },
                    {
                        "tool_param": "target_workflow_ids",
                        "context_key": "target_workflow_ids",
                    },
                    {
                        "tool_param": "fixture_payload",
                        "context_key": "experiment_fixture_payload",
                    },
                    {
                        "tool_param": "verdict_rules",
                        "context_key": "experiment_verdict_rules",
                    },
                    {
                        "tool_param": "promotion_policy",
                        "context_key": "experiment_promotion_policy",
                    },
                    {"tool_param": "metadata", "context_key": "experiment_metadata"},
                ],
                "next_state_key": "start_experiment_run",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "start_experiment_run",
                "state_key": "start_experiment_run",
                "action_id": EXPERIMENT_START_RUN_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {
                        "tool_param": "experiment_spec_id",
                        "context_key": "experiment_spec_id",
                    },
                    {
                        "tool_param": "turn_execution_request_ids",
                        "context_key": "experiment_turn_execution_request_ids",
                    },
                    {"tool_param": "metadata", "context_key": "experiment_metadata"},
                ],
                "writes_context_keys": ["run_id", "experiment_run"],
                "conditional_transitions": [
                    {
                        "to_state_key": "record_prompt_replay_observations",
                        "reason": "arm_summaries_available",
                        "condition": {
                            "kind": "context_exists",
                            "key": "prompt_replay_arm_summaries",
                            "expected": True,
                        },
                    },
                    {
                        "to_state_key": "completed",
                        "reason": "arm_summaries_not_supplied_yet",
                        "condition": {
                            "kind": "context_exists",
                            "key": "prompt_replay_arm_summaries",
                            "expected": False,
                        },
                    },
                ],
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "record_prompt_replay_observations",
                "state_key": "record_prompt_replay_observations",
                "action_id": FAILURE_CASE_PROMPT_REPLAY_RECORD_OBSERVATIONS_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {"tool_param": "run_id", "context_key": "run_id"},
                    {
                        "tool_param": "arm_summaries",
                        "context_key": "prompt_replay_arm_summaries",
                    },
                    {"tool_param": "replay_set_id", "context_key": "replay_set_id"},
                ],
                "static_input_bindings": [
                    {"tool_param": "require_represented_evaluation", "value": True}
                ],
                "writes_context_keys": [
                    "prompt_replay_observations_recorded",
                    "recorded_observation_count",
                    "turn_execution_request_ids",
                    "policy_boundary",
                ],
                "next_state_key": "compute_experiment_verdict",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "compute_experiment_verdict",
                "state_key": "compute_experiment_verdict",
                "action_id": EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {"tool_param": "run_id", "context_key": "run_id"}
                ],
                "writes_context_keys": [
                    "verdict",
                    "verdict_summary",
                    "promotion_recommendation",
                ],
                "next_state_key": "emit_learning_signal",
                "on_failure_state_key": "failed",
            },
            {
                "state_id": "emit_learning_signal",
                "state_key": "emit_learning_signal",
                "action_id": EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
                "execution_mode": "deterministic",
                "context_input_mappings": [
                    {"tool_param": "run_id", "context_key": "run_id"}
                ],
                "writes_context_keys": ["learning_signal"],
                "next_state_key": "completed",
                "on_failure_state_key": "failed",
            },
            {"state_id": "completed", "state_key": "completed", "terminal": True},
            {"state_id": "failed", "state_key": "failed", "terminal": True},
        ],
    }

    definition = build_workflow_definition_from_authoring_spec(spec)
    validation = validate_workflow_definition_contract(
        definition=definition,
        supported_action_ids=(
            FAILURE_CASE_INTAKE_COLLECT_ACTION_ID,
            FAILURE_CASE_PROMPT_REPLAY_PREPARE_ACTION_ID,
            EXPERIMENT_CREATE_SPEC_ACTION_ID,
            EXPERIMENT_START_RUN_ACTION_ID,
            FAILURE_CASE_PROMPT_REPLAY_RECORD_OBSERVATIONS_ACTION_ID,
            EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
            EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
        ),
        enforce_supported_actions=True,
    )
    assert validation["valid"] is True

    captured_create_inputs: dict[str, Any] = {}
    captured_start_inputs: dict[str, Any] = {}
    captured_record_inputs: dict[str, Any] = {}
    captured_verdict_inputs: dict[str, Any] = {}
    captured_signal_inputs: dict[str, Any] = {}

    def fake_create_spec(request: Any) -> WorkflowActionResult:
        captured_create_inputs.update(dict(request.inputs))
        return WorkflowActionResult(
            status="success",
            outputs={
                "success": True,
                "experiment_spec_id": request.inputs["experiment_spec_id"],
            },
        )

    def fake_start_run(request: Any) -> WorkflowActionResult:
        captured_start_inputs.update(dict(request.inputs))
        return WorkflowActionResult(
            status="success",
            outputs={
                "success": True,
                "run_id": "#V#run_1",
                "experiment_run": {
                    "run_id": "#V#run_1",
                    "experiment_spec_id": request.inputs["experiment_spec_id"],
                },
            },
        )

    def fake_record_experiment_observations(**kwargs: Any) -> dict[str, Any]:
        captured_record_inputs.update(kwargs)
        return {
            "success": True,
            "run_id": kwargs["run_id"],
            "recorded_observation_count": 1,
            "turn_execution_request_ids": ["req-arm-1"],
        }

    monkeypatch.setattr(
        failure_case_prompt_improvement_actions,
        "record_experiment_observations",
        fake_record_experiment_observations,
    )

    def fake_compute_verdict(request: Any) -> WorkflowActionResult:
        captured_verdict_inputs.update(dict(request.inputs))
        return WorkflowActionResult(
            status="success",
            outputs={
                "success": True,
                "run_id": request.inputs["run_id"],
                "verdict": "pass",
                "verdict_summary": {"pass": 1, "fail": 0},
                "promotion_recommendation": {
                    "recommended": False,
                    "requires_human_review": True,
                },
            },
        )

    def fake_emit_learning_signal(request: Any) -> WorkflowActionResult:
        captured_signal_inputs.update(dict(request.inputs))
        return WorkflowActionResult(
            status="success",
            outputs={
                "success": True,
                "run_id": request.inputs["run_id"],
                "learning_signal": {
                    "schema_version": "experiment_learning_signal.v1",
                    "selection_outcome": "accepted",
                },
            },
        )

    registry = ActionRegistry()
    register_failure_case_prompt_improvement_actions(registry)
    registry.register(
        ActionSpec(
            action_id=EXPERIMENT_CREATE_SPEC_ACTION_ID,
            handler=fake_create_spec,
        )
    )
    registry.register(
        ActionSpec(
            action_id=EXPERIMENT_START_RUN_ACTION_ID,
            handler=fake_start_run,
        )
    )
    registry.register(
        ActionSpec(
            action_id=EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
            handler=fake_compute_verdict,
        )
    )
    registry.register(
        ActionSpec(
            action_id=EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
            handler=fake_emit_learning_signal,
        )
    )

    result = WorkflowExecutor(registry=registry, max_transitions=8).run(
        definition,
        environment=WorkflowEnvironment(llm_client=None),
        data={
            "request_id": "req-current",
            "prompt_replay_arm_summaries": [
                {
                    "arm": {"arm_id": "arm_1"},
                    "represented_replay_evaluation": {"verdict": "pass"},
                }
            ],
        },
    )

    assert result.completed is True
    assert result.final_state == "completed"
    assert captured_create_inputs["fixture_payload"]["failure_case_intake"][
        "request_id"
    ] == "req-failure-1"
    assert captured_create_inputs["promotion_policy"] == {
        "requires": "represented_replay_gate"
    }
    assert captured_start_inputs["experiment_spec_id"] == captured_create_inputs[
        "experiment_spec_id"
    ]
    assert captured_record_inputs["run_id"] == "#V#run_1"
    assert captured_record_inputs["require_represented_evaluation"] is True
    assert captured_verdict_inputs["run_id"] == "#V#run_1"
    assert captured_signal_inputs["run_id"] == "#V#run_1"
    assert result.data["recorded_observation_count"] == 1
    assert result.data["promotion_recommendation"] == {
        "recommended": False,
        "requires_human_review": True,
    }
    assert result.data["learning_signal"]["selection_outcome"] == "accepted"
    assert result.data["policy_boundary"]["prompt_body_generated"] is False
