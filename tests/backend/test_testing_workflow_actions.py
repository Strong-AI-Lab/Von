from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.backend.workflows.durable.models import WorkflowInstanceStatus
from src.backend.services.testing_workflow_contracts import (
    EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
    EXPERIMENT_CREATE_SPEC_ACTION_ID,
    EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
    EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
    EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
    EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
    EXPERIMENT_START_RUN_ACTION_ID,
    TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
    TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
    TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
    TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
    TESTING_VALIDATE_CANDIDATE_WORKFLOW_ACTION_ID,
    TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
    THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
    THEORY_COMPUTE_DIFF_ACTION_ID,
    THEORY_CREATE_SLICE_ACTION_ID,
    THEORY_GC_EXPIRED_SLICES_ACTION_ID,
    THEORY_IMPORT_CANONICAL_CONTEXT_ACTION_ID,
    THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
    THEORY_ROLLBACK_LOCAL_WRITES_ACTION_ID,
)
from src.backend.workflows.action_registry import (
    ActionRegistry,
    WorkflowActionRequest,
    WorkflowEnvironment,
)
from src.backend.workflows.durable.testing_workflow_actions import (
    register_testing_workflow_actions,
)


def _patch_submit_verified_instance_success(monkeypatch, manager: _StubWorkflowManager) -> None:
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    def _fake_submit_verified_workflow_instance(**kwargs):
        workflow_id = str(kwargs.get("workflow_id") or "").strip()
        instance_id = manager.create_instance(
            workflow_id,
            user_id=str(kwargs.get("user_id") or "anonymous").strip() or "anonymous",
            org_id=str(kwargs.get("org_id") or "default").strip() or "default",
            namespace=str(kwargs.get("namespace") or "#V#anonymous@default").strip()
            or "#V#anonymous@default",
            inputs=dict(kwargs.get("inputs") or {}),
            max_retries=int(kwargs.get("max_retries", 3) or 3),
        )
        return WorkflowInstanceSubmissionResult(
            success=True,
            workflow_id=workflow_id,
            status="pending",
            instance_id=instance_id,
            verification={
                "preflight_passed": True,
                "postflight_passed": True,
                "runnable_verification_success": True,
                "preflight": {"errors": []},
                "postflight": {"errors": []},
            },
            created_new=True,
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )


@dataclass
class _StubWorkflowManager:
    last_call: dict[str, Any] | None = None
    instances_by_id: dict[str, Any] | None = None

    def create_instance(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        inputs: dict[str, Any] | None = None,
        max_retries: int = 3,
        **_kwargs: Any,
    ) -> str:
        self.last_call = {
            "workflow_id": workflow_id,
            "user_id": user_id,
            "org_id": org_id,
            "namespace": namespace,
            "inputs": dict(inputs or {}),
            "max_retries": max_retries,
        }
        return "#V#wf_instance_testing"

    def get_instance(self, instance_id: str) -> Any | None:
        return (self.instances_by_id or {}).get(instance_id)


@dataclass
class _StubWorkflowInstance:
    instance_id: str
    workflow_id: str
    status: WorkflowInstanceStatus
    current_state: str = "complete"
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    workflow_data: dict[str, Any] | None = None
    error: str | None = None
    error_step: str | None = None
    execution_trace_id: str | None = None
    user_id: str = "#V#user"
    org_id: str = "#V#org"
    namespace: str = "#V#user@org"

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "current_state": self.current_state,
            "error": self.error,
            "error_step": self.error_step,
            "execution_trace_id": self.execution_trace_id,
        }


