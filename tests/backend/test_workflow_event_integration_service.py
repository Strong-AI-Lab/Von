"""Tests for workflow event integration service (JVNAUTOSCI-1090 / WS6)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.backend.services import (
    workflow_event_integration_service as workflow_event_service,
)
from src.backend.services.feature_flags import (
    get_workflow_discovery_cache_invalidation_enabled,
)
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
    EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL,
    current_event_workflow_launch_suppression_reason,
    launch_event_workflow,
    maybe_launch_episode_evaluation_for_turn_completion_gate,
    maybe_launch_episode_evaluation_for_workflow_terminal,
    maybe_launch_effort_unit_completed_workflow,
    maybe_launch_file_copy_uploaded_workflow,
    maybe_launch_type_created_workflow,
    maybe_launch_vontology_mutation_workflow,
    maybe_launch_task_status_workflow,
    suppress_event_workflow_launches,
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


def test_event_workflow_launch_suppression_is_nested_and_thread_local() -> None:
    assert current_event_workflow_launch_suppression_reason() is None

    with suppress_event_workflow_launches("outer"):
        assert current_event_workflow_launch_suppression_reason() == "outer"
        with suppress_event_workflow_launches("inner"):
            assert current_event_workflow_launch_suppression_reason() == "inner"
        assert current_event_workflow_launch_suppression_reason() == "outer"

        with ThreadPoolExecutor(max_workers=1) as executor:
            isolated_reason = executor.submit(
                current_event_workflow_launch_suppression_reason
            ).result()
        assert isolated_reason is None

    assert current_event_workflow_launch_suppression_reason() is None


def test_suppressed_event_launch_returns_before_binding_reads(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    binding_lookup = MagicMock(side_effect=AssertionError("binding read attempted"))
    monkeypatch.setattr(
        workflow_event_service,
        "_resolve_bindings_for_event",
        binding_lookup,
    )

    with suppress_event_workflow_launches("owned_materialisation_mutation"):
        result = launch_event_workflow(
            event_type=EVENT_TYPE_TASK_CREATED,
            event_id="task-1",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
        )

    assert result == {
        "success": True,
        "triggered": False,
        "outcome": "suppressed",
        "event_type": EVENT_TYPE_TASK_CREATED,
        "event_id": "task-1",
        "reason": "event_workflow_launch_suppressed",
        "suppression_reason": "owned_materialisation_mutation",
        "event_workflow_launch_suppressed": True,
    }
    binding_lookup.assert_not_called()


def test_event_launch_suppression_disables_discovery_cache_invalidation_locally(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        "VON_WORKFLOW_DISCOVERY_CACHE_INVALIDATION_ENABLE",
        raising=False,
    )
    assert get_workflow_discovery_cache_invalidation_enabled(default=True) is True

    with suppress_event_workflow_launches("owned_materialisation_mutation"):
        assert get_workflow_discovery_cache_invalidation_enabled(default=True) is False

    assert get_workflow_discovery_cache_invalidation_enabled(default=True) is True


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
    assert called_args.kwargs["namespace"] == "#V#user_alice@org_nao"
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


def test_maybe_launch_task_status_workflow_emits_all_status_changes(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

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
    assert result["reason"] == "workflow_not_configured"
    assert result["launch_strategy"] == "resolved_persistent_bindings"


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_task_status_workflow_delegates_to_event_launcher(
    mock_launch_event_workflow: MagicMock,
) -> None:
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


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_applies_represented_binding_condition(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    binding = EventWorkflowBinding.create(
        event_type="task.status_changed",
        workflow_id="#V#task_status_workflow",
        input_mapping={"status": "event.new_status"},
        condition={
            "kind": "context_value_equals",
            "key": "event.new_status",
            "value": "completed",
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
            workflow_id="#V#task_status_workflow",
            instance_id="instance-123",
            created_new=True,
        ),
    ) as mock_submit:
        skipped = launch_event_workflow(
            event_type="task.status_changed",
            event_id="task-1:pending->in_progress:now",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            event_payload={
                "task_concept_id": "#V#task_1",
                "previous_status": "pending",
                "new_status": "in_progress",
            },
        )
        triggered = launch_event_workflow(
            event_type="task.status_changed",
            event_id="task-1:in_progress->completed:later",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            event_payload={
                "task_concept_id": "#V#task_1",
                "previous_status": "in_progress",
                "new_status": "completed",
            },
        )

    assert skipped["triggered"] is False
    assert skipped["reason"] == "binding_condition_not_matched"
    assert skipped["binding_condition_result"] is False
    assert skipped["binding_id"] == binding.binding_id

    assert triggered["triggered"] is True
    assert triggered["binding_condition_result"] is True
    assert triggered["binding_id"] == binding.binding_id
    assert mock_submit.call_count == 1
    assert mock_submit.call_args is not None
    assert mock_submit.call_args.kwargs["inputs"]["status"] == "completed"


@pytest.mark.parametrize(
    "org_id, expected_namespace",
    [(None, "#V#user_alice"), ("#V#org_nao", "#V#user_alice@org_nao")],
)
@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_event_submission_preserves_personal_or_organisation_authority(
    mock_get_instance_manager, monkeypatch, org_id, expected_namespace
):
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")
    manager = MagicMock()
    manager.list_event_bindings.return_value = [
        EventWorkflowBinding.create(
            event_type="otter.meeting_collected",
            workflow_id="#V#meeting_followup_workflow",
            enabled=True,
            actor="test",
        )
    ]
    mock_get_instance_manager.return_value = manager
    with patch.object(
        workflow_event_service,
        "submit_verified_workflow_instance",
        return_value=_submission_result(
            workflow_id="#V#meeting_followup_workflow",
            instance_id="private-event-instance",
            created_new=True,
        ),
    ) as submit:
        result = launch_event_workflow(
            event_type="otter.meeting_collected",
            event_id="meeting:source-revision",
            user_id="#V#user_alice",
            org_id=org_id,
            event_payload={"meeting_concept_id": "#V#private_meeting"},
        )
    assert result["triggered"] is True
    assert submit.call_args.kwargs["org_id"] == org_id
    assert submit.call_args.kwargs["namespace"] == expected_namespace
    assert submit.call_args.kwargs["user_id"] == "#V#user_alice"


@patch("src.backend.services.workflow_event_integration_service.get_instance_manager")
def test_launch_event_workflow_applies_membership_binding_condition(
    mock_get_instance_manager: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EVENT_WORKFLOW_INTEGRATION_ENABLE", "1")
    monkeypatch.setenv("VON_DURABLE_WORKFLOWS_ENABLE", "1")

    binding = EventWorkflowBinding.create(
        event_type="relationship.added",
        workflow_id="#V#profile_refresh_workflow",
        condition={
            "kind": "context_value_in",
            "key": "event.predicate",
            "values": ["#V#has_research_interest", "#V#working_on_project"],
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
            workflow_id="#V#profile_refresh_workflow",
            instance_id="instance-456",
            created_new=True,
        ),
    ) as mock_submit:
        skipped = launch_event_workflow(
            event_type="relationship.added",
            event_id="rel-ignored",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            event_payload={
                "source_id": "#V#paper_1",
                "predicate": "#V#authored_by",
                "target_id": "#V#person_1",
            },
        )
        triggered = launch_event_workflow(
            event_type="relationship.added",
            event_id="rel-profile",
            user_id="#V#user_alice",
            org_id="#V#org_nao",
            event_payload={
                "source_id": "#V#person_1",
                "predicate": "#V#has_research_interest",
                "target_id": "#V#topic_1",
            },
        )

    assert skipped["triggered"] is False
    assert skipped["reason"] == "binding_condition_not_matched"
    assert triggered["triggered"] is True
    assert triggered["binding_condition_result"] is True
    assert mock_submit.call_count == 1


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
def test_launch_event_workflow_normalises_legacy_namespace_override(
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
            instance_id="instance-ns-legacy",
            created_new=True,
        ),
    ) as mock_submit:
        result = launch_event_workflow(
            event_type=EVENT_TYPE_TASK_CREATED,
            event_id="task-legacy-namespace",
            user_id=None,
            org_id=None,
            namespace="#V#user_alice/#V#org_nao",
            workflow_id="#V#task_event_workflow",
            inputs={"task_concept_id": "#V#task_99"},
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
    assert (
        result.get("cadence_policy_source")
        == "text_relation:#V#hasBackgroundLaunchPolicyJson"
    )
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

    with (
        patch(
            "src.backend.services.workflow_event_integration_service.submit_verified_workflow_instance",
        ) as mock_submit,
        patch(
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
    assert (
        called_args.kwargs["inputs"]["file_copy_concept_id"]
        == "#V#uploaded_file_copy_123"
    )
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
        {
            "success": True,
            "triggered": False,
            "event_type": EVENT_TYPE_VONTOLOGY_MUTATED,
        },
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


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_episode_evaluation_autotrigger_is_disabled_by_default(
    mock_launch_event_workflow: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.delenv("VON_EPISODE_EVALUATION_AUTOTRIGGER_ENABLE", raising=False)
    monkeypatch.delenv(
        "VON_WORKFLOW_INTROSPECTION_AUTOTRIGGER_ENABLE",
        raising=False,
    )

    result = maybe_launch_episode_evaluation_for_turn_completion_gate(
        request_id="req-default-off",
        session_id="sess-default-off",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        selected_workflow_id="#V#tool_calling_workflow",
        incident_text=None,
        maintenance_apply_repairs_default=False,
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["enabled"] is False
    assert result["reason"] == "autotrigger_disabled"
    mock_launch_event_workflow.assert_not_called()


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_episode_evaluation_for_turn_completion_gate_uses_event_binding(
    mock_launch_event_workflow: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EPISODE_EVALUATION_AUTOTRIGGER_ENABLE", "1")
    monkeypatch.setenv("VON_EPISODE_EVALUATION_MAX_DEPTH", "1")
    mock_launch_event_workflow.return_value = {
        "success": True,
        "triggered": True,
        "workflow_id": "#V#episode_evaluation_workflow",
        "instance_id": "wf-episode-1",
        "status": "pending",
    }

    result = maybe_launch_episode_evaluation_for_turn_completion_gate(
        request_id="req-1605",
        session_id="sess-1605",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        selected_workflow_id="#V#tool_calling_workflow",
        incident_text="Follow-up required after completion gate.",
        maintenance_apply_repairs_default=True,
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["episode_evaluation_depth"] == 1
    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == "turn_execution.completion_gate"
    assert "workflow_id" not in called_args.kwargs
    assert called_args.kwargs["inputs"]["request_id"] == "req-1605"
    assert called_args.kwargs["inputs"]["episode_evaluation_depth"] == 1


def test_maybe_launch_episode_evaluation_for_workflow_terminal_honours_depth_guard(
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EPISODE_EVALUATION_AUTOTRIGGER_ENABLE", "1")
    monkeypatch.setenv("VON_EPISODE_EVALUATION_MAX_DEPTH", "1")

    instance = SimpleNamespace(
        instance_id="inst-1605",
        workflow_id="#V#episode_evaluation_workflow",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        inputs={"episode_evaluation_depth": 1},
    )

    result = maybe_launch_episode_evaluation_for_workflow_terminal(
        instance=instance,
        terminal_status="completed",
        final_state="complete",
        termination_code="completed",
        termination_detail=None,
    )

    assert result["success"] is False
    assert result["triggered"] is False
    assert result["reason"] == "max_depth_reached"
    assert result["event_type"] == EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL


@patch("src.backend.services.workflow_event_integration_service.launch_event_workflow")
def test_maybe_launch_episode_evaluation_for_workflow_terminal_emits_terminal_event(
    mock_launch_event_workflow: MagicMock,
    monkeypatch,
) -> None:
    monkeypatch.setenv("VON_EPISODE_EVALUATION_AUTOTRIGGER_ENABLE", "1")
    monkeypatch.setenv("VON_EPISODE_EVALUATION_MAX_DEPTH", "2")
    mock_launch_event_workflow.return_value = {
        "success": True,
        "triggered": True,
        "workflow_id": "#V#episode_evaluation_workflow",
        "instance_id": "wf-episode-2",
        "status": "pending",
    }

    instance = SimpleNamespace(
        instance_id="inst-1605-b",
        workflow_id="#V#workflow_introspection_maintenance_workflow",
        namespace="#V#user@org",
        user_id="#V#user",
        org_id="#V#org",
        execution_trace_id="trace-1605",
        inputs={
            "request_id": "req-1605-b",
            "session_id": "sess-1605-b",
        },
    )

    result = maybe_launch_episode_evaluation_for_workflow_terminal(
        instance=instance,
        terminal_status="completed",
        final_state="complete",
        termination_code="completed",
        termination_detail=None,
    )

    assert result["success"] is True
    assert result["triggered"] is True
    assert result["episode_evaluation_depth"] == 1
    called_args = mock_launch_event_workflow.call_args
    assert called_args is not None
    assert called_args.kwargs["event_type"] == EVENT_TYPE_WORKFLOW_INSTANCE_TERMINAL
    assert called_args.kwargs["event_id"] == "inst-1605-b"
    assert called_args.kwargs["inputs"]["instance_id"] == "inst-1605-b"
    assert called_args.kwargs["inputs"]["selected_workflow_id"] == (
        "#V#workflow_introspection_maintenance_workflow"
    )
