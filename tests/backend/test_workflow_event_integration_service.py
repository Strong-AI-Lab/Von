"""Tests for workflow event integration service (JVNAUTOSCI-1090 / WS6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.backend.services import workflow_event_integration_service as workflow_event_service
from src.backend.workflows.durable.models import EventWorkflowBinding
from src.backend.workflows.durable.workflow_instance_submission_service import (
    WorkflowInstanceSubmissionResult,
)
from src.backend.services.workflow_event_integration_service import (
    EVENT_TYPE_CONCEPT_UPDATED,
    EVENT_TYPE_EFFORT_UNIT_COMPLETED,
    EVENT_TYPE_FILE_COPY_UPLOADED,
    EVENT_TYPE_TASK_CREATED,
    EVENT_TYPE_TYPE_CREATED,
    EVENT_TYPE_VONTOLOGY_MUTATED,
    launch_event_workflow,
    maybe_launch_effort_unit_completed_workflow,
    maybe_launch_file_copy_uploaded_workflow,
    maybe_launch_type_created_workflow,
    maybe_launch_vontology_mutation_workflow,
    maybe_launch_task_status_workflow,
)


def _submission_result(
    *,
    workflow_id: str,
    instance_id: str,
    created_new: bool,
) -> WorkflowInstanceSubmissionResult:
    return WorkflowInstanceSubmissionResult(
        success=True,
        workflow_id=workflow_id,
        status="pending" if created_new else "reused",
        instance_id=instance_id,
        verification={
            "preflight_passed": True,
            "postflight_passed": True,
            "runnable_verification_success": True,
        },
        created_new=created_new,
    )


def test_launch_event_workflow_integration_flag_disables_trigger(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "0")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"
    assert result["event_type"] == EVENT_TYPE_TASK_CREATED
    assert result["reason"] == "integration_disabled"
    assert "hint" in result


def test_launch_event_workflow_durable_gate_disables_trigger(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "0")

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"
    assert result["event_type"] == EVENT_TYPE_TASK_CREATED
    assert result["reason"] == "durable_disabled"
    assert "hint" in result


def test_launch_event_workflow_ignores_legacy_env_fallback_without_persisted_binding(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    monkeypatch.setenv("VON_EVENT_TASK_CREATED_WORKFLOW_ID", "#V#legacy_env_workflow")

    result = launch_event_workflow(
        event_type=EVENT_TYPE_TASK_CREATED,
        event_id="task-1",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"
    assert result["event_type"] == EVENT_TYPE_TASK_CREATED
    assert result["reason"] == "workflow_not_configured"
    assert "hint" in result
    assert "workflow_bind_event" in result["hint"]
    assert "workflow_id_env" not in result


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_creates_instance_when_configured(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    mock_manager = MagicMock()
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#task_event_workflow",
            instance_id="instance-123",
            created_new=True,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type=EVENT_TYPE_TASK_CREATED,
            event_id="task-1",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            workflow_id="#V#task_event_workflow",
            inputs={"task_concept_id": "#V#task_1"},
        )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["outcome"] == "triggered"
    assert result["reason"] == "created_new_instance"
    assert result["workflow_id"] == "#V#task_event_workflow"
    assert result["instance_id"] == "instance-123"
    timings = result.get("launch_check_timings_ms")
    assert isinstance(timings, dict)
    assert "idempotency_lookup_ms" in timings
    assert "cadence_check_ms" in timings
    assert "instance_create_ms" in timings
    assert "total_ms" in timings

    called_args = mock_submit.call_args
    assert called_args is not None
    assert called_args.kwargs["workflow_id"] == "#V#task_event_workflow"
    assert called_args.kwargs["source_event_type"] == EVENT_TYPE_TASK_CREATED
    assert called_args.kwargs["source_event_id"] == "task-1"
    assert called_args.kwargs["namespace"] == "#V#user_alice/#V#org_nao"
    assert called_args.kwargs["event_idempotency_key"].startswith(
        "evt:task.created:",
    )


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_reports_reused_idempotent_instance(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    mock_manager = MagicMock()
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#task_event_workflow",
            instance_id="instance-123",
            created_new=False,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type=EVENT_TYPE_TASK_CREATED,
            event_id="task-1",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            workflow_id="#V#task_event_workflow",
            inputs={"task_concept_id": "#V#task_1"},
        )

    assert result["success"] is True
    assert result["triggered"] is False
    assert result["idempotent_reused"] is True
    assert result["outcome"] == "reused"
    assert result["reason"] == "idempotent_reuse"
    assert "hint" in result
    timings = result.get("launch_check_timings_ms")
    assert isinstance(timings, dict)
    assert "idempotency_lookup_ms" in timings
    assert "total_ms" in timings
    assert result["triggered_count"] == 0
    assert result["reused_count"] == 1
    assert result["success_count"] == 1
    assert result["failure_count"] == 0
    assert mock_submit.call_args is not None


def test_maybe_launch_task_status_workflow_skips_unconfigured_status(monkeypatch) -> None:
    monkeypatch.setenv("VON_EVENT_TASK_STATUS_TRIGGER_VALUES", "completed")

    result = maybe_launch_task_status_workflow(
        task_concept_id="#V#task_1",
        previous_status="pending",
        new_status="in_progress",
        updated_at_iso="2026-02-08T10:00:00+00:00",
        created_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"
    assert result["event_type"] == "task.status_changed"
    assert result["reason"] == "status_not_configured_for_trigger"
    assert "hint" in result


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_task_status_workflow_triggers_configured_status(
    mock_launch_event_workflow: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_TASK_STATUS_TRIGGER_VALUES", "completed")
    mock_launch_event_workflow.return_value = {"success": True, "triggered": True}

    result = maybe_launch_task_status_workflow(
        task_concept_id="#V#task_1",
        previous_status="in_progress",
        new_status="completed",
        updated_at_iso="2026-02-08T10:00:00+00:00",
        created_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
    )

    assert result == {"success": True, "triggered": True}

    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == "task.status_changed"
    assert (
        called_args.kwargs["event_id"]
        == "#V#task_1:in_progress->completed:2026-02-08T10:00:00+00:00"
    )


def test_build_event_workflow_binding_diagnostics_reports_operator_actions() -> None:
    multiple_enabled_a = EventWorkflowBinding.create(
        event_type="file_copy.uploaded",
        workflow_id="#V#file_copy_upload_handler_workflow",
        input_mapping={"concept_id": "event.file_copy_concept_id"},
        enabled=True,
        actor="test",
    )
    multiple_enabled_b = EventWorkflowBinding.create(
        event_type="file_copy.uploaded",
        workflow_id="#V#legacy_upload_workflow",
        input_mapping={"concept_id": "event.file_copy_concept_id"},
        enabled=True,
        actor="test",
    )
    disabled_only = EventWorkflowBinding.create(
        event_type="task.created",
        workflow_id="#V#task_followup_workflow",
        input_mapping={"task_concept_id": "event.task_concept_id"},
        enabled=False,
        actor="test",
    )
    historical_enabled = EventWorkflowBinding.create(
        event_type="concept.updated",
        workflow_id="#V#enrichment_workflow",
        input_mapping={"concept_id": "event.concept_id"},
        enabled=True,
        actor="test",
    )
    historical_disabled = EventWorkflowBinding.create(
        event_type="concept.updated",
        workflow_id="#V#legacy_enrichment_workflow",
        input_mapping={"concept_id": "event.concept_id"},
        enabled=False,
        actor="test",
    )
    bindings = [
        {**multiple_enabled_a.to_status_dict(), "source": "persistent"},
        {**multiple_enabled_b.to_status_dict(), "source": "persistent"},
        {**disabled_only.to_status_dict(), "source": "persistent"},
        {**historical_enabled.to_status_dict(), "source": "persistent"},
        {**historical_disabled.to_status_dict(), "source": "persistent"},
    ]

    diagnostics = workflow_event_service.build_event_workflow_binding_diagnostics(
        bindings
    )

    by_event = {item["event_type"]: item for item in diagnostics}
    assert by_event["file_copy.uploaded"]["reason_code"] == "multiple_enabled_bindings"
    assert by_event["file_copy.uploaded"]["severity"] == "warning"
    assert by_event["task.created"]["reason_code"] == "all_persistent_bindings_disabled"
    assert by_event["task.created"]["severity"] == "warning"
    assert by_event["concept.updated"]["reason_code"] == "historical_bindings_present"
    assert by_event["concept.updated"]["severity"] == "info"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_list_event_workflow_bindings_excludes_env_fallback_entries(
    mock_get_instance_manager: MagicMock,
) -> None:
    mock_manager = MagicMock()
    mock_manager.list_event_bindings.return_value = []
    mock_get_instance_manager.return_value = mock_manager

    bindings = workflow_event_service.list_event_workflow_bindings(
        event_type=EVENT_TYPE_TASK_CREATED,
        limit=20,
    )

    assert bindings == []


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_uses_persistent_binding_input_mapping(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    binding = EventWorkflowBinding.create(
        event_type="type.created",
        workflow_id="#V#salient_predicate_governance_workflow",
        input_mapping={
            "type_concept_id": "event.concept_id",
            "seed_value": "inputs.seed",
        },
        enabled=True,
        actor="test",
    )
    mock_manager = MagicMock()
    mock_manager.list_event_bindings.return_value = [binding]
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#salient_predicate_governance_workflow",
            instance_id="instance-999",
            created_new=True,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type="type.created",
            event_id="#V#my_new_type",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            inputs={"seed": "abc-123"},
            event_payload={"concept_id": "#V#my_new_type"},
        )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["workflow_id"] == "#V#salient_predicate_governance_workflow"

    called_args = mock_submit.call_args
    assert called_args is not None
    workflow_inputs = called_args.kwargs["inputs"]
    assert workflow_inputs["type_concept_id"] == "#V#my_new_type"
    assert workflow_inputs["seed_value"] == "abc-123"
    assert workflow_inputs["event"]["event_type"] == "type.created"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_resolves_braced_event_mapping_expression(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    binding = EventWorkflowBinding.create(
        event_type="type.created",
        workflow_id="#V#salient_predicate_governance_workflow",
        input_mapping={
            "target_type_id": "{{event.concept_id}}",
        },
        enabled=True,
        actor="test",
    )
    mock_manager = MagicMock()
    mock_manager.list_event_bindings.return_value = [binding]
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#salient_predicate_governance_workflow",
            instance_id="instance-1000",
            created_new=True,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type="type.created",
            event_id="#V#my_new_type",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            event_payload={"concept_id": "#V#my_new_type"},
        )

    assert result["success"] is True
    assert result["triggered"] is True
    called_args = mock_submit.call_args
    assert called_args is not None
    workflow_inputs = called_args.kwargs["inputs"]
    assert workflow_inputs["target_type_id"] == "#V#my_new_type"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_uses_namespace_override_for_tenancy(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    binding = EventWorkflowBinding.create(
        event_type="type.created",
        workflow_id="#V#salient_predicate_governance_workflow",
        input_mapping={"target_type_id": "event.concept_id"},
        enabled=True,
        actor="test",
    )
    mock_manager = MagicMock()
    mock_manager.list_event_bindings.return_value = [binding]
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#salient_predicate_governance_workflow",
            instance_id="instance-ns-1",
            created_new=True,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type="type.created",
            event_id="#V#my_new_type",
            user_id=None,
            org_id=None,
            namespace="#V#user_alice@org_nao",
            event_payload={"concept_id": "#V#my_new_type"},
        )

    assert result["success"] is True
    assert result["triggered"] is True

    called_args = mock_submit.call_args
    assert called_args is not None
    assert called_args.kwargs["user_id"] == "#V#user_alice"
    assert called_args.kwargs["org_id"] == "#V#org_nao"
    assert called_args.kwargs["namespace"] == "#V#user_alice@org_nao"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_uses_user_only_namespace_when_org_unknown(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    mock_manager = MagicMock()
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#task_event_workflow",
            instance_id="instance-user-only",
            created_new=True,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type=EVENT_TYPE_TASK_CREATED,
            event_id="task-namespace-user-only",
            user_id="#V#user_alice",
            org_id=None,
            workflow_id="#V#task_event_workflow",
            inputs={"task_concept_id": "#V#task_99"},
        )

    assert result["success"] is True
    assert result["triggered"] is True

    called_args = mock_submit.call_args
    assert called_args is not None
    assert called_args.kwargs["namespace"] == "#V#user_alice"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_blocks_when_cadence_window_is_active(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    now = datetime.now(timezone.utc)
    mock_manager = MagicMock()
    mock_manager.get_event_instance.return_value = None
    mock_manager.get_latest_instance_for_workflow.return_value = SimpleNamespace(
        instance_id="instance-recent",
        created_at=now - timedelta(seconds=90),
    )
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
    ) as mock_submit:
        with patch(
            "src.backend.services.workflow_event_integration_service._workflow_background_launch_policy_for_id",
            return_value=(
                {
                    "schema_version": "workflow_background_launch_policy.v1",
                    "enabled": True,
                    "min_interval_seconds": 300,
                    "scope": "global_per_server",
                    "applies_to_sources": ["event"],
                },
                "text_relation:#V#hasBackgroundLaunchPolicyJson",
            ),
        ):
                result = launch_event_workflow(
                    event_type=EVENT_TYPE_TASK_CREATED,
                    event_id="task-cadence-1",
                    user_id="#V#user_alice",
                    org_id="#V#org_nao",
                    workflow_id="#V#task_event_workflow",
                )

    assert result["success"] is True
    assert result["triggered"] is False
    assert result["outcome"] == "not_triggered"
    assert result["reason"] == "workflow_cadence_limited"
    retry_after_seconds = result.get("retry_after_seconds")
    assert isinstance(retry_after_seconds, int)
    assert retry_after_seconds > 0
    assert result.get("cadence_policy_source") == "text_relation:#V#hasBackgroundLaunchPolicyJson"
    mock_submit.assert_not_called()
    mock_manager.create_instance_for_event.assert_not_called()


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_idempotent_reuse_takes_precedence_over_cadence(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    now = datetime.now(timezone.utc)
    mock_manager = MagicMock()
    mock_manager.get_event_instance.return_value = SimpleNamespace(
        instance_id="instance-existing",
        created_at=now - timedelta(seconds=10),
    )
    mock_manager.get_latest_instance_for_workflow.return_value = SimpleNamespace(
        instance_id="instance-existing",
        created_at=now - timedelta(seconds=10),
    )
    mock_get_instance_manager.return_value = mock_manager

    with patch(
        "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
    ) as mock_submit, patch(
        "src.backend.services.workflow_event_integration_service._workflow_background_launch_policy_for_id",
        return_value=(
            {
                "schema_version": "workflow_background_launch_policy.v1",
                "enabled": True,
                "min_interval_seconds": 300,
                "scope": "global_per_server",
                "applies_to_sources": ["event"],
            },
            "text_relation:#V#hasBackgroundLaunchPolicyJson",
        ),
    ):
            result = launch_event_workflow(
                event_type=EVENT_TYPE_TASK_CREATED,
                event_id="task-cadence-existing",
                user_id="#V#user_alice",
                org_id="#V#org_nao",
                workflow_id="#V#task_event_workflow",
            )

    assert result["success"] is True
    assert result["triggered"] is False
    assert result["outcome"] == "reused"
    assert result["reason"] == "idempotent_reuse"
    assert result["idempotent_reused"] is True
    assert result["instance_id"] == "instance-existing"
    mock_submit.assert_not_called()
    mock_manager.create_instance_for_event.assert_not_called()


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_effort_unit_completed_workflow_emits_completion_event(
    mock_launch_event_workflow: MagicMock,
) -> None:
    mock_launch_event_workflow.return_value = {"success": True, "triggered": True}

    result = maybe_launch_effort_unit_completed_workflow(
        effort_unit_concept_id="#V#task_1",
        effort_unit_type_ids=["#V#task_specification"],
        successor_effort_unit_type_ids=["#V#conference_presentation"],
        completed_at_iso="2026-02-21T12:00:00+00:00",
        created_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
    )

    assert result == {"success": True, "triggered": True}
    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == EVENT_TYPE_EFFORT_UNIT_COMPLETED
    assert (
        called_args.kwargs["event_id"]
        == "#V#task_1:completed:2026-02-21T12:00:00+00:00"
    )
    assert called_args.kwargs["event_payload"]["successor_effort_unit_type_ids"] == [
        "#V#conference_presentation"
    ]


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_file_copy_uploaded_workflow_emits_upload_event(
    mock_launch_event_workflow: MagicMock,
) -> None:
    mock_launch_event_workflow.return_value = {
        "success": True,
        "triggered": True,
        "workflow_id": "#V#file_copy_upload_handler_workflow",
        "selected_workflow_id": "#V#file_copy_upload_handler_workflow",
        "launch_strategy": "resolved_persistent_bindings",
    }

    result = maybe_launch_file_copy_uploaded_workflow(
        file_copy_concept_id="#V#uploaded_file_copy_123",
        uploaded_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
        content_type="image/png",
        original_filename="screenshot.png",
        size_bytes=1024,
        sha256="abc123",
        blob_uri="local://uploads/user/abc/screenshot.png",
        uploaded_at_iso="2026-02-27T09:00:00+00:00",
        index_in_rag=True,
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["launch_strategy"] == "resolved_persistent_bindings"
    assert result["selected_workflow_id"] == "#V#file_copy_upload_handler_workflow"

    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == EVENT_TYPE_FILE_COPY_UPLOADED
    assert called_args.kwargs["event_id"] == "#V#uploaded_file_copy_123"
    assert "workflow_id" not in called_args.kwargs
    assert called_args.kwargs["inputs"]["file_copy_concept_id"] == "#V#uploaded_file_copy_123"
    assert called_args.kwargs["inputs"]["index_in_rag"] is True

@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_type_created_workflow_defers_to_persisted_binding(
    mock_launch_event_workflow: MagicMock,
) -> None:
    mock_launch_event_workflow.return_value = {
        "success": True,
        "triggered": True,
        "workflow_id": "#V#salient_predicate_governance_workflow",
        "selected_workflow_id": "#V#salient_predicate_governance_workflow",
        "launch_strategy": "resolved_persistent_bindings",
    }

    result = maybe_launch_type_created_workflow(
        type_concept_id="#V#new_type",
        created_by_concept_id="#V#user_alice",
        organisation_concept_id="#V#org_nao",
        parent_type_ids=["#V#supertype"],
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["selected_workflow_id"] == "#V#salient_predicate_governance_workflow"
    assert result["launch_strategy"] == "resolved_persistent_bindings"

    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == EVENT_TYPE_TYPE_CREATED
    assert "workflow_id" not in called_args.kwargs
    assert called_args.kwargs["inputs"]["type_concept_id"] == "#V#new_type"


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_vontology_mutation_workflow_emits_specific_and_catch_all(
    mock_launch_event_workflow: MagicMock,
) -> None:
    mock_launch_event_workflow.side_effect = [
        {"success": True, "triggered": True, "event_type": EVENT_TYPE_CONCEPT_UPDATED},
        {"success": True, "triggered": False, "event_type": EVENT_TYPE_VONTOLOGY_MUTATED},
    ]

    result = maybe_launch_vontology_mutation_workflow(
        mutation_event_type=EVENT_TYPE_CONCEPT_UPDATED,
        mutation_id="#V#person",
        user_id="#V#user_alice",
        org_id="#V#org_nao",
        event_payload={"concept_id": "#V#person"},
        inputs={"concept_id": "#V#person"},
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["specific_triggered"] is True
    assert result["catch_all_triggered"] is False

    first_call = mock_launch_event_workflow.call_args_list[0]
    second_call = mock_launch_event_workflow.call_args_list[1]
    assert first_call.kwargs["event_type"] == EVENT_TYPE_CONCEPT_UPDATED
    assert second_call.kwargs["event_type"] == EVENT_TYPE_VONTOLOGY_MUTATED
