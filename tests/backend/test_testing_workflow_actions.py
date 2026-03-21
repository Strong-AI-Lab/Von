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
    TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
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
            max_retries=int(kwargs.get("max_retries", 1) or 1),
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
        max_retries: int = 1,
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
    error: str | None = None
    error_step: str | None = None
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
        TESTING_PREPARE_MEETING_INVITATION_SPEC_ACTION_ID,
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
        },
        "max_retries": 2,
    }


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
                outputs={"meeting_type": "project_meeting"},
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
        or {"success": True, "run_id": kwargs["run_id"]},
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
        "meeting_type": "project_meeting"
    }
    assert result.outputs["observation_recording"]["success"] is True
    assert recorded["run_id"] == "#V#run_1"
    observation = recorded["observations"][0]
    assert observation["label"] == "target_workflow_execution"
    assert observation["verdict"] == "pass"
    assert observation["observed_outcome"] == "completed"
    assert observation["workflow_execution"]["final_status"] == "completed"