def test_register_testing_workflow_actions_exposes_all_expected_action_ids():
    registry = ActionRegistry()
    register_testing_workflow_actions(registry)

    expected = {
        THEORY_CREATE_SLICE_ACTION_ID,
        THEORY_IMPORT_CANONICAL_CONTEXT_ACTION_ID,
        THEORY_ASSERT_LOCAL_CLAIM_ACTION_ID,
        THEORY_COMPUTE_DIFF_ACTION_ID,
        THEORY_ROLLBACK_LOCAL_WRITES_ACTION_ID,
        THEORY_PROMOTE_VALIDATED_CLAIMS_ACTION_ID,
        THEORY_GC_EXPIRED_SLICES_ACTION_ID,
        EXPERIMENT_CREATE_SPEC_ACTION_ID,
        EXPERIMENT_START_RUN_ACTION_ID,
        EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
        EXPERIMENT_COMPUTE_VERDICT_ACTION_ID,
        EXPERIMENT_EMIT_LEARNING_SIGNAL_ACTION_ID,
        EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
        EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
        TESTING_VALIDATE_CANDIDATE_WORKFLOW_ACTION_ID,
        TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
        TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
        TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
        TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
        TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
    }

    for action_id in expected:
        assert registry.get(action_id) is not None


def test_theory_create_slice_action_derives_actor_context_from_namespace(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "create_testing_theory_slice",
        lambda **kwargs: captured.update(kwargs) or {"success": True, "theory_id": "#V#slice"},
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(THEORY_CREATE_SLICE_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=THEORY_CREATE_SLICE_ACTION_ID,
            inputs={"name": "Meeting slice", "ttl_seconds": 600},
            environment=WorkflowEnvironment(
                llm_client=None,
                user_namespace="#V#user@org",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert captured["namespace"] == "#V#user@org"
    assert captured["user_id"] == "#V#user"
    assert captured["org_id"] == "#V#org"
    assert captured["ttl_seconds"] == 600


def test_execute_target_workflow_action_launches_durable_instance(monkeypatch):
    manager = _StubWorkflowManager()
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    _patch_submit_verified_instance_success(monkeypatch, manager)

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            inputs={
                "workflow_id": "#V#meeting_invitation_testing_workflow",
                "workflow_inputs": {"fixture_id": "fixture-1"},
                "run_id": "#V#run_1",
                "theory_id": "#V#theory_1",
                "max_retries": 2,
            },
            environment=WorkflowEnvironment(
                llm_client=None,
                user_namespace="#V#user@org",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert result.outputs["instance_id"] == "#V#wf_instance_testing"
    assert manager.last_call == {
        "workflow_id": "#V#meeting_invitation_testing_workflow",
        "user_id": "#V#user",
        "org_id": "#V#org",
        "namespace": "#V#user@org",
        "inputs": {
            "fixture_id": "fixture-1",
            "experiment_run_id": "#V#run_1",
            "testing_theory_id": "#V#theory_1",
            "workflow_execution_side_effect_policy": {
                "schema_version": "workflow_execution_side_effect_policy.v1",
                "mode": "theory_bounded",
                "testing_theory_id": "#V#theory_1",
                "audit_label": "experiment.execute_target_workflow",
            },
        },
        "max_retries": 2,
    }


def test_execute_target_workflow_action_defaults_to_three_retries(monkeypatch):
    manager = _StubWorkflowManager()
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    _patch_submit_verified_instance_success(monkeypatch, manager)

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            inputs={
                "workflow_id": "#V#arxiv_paper_representation_workflow",
                "workflow_inputs": {"prompt": "test"},
            },
            environment=WorkflowEnvironment(
                llm_client=None,
                user_namespace="#V#user@org",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert manager.last_call is not None
    assert manager.last_call["max_retries"] == 3


def test_prepare_experiment_spec_action_resolves_actor_context(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "prepare_experiment_spec_from_template",
        lambda **kwargs: captured.update(kwargs)
        or {"success": True, "experiment_spec_id": "#V#scenario_spec"},
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=TESTING_PREPARE_EXPERIMENT_SPEC_ACTION_ID,
            inputs={
                "scenario_template": {"schema_version": "testing_experiment_scenario_template.v1"},
                "invitation_text": "Meet tomorrow",
                "candidate_workflow_ids": ["#V#wf_candidate"],
            },
            environment=WorkflowEnvironment(
                llm_client=None,
                user_namespace="#V#user@org",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert captured["namespace"] == "#V#user@org"
    assert captured["user_id"] == "#V#user"
    assert captured["org_id"] == "#V#org"
    assert captured["template_inputs"] == {
        "invitation_text": "Meet tomorrow",
        "candidate_workflow_ids": ["#V#wf_candidate"],
    }


def test_validate_candidate_workflow_action_forwards_validation_controls(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "validate_workflow_candidate",
        lambda workflow_id, **kwargs: (
            captured.update({"workflow_id": workflow_id, **kwargs})
            or {
                "success": True,
                "workflow_id": workflow_id,
                "candidate_validation": {"valid": True},
                "preview": {"definition_identity": {"hash": "candidate-hash"}},
            }
        ),
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(TESTING_VALIDATE_CANDIDATE_WORKFLOW_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=TESTING_VALIDATE_CANDIDATE_WORKFLOW_ACTION_ID,
            inputs={
                "workflow_id": "#V#candidate_workflow",
                "authoring_spec": {"workflow_id": "#V#candidate_workflow"},
                "base_definition_hash": "current-hash",
                "validation_profile": "contract_only",
                "include_preview": True,
            },
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is True
    assert result.outputs["workflow_id"] == "#V#candidate_workflow"
    assert captured == {
        "workflow_id": "#V#candidate_workflow",
        "authoring_spec": {"workflow_id": "#V#candidate_workflow"},
        "base_definition_hash": "current-hash",
        "validation_profile": "contract_only",
        "include_preview": True,
    }


def test_prepare_arxiv_fixture_action_resolves_prompt_and_actor_context(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "prepare_arxiv_paper_ingestion_test_fixture",
        lambda **kwargs: captured.update(kwargs)
        or {"success": True, "arxiv_id": "2603.21702"},
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=TESTING_PREPARE_ARXIV_PAPER_INGESTION_FIXTURE_ACTION_ID,
            inputs={
                "prompt_text": "Run the ingestion test on https://arxiv.org/abs/2603.21702",
                "repair_existing_artifacts": True,
            },
            environment=WorkflowEnvironment(
                llm_client=None,
                user_namespace="#V#user@org",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert captured["prompt_text"] == "Run the ingestion test on https://arxiv.org/abs/2603.21702"
    assert captured["user_concept_id"] == "#V#user"
    assert captured["timeout_seconds"] == 15.0
    assert captured["repair_existing_artifacts"] is True


def test_verify_arxiv_ingestion_result_action_forwards_expected_metadata(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "verify_arxiv_paper_ingestion_test_result",
        lambda **kwargs: captured.update(kwargs)
        or {"success": True, "verification_passed": True},
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=TESTING_VERIFY_ARXIV_PAPER_INGESTION_RESULT_ACTION_ID,
            inputs={
                "workflow_execution": {"outputs": {"paper_concept_id": "#V#paper_2603_21702"}},
                "arxiv_id": "2603.21702",
                "source_uri": "https://arxiv.org/abs/2603.21702",
                "expected_title": "Example title",
                "expected_summary": "Example abstract",
                "expected_publication_date": "2026-03-25",
                "expected_author_names": ["Author One"],
                "expected_author_concept_ids": ["#V#author_one"],
                "expected_topic_labels": ["cs.AI"],
                "expected_topic_concept_ids": ["#V#topic_cs_ai"],
                "paper_concept_id": "#V#paper_2603_21702",
            },
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is True
    assert captured["arxiv_id"] == "2603.21702"
    assert captured["expected_publication_date"] == "2026-03-25"
    assert captured["expected_author_concept_ids"] == ["#V#author_one"]


def test_cleanup_arxiv_ingestion_artifacts_action_forwards_cleanup_targets(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "cleanup_arxiv_paper_ingestion_test_artifacts",
        lambda **kwargs: captured.update(kwargs)
        or {"success": True, "cleanup_passed": True},
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=TESTING_CLEANUP_ARXIV_PAPER_INGESTION_ARTIFACTS_ACTION_ID,
            inputs={
                "paper_concept_id": "#V#paper_2603_21702",
                "file_copy_concept_id": "#V#file_copy_2603_21702",
                "file_copy_concept_ids": [
                    "#V#file_copy_2603_21702",
                    "#V#markdown_file_copy_2603_21702",
                ],
                "author_concept_ids": ["#V#author_one"],
                "topic_concept_ids": ["#V#topic_cs_ai"],
                "preexisting_author_concept_ids": ["#V#author_one"],
                "preexisting_topic_concept_ids": [],
            },
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is True
    assert captured["paper_concept_id"] == "#V#paper_2603_21702"
    assert captured["file_copy_concept_id"] == "#V#file_copy_2603_21702"
    assert captured["file_copy_concept_ids"] == [
        "#V#file_copy_2603_21702",
        "#V#markdown_file_copy_2603_21702",
    ]
    assert captured["preexisting_author_concept_ids"] == ["#V#author_one"]


def test_execute_regression_suite_action_forwards_suite_policy(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "execute_regression_suite",
        lambda **kwargs: captured.update(kwargs) or {"success": True},
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_EXECUTE_REGRESSION_SUITE_ACTION_ID,
            inputs={
                "execution_tier": "tier2",
                "suite_policy": {"tiers": {"tier2": {"mode": "benchmark"}}},
            },
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is True
    assert captured["suite_policy"] == {"tiers": {"tier2": {"mode": "benchmark"}}}


def test_start_run_action_returns_compact_summary(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    monkeypatch.setattr(
        mod,
        "start_experiment_run",
        lambda **_kwargs: {
            "success": True,
            "run_id": "#V#run_1",
            "experiment_run": {
                "experiment_spec_id": "#V#spec_1",
                "status": "running",
                "theory_id": "#V#theory_1",
                "target_workflow_ids": ["#V#arxiv_paper_representation_workflow"],
                "candidate_workflow_ids": ["#V#arxiv_paper_representation_workflow"],
                "benchmark_tier": "tier1",
                "large_payload": "x" * 4096,
            },
            "projection": {"large_payload": "y" * 4096},
        },
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_START_RUN_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_START_RUN_ACTION_ID,
            inputs={"experiment_spec_id": "#V#spec_1"},
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is True
    assert result.outputs["run_id"] == "#V#run_1"
    assert result.outputs["experiment_spec_id"] == "#V#spec_1"
    assert result.outputs["status"] == "running"
    assert "projection" not in result.outputs
    assert "large_payload" not in result.outputs


def test_record_observation_action_returns_compact_summary(monkeypatch):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    monkeypatch.setattr(
        mod,
        "record_experiment_observation",
        lambda **_kwargs: {
            "success": True,
            "run_id": "#V#run_1",
            "recorded_observations": [
                {"label": "metadata_representation", "verdict": "pass"}
            ],
            "experiment_run": {
                "observations": [
                    {"label": "target_workflow_execution", "verdict": "pass"},
                    {"label": "metadata_representation", "verdict": "pass"},
                ],
                "large_payload": "x" * 4096,
            },
            "projection": {"large_payload": "y" * 4096},
        },
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_RECORD_OBSERVATION_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_RECORD_OBSERVATION_ACTION_ID,
            inputs={
                "run_id": "#V#run_1",
                "observations": [{"label": "metadata_representation", "verdict": "pass"}],
            },
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is True
    assert result.outputs["run_id"] == "#V#run_1"
    assert result.outputs["recorded_observation_count"] == 1
    assert result.outputs["recorded_observation_labels"] == ["metadata_representation"]
    assert result.outputs["observation_count"] == 2
    assert result.outputs["observations"] == [
        {"label": "target_workflow_execution", "verdict": "pass"},
        {"label": "metadata_representation", "verdict": "pass"},
    ]
    assert "experiment_run" not in result.outputs
    assert "projection" not in result.outputs


def test_execute_target_workflow_action_can_await_terminal_and_record_observation(
    monkeypatch,
):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    manager = _StubWorkflowManager(
        instances_by_id={
            "#V#wf_instance_testing": _StubWorkflowInstance(
                instance_id="#V#wf_instance_testing",
                workflow_id="#V#meeting_invitation_testing_workflow",
                status=WorkflowInstanceStatus.COMPLETED,
                execution_trace_id="#V#trace_1",
                outputs={
                    "meeting_type": "project_meeting",
                    "nested_payload": {"detail": "should be omitted"},
                },
                workflow_data={
                    "mutation_guardrail_events": [
                        {
                            "tool_name": "workflow_create_instance",
                            "decision": "allow",
                            "requires_confirmation": False,
                        }
                    ],
                    "workflow_step_result_envelopes": [
                        {"step_id": "prepare", "ok": True, "payload": {"detail": "x" * 1024}}
                    ],
                    "last_workflow_step_result_envelope": {
                        "step_id": "complete",
                        "ok": True,
                    },
                    "workflow_result_envelope": {"ok": True},
                    "workflow_metadata_validation_events": [
                        {"phase": "verify", "ok": True, "enforced": True}
                    ],
                    "last_metadata_validation": {
                        "phase": "verify",
                        "ok": True,
                        "enforced": True,
                    },
                },
            )
        }
    )
    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    _patch_submit_verified_instance_success(monkeypatch, manager)
    monkeypatch.setattr(
        mod,
        "record_experiment_observation",
        lambda **kwargs: recorded.update(kwargs)
        or {
            "success": True,
            "run_id": kwargs["run_id"],
            "recorded_observations": list(kwargs["observations"]),
            "experiment_run": {"large_payload": "x" * 4096},
            "projection": {"large_payload": "y" * 4096},
        },
    )
    monkeypatch.setattr(
        mod,
        "get_workflow_execution_trace",
        lambda execution_trace_id: {
            "execution_trace_id": execution_trace_id,
            "workflow_id": "#V#meeting_invitation_testing_workflow",
        },
    )
    monkeypatch.setattr(
        mod,
        "build_workflow_execution_trace_summary",
        lambda trace_doc: {
            "execution_trace_id": trace_doc["execution_trace_id"],
            "failed_step_count": 0,
            "completed_step_count": 2,
        },
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            inputs={
                "workflow_id": "#V#meeting_invitation_testing_workflow",
                "workflow_inputs": {"fixture_id": "fixture-1"},
                "run_id": "#V#run_1",
                "candidate_validation": {
                    "valid": True,
                    "assertion_classes": ["workflow_candidate_validation"],
                    "repair_hints": [
                        {
                            "scope": "workflow_contract",
                            "reason_code": "contract_validated",
                        }
                    ],
                },
                "await_terminal": True,
                "timeout_seconds": 5,
                "poll_interval_seconds": 0,
            },
            environment=WorkflowEnvironment(
                llm_client=None,
                user_namespace="#V#user@org",
            ),
            data={},
        )
    )

    assert result.ok is True
    assert result.outputs["final_status"] == "completed"
    assert result.outputs["workflow_execution"]["await_terminal"] is True
    assert result.outputs["workflow_execution"]["outputs"] == {
        "meeting_type": "project_meeting",
        "omitted_output_keys": ["nested_payload"],
    }
    assert result.outputs["candidate_validation"]["valid"] is True
    assert result.outputs["trace_summary"] == {
        "execution_trace_id": "#V#trace_1",
        "failed_step_count": 0,
        "completed_step_count": 2,
    }
    assert result.outputs["side_effect_audit"] == {
        "event_count": 1,
        "blocked_event_count": 0,
        "requires_confirmation_count": 0,
        "decision_counts": {"allow": 1},
        "allowed_side_effects": [],
        "forbidden_side_effects": [],
    }
    assert result.outputs["repair_hints"] == [
        {
            "scope": "workflow_contract",
            "reason_code": "contract_validated",
        }
    ]
    assert result.outputs["quality_signals"] == {
        "timed_out": False,
        "candidate_validation_valid": True,
        "metadata_validation_failed_count": 0,
        "metadata_validation_enforced_failed_count": 0,
        "mutation_guardrail_blocked_count": 0,
        "requires_follow_up": False,
    }
    assert "step_result_envelopes" not in result.outputs["workflow_execution"]
    assert "workflow_instance" not in result.outputs
    assert result.outputs["observation_recording"]["success"] is True
    assert result.outputs["observation_recording"]["recorded_observation_count"] == 1
    assert result.outputs["observation_recording"]["recorded_observation_labels"] == [
        "target_workflow_execution"
    ]
    assert "experiment_run" not in result.outputs["observation_recording"]
    assert recorded["run_id"] == "#V#run_1"
    observation = recorded["observations"][0]
    assert observation["label"] == "target_workflow_execution"
    assert observation["verdict"] == "pass"
    assert observation["observed_outcome"] == "completed"
    assert observation["workflow_execution"]["final_status"] == "completed"
    assert "step_result_envelopes" not in observation["workflow_execution"]
    assert observation["candidate_validation"]["valid"] is True
    assert observation["trace_summary"]["completed_step_count"] == 2
    assert observation["side_effect_audit"]["decision_counts"] == {"allow": 1}
    assert observation["quality_signals"]["requires_follow_up"] is False


def test_execute_target_workflow_action_fail_closes_invalid_candidate_and_records_observation(
    monkeypatch,
):
    from src.backend.workflows.durable import testing_workflow_actions as mod

    recorded: dict[str, Any] = {}
    monkeypatch.setattr(
        mod,
        "record_experiment_observation",
        lambda **kwargs: recorded.update(kwargs)
        or {
            "success": True,
            "run_id": kwargs["run_id"],
            "recorded_observations": list(kwargs["observations"]),
            "experiment_run": {"large_payload": "x" * 4096},
        },
    )

    registry = ActionRegistry()
    register_testing_workflow_actions(registry)
    spec = registry.get(EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID)
    assert spec is not None

    result = spec.handler(
        WorkflowActionRequest(
            action_id=EXPERIMENT_EXECUTE_TARGET_WORKFLOW_ACTION_ID,
            inputs={
                "workflow_id": "#V#candidate_workflow",
                "run_id": "#V#run_invalid_candidate",
                "record_observation": True,
                "candidate_validation": {
                    "valid": False,
                    "assertion_classes": ["workflow_generation_safety_failure"],
                    "repair_hints": [
                        {
                            "scope": "generation_safety",
                            "reason_code": "llm_step_missing_validation_policy",
                        }
                    ],
                },
            },
            environment=WorkflowEnvironment(llm_client=None),
            data={},
        )
    )

    assert result.ok is False
    assert result.error == "candidate_validation_failed"
    assert result.outputs["candidate_validation"]["valid"] is False
    assert result.outputs["observation_recording"]["recorded_observation_count"] == 1
    observation = recorded["observations"][0]
    assert observation["label"] == "target_workflow_execution"
    assert observation["verdict"] == "fail"
    assert observation["observed_outcome"] == "candidate_validation_failed"
    assert observation["assertion_classes"] == [
        "workflow_candidate_validation",
        "workflow_generation_safety_failure",
    ]
    assert observation["quality_signals"]["requires_follow_up"] is True
