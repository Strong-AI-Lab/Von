from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.integrations.internal_mcp.workflow_surface_capabilities import (
    tracked_workflow_surface_tool_names,
)
from src.backend.workflows.durable.models import (
    EventWorkflowBinding,
    WorkflowInstanceStatus,
    WorkflowSchedule,
)
from src.backend.workflows.durable.scheduler import WorkflowScheduler
from workflow_test_support import (
    bootstrap_authoritative_file_copy_workflows,
    bootstrap_authoritative_reasoning_recovery_workflows,
    bootstrap_authoritative_support_maintenance_workflows,
)


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
        trusted_actor_payload_fallback=True,
    )


def _patch_submit_verified_instance_success(monkeypatch) -> None:
    """Make success-path tests independent of external workflow authority state."""
    from src.backend.services.namespace_service import resolve_canonical_namespace
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    def _fake_submit_verified_workflow_instance(**kwargs):
        manager = kwargs["manager"]
        workflow_id = str(kwargs.get("workflow_id") or "").strip()
        user_id = str(kwargs.get("user_id") or "anonymous").strip() or "anonymous"
        org_id = str(kwargs.get("org_id") or "default").strip() or "default"
        namespace = (
            resolve_canonical_namespace(kwargs.get("namespace"), user_id, org_id)
            or str(kwargs.get("namespace") or "").strip()
        )
        if not namespace:
            raise AssertionError("workflow test helper expected canonical namespace")
        inputs = kwargs.get("inputs")
        max_retries_raw = kwargs.get("max_retries", 3)
        try:
            max_retries = int(max_retries_raw)
        except (TypeError, ValueError):
            max_retries = 3
        source_event_type = kwargs.get("source_event_type")
        source_event_id = kwargs.get("source_event_id")
        event_idempotency_key = kwargs.get("event_idempotency_key")
        if (
            isinstance(source_event_type, str)
            and source_event_type.strip()
            and isinstance(source_event_id, str)
            and source_event_id.strip()
            and isinstance(event_idempotency_key, str)
            and event_idempotency_key.strip()
        ):
            instance_id, created_new = manager.create_instance_for_event(
                workflow_id=workflow_id,
                user_id=user_id,
                org_id=org_id,
                namespace=namespace,
                event_idempotency_key=event_idempotency_key.strip(),
                source_event_type=source_event_type.strip(),
                source_event_id=source_event_id.strip(),
                inputs=dict(inputs) if isinstance(inputs, dict) else {},
                schedule_id=kwargs.get("schedule_id"),
                max_retries=max_retries,
                required_worker_build=kwargs.get("required_worker_build"),
            )
            status = "created" if created_new else "reused"
        else:
            created_new = True
            instance_id = manager.create_instance(
                workflow_id,
                user_id=user_id,
                org_id=org_id,
                namespace=namespace,
                inputs=dict(inputs) if isinstance(inputs, dict) else {},
                max_retries=max_retries,
                schedule_id=kwargs.get("schedule_id"),
                source_event_type=source_event_type,
                source_event_id=source_event_id,
                event_idempotency_key=event_idempotency_key,
                required_worker_build=kwargs.get("required_worker_build"),
            )
            status = "created"

        return WorkflowInstanceSubmissionResult(
            success=True,
            workflow_id=workflow_id,
            status=status,
            instance_id=instance_id,
            verification={
                "preflight_passed": True,
                "postflight_passed": True,
                "runnable_verification_success": True,
                "preflight": {"errors": []},
                "postflight": {"errors": []},
            },
            created_new=created_new,
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service.submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.scheduler.submit_verified_workflow_instance",
        _fake_submit_verified_workflow_instance,
    )


def _patch_read_only_registry(monkeypatch, *workflow_ids: str) -> None:
    class _StubRegistry:
        def all_workflow_ids(self) -> list[str]:
            return [wid for wid in workflow_ids if isinstance(wid, str) and wid.strip()]

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        lambda: _StubRegistry(),
    )


class _StubInstance:
    def __init__(
        self,
        *,
        instance_id: str,
        workflow_id: str,
        user_id: str,
        org_id: str,
        namespace: str,
        inputs: dict[str, object],
        max_retries: int,
    ) -> None:
        self.instance_id = instance_id
        self.workflow_id = workflow_id
        self.status = WorkflowInstanceStatus.PENDING
        self.current_state: str | None = None
        self.step_index = 0
        self.inputs = inputs
        self.outputs: dict[str, object] | None = None
        self.user_id = user_id
        self.org_id = org_id
        self.namespace = namespace
        self.error: str | None = None
        self.error_step: str | None = None
        self.schedule_id: str | None = None
        self.workflow_data: dict[str, object] = {}
        self.retry_count = 0
        self.max_retries = max_retries
        self.source_event_type: str | None = None
        self.source_event_id: str | None = None
        self.event_idempotency_key: str | None = None
        self.execution_trace_id: str | None = None
        self.manual_resume_required = False
        self.checkpoint_pause_receipt: dict[str, object] | None = None
        self.checkpoint_resume_receipt: dict[str, object] | None = None
        self.checkpoint_resume_count = 0
        self.created_at = datetime.now(timezone.utc)

    def to_status_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "current_state": self.current_state,
            "step_index": self.step_index,
            "error": self.error,
            "execution_trace_id": self.execution_trace_id,
            "manual_resume_required": self.manual_resume_required,
            "checkpoint_pause_receipt": self.checkpoint_pause_receipt,
            "checkpoint_resume_receipt": self.checkpoint_resume_receipt,
            "checkpoint_resume_count": self.checkpoint_resume_count,
        }


class _StubWorkflowManager:
    def __init__(self) -> None:
        self.instances: dict[str, _StubInstance] = {}
        self._event_index: dict[str, str] = {}
        self._counter = 0
        self.bindings: dict[tuple[str, str], EventWorkflowBinding] = {}

    def create_instance(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        inputs: dict[str, object] | None = None,
        max_retries: int = 3,
        **kwargs,
    ) -> str:
        self._counter += 1
        instance_id = f"#V#wf_instance_{self._counter}"
        self.instances[instance_id] = _StubInstance(
            instance_id=instance_id,
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=dict(inputs or {}),
            max_retries=max_retries,
        )
        instance = self.instances[instance_id]
        source_event_type = kwargs.get("source_event_type")
        source_event_id = kwargs.get("source_event_id")
        event_idempotency_key = kwargs.get("event_idempotency_key")
        if isinstance(source_event_type, str) and source_event_type.strip():
            instance.source_event_type = source_event_type.strip()
        if isinstance(source_event_id, str) and source_event_id.strip():
            instance.source_event_id = source_event_id.strip()
        if isinstance(event_idempotency_key, str) and event_idempotency_key.strip():
            instance.event_idempotency_key = event_idempotency_key.strip()
        return instance_id

    def create_instance_for_event(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        event_idempotency_key: str,
        source_event_type: str,
        source_event_id: str,
        inputs: dict[str, object] | None = None,
        schedule_id: str | None = None,
        max_retries: int = 3,
        required_worker_build: str | None = None,
    ) -> tuple[str, bool]:
        existing_instance_id = self._event_index.get(event_idempotency_key)
        if isinstance(existing_instance_id, str):
            return existing_instance_id, False

        instance_id = self.create_instance(
            workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=inputs,
            max_retries=max_retries,
            schedule_id=schedule_id,
            source_event_type=source_event_type,
            source_event_id=source_event_id,
            event_idempotency_key=event_idempotency_key,
            required_worker_build=required_worker_build,
        )
        self._event_index[event_idempotency_key] = instance_id
        return instance_id, True

    def list_instances(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
        namespace: str | None = None,
        status: object | None = None,
        workflow_id: str | None = None,
        source_event_type: str | None = None,
        source_event_id: str | None = None,
        conversation_session_id: str | None = None,
        request_id: str | None = None,
        from_utc: datetime | str | None = None,
        to_utc: datetime | str | None = None,
        limit: int = 50,
    ) -> list[_StubInstance]:
        def _parse(value: datetime | str | None) -> datetime | None:
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
            if isinstance(value, str) and value.strip():
                return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return None

        from_dt = _parse(from_utc)
        to_dt = _parse(to_utc)
        status_value = getattr(status, "value", status)
        instances = list(self.instances.values())
        filtered = [
            inst
            for inst in instances
            if (user_id is None or inst.user_id == user_id)
            and (org_id is None or inst.org_id == org_id)
            and (namespace is None or inst.namespace == namespace)
            and (status_value is None or inst.status.value == status_value)
            and (workflow_id is None or inst.workflow_id == workflow_id)
            and (
                source_event_type is None or inst.source_event_type == source_event_type
            )
            and (source_event_id is None or inst.source_event_id == source_event_id)
            and (
                conversation_session_id is None
                or str(inst.inputs.get("conversation_session_id"))
                == conversation_session_id
            )
            and (request_id is None or str(inst.inputs.get("turn_id")) == request_id)
            and (from_dt is None or inst.created_at >= from_dt)
            and (to_dt is None or inst.created_at <= to_dt)
        ]
        return filtered[:limit]

    def get_instance(self, instance_id: str) -> _StubInstance | None:
        return self.instances.get(instance_id)

    def checkpoint(
        self,
        instance_id: str,
        *,
        state: str | None = None,
        step_index: int | None = None,
        workflow_data: dict[str, object] | None = None,
    ) -> bool:
        instance = self.instances.get(instance_id)
        if instance is None:
            return False
        instance.status = WorkflowInstanceStatus.RUNNING
        if state is not None:
            instance.current_state = state
        if isinstance(step_index, int):
            instance.step_index = step_index
        if isinstance(workflow_data, dict):
            instance.workflow_data = dict(workflow_data)
        return True

    def mark_failed(
        self,
        instance_id: str,
        *,
        error: str = "failed",
        error_step: str | None = None,
        increment_retry: bool = False,
        outputs: dict[str, object] | None = None,
    ) -> bool:
        instance = self.instances.get(instance_id)
        if instance is None:
            return False
        instance.status = WorkflowInstanceStatus.FAILED
        instance.error = error
        instance.error_step = error_step
        if outputs is not None:
            instance.outputs = dict(outputs)
        if increment_retry:
            instance.retry_count += 1
        return True

    def mark_cancelled(self, instance_id: str) -> bool:
        instance = self.instances.get(instance_id)
        if instance is None:
            return False
        if instance.status in {
            WorkflowInstanceStatus.COMPLETED,
            WorkflowInstanceStatus.FAILED,
            WorkflowInstanceStatus.CANCELLED,
        }:
            return False
        instance.status = WorkflowInstanceStatus.CANCELLED
        return True

    def resume_instance(self, instance_id: str) -> dict[str, object] | None:
        instance = self.instances.get(instance_id)
        if (
            instance is None
            or instance.status != WorkflowInstanceStatus.PAUSED
            or not instance.manual_resume_required
        ):
            return None
        instance.status = WorkflowInstanceStatus.PENDING
        instance.manual_resume_required = False
        instance.checkpoint_resume_count += 1
        instance.checkpoint_resume_receipt = {
            "schema_version": "workflow_checkpoint_resume_receipt.v1",
            "instance_id": instance_id,
            "workflow_id": instance.workflow_id,
            "status": "pending",
            "checkpoint_state": instance.current_state,
            "checkpoint_step_index": instance.step_index,
            "resume_count": instance.checkpoint_resume_count,
            "same_instance_resume": True,
            "claim_token": getattr(instance, "claim_token", None),
        }
        return dict(instance.checkpoint_resume_receipt)

    def reset_for_retry(self, instance_id: str) -> bool:
        instance = self.instances.get(instance_id)
        if instance is None:
            return False
        if instance.status != WorkflowInstanceStatus.FAILED:
            return False
        if instance.retry_count >= instance.max_retries:
            return False
        instance.status = WorkflowInstanceStatus.PENDING
        instance.error = None
        instance.error_step = None
        return True

    def upsert_event_binding(
        self,
        *,
        event_type: str,
        workflow_id: str,
        input_mapping: dict[str, str] | None = None,
        condition: dict[str, object] | None = None,
        enabled: bool = True,
        actor: str | None = None,
        replace_existing: bool = False,
    ) -> tuple[EventWorkflowBinding, bool, bool]:
        key = (event_type, workflow_id)
        mapping = dict(input_mapping or {})
        condition_clean = dict(condition) if isinstance(condition, dict) else None
        existing = self.bindings.get(key)
        if existing is not None:
            if (
                existing.input_mapping == mapping
                and existing.condition == condition_clean
                and bool(existing.enabled) == bool(enabled)
            ):
                return existing, False, False
            if not replace_existing:
                raise ValueError("binding_conflict")
            existing.input_mapping = mapping
            existing.condition = condition_clean
            existing.enabled = bool(enabled)
            existing.updated_by = actor
            existing.revision += 1
            return existing, False, True

        created = EventWorkflowBinding.create(
            event_type=event_type,
            workflow_id=workflow_id,
            input_mapping=mapping,
            condition=condition_clean,
            enabled=bool(enabled),
            actor=actor,
        )
        self.bindings[key] = created
        return created, True, False

    def list_event_bindings(
        self,
        *,
        event_type: str | None = None,
        enabled_only: bool = False,
        limit: int = 100,
    ) -> list[EventWorkflowBinding]:
        values = list(self.bindings.values())
        if event_type is not None:
            values = [item for item in values if item.event_type == event_type]
        if enabled_only:
            values = [item for item in values if bool(item.enabled)]
        return values[:limit]

    def get_event_binding(self, binding_id: str) -> EventWorkflowBinding | None:
        binding_id_clean = str(binding_id or "").strip()
        if not binding_id_clean:
            return None
        for binding in self.bindings.values():
            if binding.binding_id == binding_id_clean:
                return binding
        return None

    def set_event_binding_enabled(
        self,
        binding_id: str,
        *,
        enabled: bool,
        actor: str | None = None,
    ) -> EventWorkflowBinding | None:
        existing = self.get_event_binding(binding_id)
        if existing is None:
            return None
        if bool(existing.enabled) == bool(enabled):
            return existing
        existing.enabled = bool(enabled)
        existing.updated_by = actor
        existing.revision += 1
        return existing

    def delete_event_binding(self, binding_id: str) -> EventWorkflowBinding | None:
        existing = self.get_event_binding(binding_id)
        if existing is None:
            return None
        key = (existing.event_type, existing.workflow_id)
        self.bindings.pop(key, None)
        return existing


class _InMemoryScheduleWorkflowManager:
    def __init__(self) -> None:
        self.schedules: dict[str, Any] = {}
        self.instances: list[dict[str, object]] = []
        self._instance_lookup: dict[str, _StubInstance] = {}
        self._instance_counter = 0

    def create_schedule(self, schedule):
        self.schedules[schedule.schedule_id] = schedule
        return schedule.schedule_id

    def get_schedule(self, schedule_id: str):
        return self.schedules.get(schedule_id)

    def list_schedules(self, user_id=None, enabled_only=False, limit=50):
        schedules = list(self.schedules.values())
        if user_id:
            schedules = [s for s in schedules if getattr(s, "user_id", None) == user_id]
        if enabled_only:
            schedules = [s for s in schedules if bool(getattr(s, "enabled", False))]
        return schedules[:limit]

    def find_due_schedules(self, limit=50):
        now = datetime.now(timezone.utc)
        due = []
        for schedule in self.schedules.values():
            next_run_at = getattr(schedule, "next_run_at", None)
            if not bool(getattr(schedule, "enabled", False)):
                continue
            if isinstance(next_run_at, datetime) and next_run_at <= now:
                due.append(schedule)
        return due[:limit]

    def create_instance(
        self,
        workflow_id: str,
        *,
        user_id: str,
        org_id: str,
        namespace: str,
        inputs: dict | None = None,
        schedule_id: str | None = None,
        **_kwargs,
    ) -> str:
        self._instance_counter += 1
        instance_id = f"#V#instance_{self._instance_counter}"
        self.instances.append(
            {
                "instance_id": instance_id,
                "workflow_id": workflow_id,
                "user_id": user_id,
                "org_id": org_id,
                "namespace": namespace,
                "inputs": dict(inputs or {}),
                "schedule_id": schedule_id,
            }
        )
        stub_instance = _StubInstance(
            instance_id=instance_id,
            workflow_id=workflow_id,
            user_id=user_id,
            org_id=org_id,
            namespace=namespace,
            inputs=dict(inputs or {}),
            max_retries=3,
        )
        stub_instance.schedule_id = schedule_id
        self._instance_lookup[instance_id] = stub_instance
        return instance_id

    def get_instance(self, instance_id: str):
        return self._instance_lookup.get(instance_id)

    def checkpoint(
        self,
        instance_id: str,
        *,
        state: str | None = None,
        step_index: int | None = None,
        workflow_data: dict[str, object] | None = None,
    ) -> bool:
        instance = self._instance_lookup.get(instance_id)
        if instance is None:
            return False
        instance.status = WorkflowInstanceStatus.RUNNING
        if state is not None:
            instance.current_state = state
        if isinstance(step_index, int):
            instance.step_index = step_index
        if isinstance(workflow_data, dict):
            instance.workflow_data = dict(workflow_data)
        return True

    def mark_failed(
        self,
        instance_id: str,
        *,
        error: str = "failed",
        error_step: str | None = None,
        increment_retry: bool = False,
        outputs: dict[str, object] | None = None,
    ) -> bool:
        instance = self._instance_lookup.get(instance_id)
        if instance is None:
            return False
        instance.status = WorkflowInstanceStatus.FAILED
        instance.error = error
        instance.error_step = error_step
        if outputs is not None:
            instance.outputs = dict(outputs)
        if increment_retry:
            instance.retry_count += 1
        return True

    def reset_for_retry(self, instance_id: str) -> bool:
        instance = self._instance_lookup.get(instance_id)
        if instance is None:
            return False
        if instance.status != WorkflowInstanceStatus.FAILED:
            return False
        if instance.retry_count >= instance.max_retries:
            return False
        instance.status = WorkflowInstanceStatus.PENDING
        instance.error = None
        instance.error_step = None
        return True

    def update_schedule_after_run(self, schedule_id: str, *, next_run_at=None):
        schedule = self.schedules.get(schedule_id)
        if schedule is None:
            return False
        schedule.next_run_at = next_run_at
        schedule.last_run_at = datetime.now(timezone.utc)
        return True


def test_workflow_list_definitions_exists_and_returns_data():
    bootstrap_authoritative_file_copy_workflows()
    bootstrap_authoritative_reasoning_recovery_workflows()
    bootstrap_authoritative_support_maintenance_workflows()
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_list_definitions" in methods

    handler = catalogue.get("workflow_list_definitions").handler
    # Keep the sample wide enough that newly-registered durable workflows do
    # not get paged out and cause false negatives.
    result = handler(limit=200)

    assert result.get("success") is True
    assert "definitions" in result
    assert "count" in result
    assert "parity_inventory" in result
    assert "baseline_telemetry" in result
    assert "capability_matrix" in result
    assert result["count"] >= 0

    # Check default workflows are present (from registry)
    def_ids = [d["workflow_id"] for d in result["definitions"]]
    # We expect durable workflows to be registered by the factory
    # (rag_sync_workflow is registered in build_durable_workflow_registry)
    assert "#V#rag_text_relation_sync_workflow" in def_ids
    assert "#V#enrichment_workflow" in def_ids
    assert "#V#workflow_introspection_maintenance_workflow" in def_ids
    assert "#V#mongo_query_diagnostics_maintenance_workflow" in def_ids
    assert "#V#entity_identity_resolution_workflow" in def_ids
    assert "#V#jira_task_incremental_import_workflow" in def_ids
    assert "#V#planning_workflow" in def_ids
    assert "#V#rumination_workflow" in def_ids
    assert "#V#parent_specificity_concept_dossier_workflow" in def_ids
    assert "#V#parent_specificity_rumination_workflow" in def_ids
    assert "#V#file_copy_typing_workflow" in def_ids
    assert "#V#file_copy_upload_classification_workflow" in def_ids
    assert "#V#file_copy_upload_handler_workflow" in def_ids
    assert "#V#file_copy_interpretation_workflow" in def_ids
    source_by_workflow_id = {
        item["workflow_id"]: str(item.get("source") or "").strip().lower()
        for item in result["definitions"]
    }
    assert source_by_workflow_id["#V#rag_text_relation_sync_workflow"] == "vontology"
    assert source_by_workflow_id["#V#enrichment_workflow"] == "vontology"
    assert (
        source_by_workflow_id["#V#workflow_introspection_maintenance_workflow"]
        == "vontology"
    )
    assert (
        source_by_workflow_id["#V#mongo_query_diagnostics_maintenance_workflow"]
        == "vontology"
    )
    assert (
        source_by_workflow_id["#V#entity_identity_resolution_workflow"] == "vontology"
    )
    assert (
        source_by_workflow_id["#V#jira_task_incremental_import_workflow"] == "vontology"
    )
    assert source_by_workflow_id["#V#planning_workflow"] == "vontology"
    assert source_by_workflow_id["#V#rumination_workflow"] == "vontology"
    assert (
        source_by_workflow_id["#V#parent_specificity_concept_dossier_workflow"]
        == "vontology"
    )
    assert (
        source_by_workflow_id["#V#parent_specificity_rumination_workflow"]
        == "vontology"
    )
    assert source_by_workflow_id["#V#file_copy_typing_workflow"] == "vontology"
    assert (
        source_by_workflow_id["#V#file_copy_upload_classification_workflow"]
        == "vontology"
    )
    assert source_by_workflow_id["#V#file_copy_upload_handler_workflow"] == "vontology"
    assert source_by_workflow_id["#V#file_copy_interpretation_workflow"] == "vontology"
    assert any("description_source" in d for d in result["definitions"])
    assert any("definition_identity" in d for d in result["definitions"])
    assert any("background_launch_policy_source" in d for d in result["definitions"])
    first_definition = result["definitions"][0]
    identity = first_definition.get("definition_identity") or {}
    assert identity.get("schema_version") == "workflow_definition_identity.v1"
    assert identity.get("definition_hash") or identity.get("build_state") == (
        "pending_lazy_definition"
    )

    parity_inventory = result["parity_inventory"]
    assert isinstance(parity_inventory, dict)
    assert "counts" in parity_inventory
    assert "summary_text" in parity_inventory
    assert "diagnostics" in parity_inventory
    assert "parity_policy" in parity_inventory
    assert "workflow_purity" in parity_inventory
    assert "workflow_description_quality" in parity_inventory
    diagnostics = parity_inventory["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert "drift_detected" in diagnostics
    assert "severity" in diagnostics
    assert "reason_codes" in diagnostics
    workflow_purity = parity_inventory["workflow_purity"]
    assert isinstance(workflow_purity, dict)
    assert "counters" in workflow_purity
    assert "baseline" in workflow_purity
    workflow_description_quality = parity_inventory["workflow_description_quality"]
    assert isinstance(workflow_description_quality, dict)
    assert "counts" in workflow_description_quality

    baseline_telemetry = result["baseline_telemetry"]
    assert isinstance(baseline_telemetry, dict)
    assert "workflow_discovery_executable_hit_ratio" in baseline_telemetry
    assert "generic_fallback_mcp_invocations_total" in baseline_telemetry

    capability_matrix = result["capability_matrix"]
    assert isinstance(capability_matrix, dict)
    assert capability_matrix.get("docs_reference")
    surfaces = capability_matrix.get("surfaces") or {}
    assert "internal_mcp_gateway" in surfaces
    assert "vontology_mcp_stdio_server" in surfaces
    assert "vonrag_mcp_stdio_server" in surfaces


def test_workflow_list_definitions_gateway_invoke_success_path():
    bootstrap_authoritative_file_copy_workflows()
    bootstrap_authoritative_reasoning_recovery_workflows()
    bootstrap_authoritative_support_maintenance_workflows()
    gateway = _build_gateway()
    result = gateway.invoke("workflow_list_definitions", {"limit": 10})
    payload = result.payload
    assert payload.get("success") is True
    assert "definitions" in payload
    assert "parity_inventory" in payload
    assert "capability_matrix" in payload


def test_workflow_validate_candidate_exists_and_returns_data(monkeypatch):
    monkeypatch.setattr(
        "src.backend.workflows.workflow_studio_service.validate_workflow_candidate",
        lambda workflow_id, **kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "candidate_validation": {
                "valid": True,
                "validation_profile": kwargs.get("validation_profile")
                or "generation_safe",
            },
        },
    )

    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_validate_candidate" in methods

    handler = catalogue.get("workflow_validate_candidate").handler
    result = handler(
        workflow_id="#V#candidate_workflow",
        authoring_spec={"workflow_id": "#V#candidate_workflow"},
        validation_profile="contract_only",
    )

    assert result == {
        "success": True,
        "workflow_id": "#V#candidate_workflow",
        "candidate_validation": {
            "valid": True,
            "validation_profile": "contract_only",
        },
    }


def test_workflow_validate_candidate_gateway_invoke_success_path(monkeypatch):
    monkeypatch.setattr(
        "src.backend.workflows.workflow_studio_service.validate_workflow_candidate",
        lambda workflow_id, **kwargs: {
            "success": True,
            "workflow_id": workflow_id,
            "candidate_validation": {
                "valid": True,
                "validation_profile": kwargs.get("validation_profile")
                or "generation_safe",
            },
            "preview": {"definition_identity": {"hash": "candidate-hash"}},
        },
    )

    gateway = _build_gateway()
    result = gateway.invoke(
        "workflow_validate_candidate",
        {
            "workflow_id": "#V#candidate_workflow",
            "authoring_spec": {"workflow_id": "#V#candidate_workflow"},
            "validation_profile": "contract_only",
            "include_preview": True,
        },
    )
    payload = result.payload

    assert payload.get("success") is True
    assert payload.get("workflow_id") == "#V#candidate_workflow"
    assert payload.get("candidate_validation", {}).get("valid") is True
    assert payload.get("candidate_validation", {}).get("validation_profile") == (
        "contract_only"
    )
    assert payload.get("preview", {}).get("definition_identity", {}).get("hash") == (
        "candidate-hash"
    )


def test_workflow_list_definitions_skips_bootstrap_writes():
    with patch(
        "src.backend.workflows.workflow_concept_authority_service.bootstrap_workflow_concepts",
        side_effect=AssertionError("workflow_list_definitions should be read-only"),
    ):
        handler = build_default_catalogue().get("workflow_list_definitions").handler
        result = handler(limit=10)

    assert result.get("success") is True


def test_workflow_list_definitions_filters_warm_registry_by_ambient_actor(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module
    from src.backend.security import access_control

    public_workflow_id = "#V#public_profile_workflow"
    team_workflow_id = "#V#team_profile_workflow"
    private_workflow_id = "#V#private_profile_workflow"
    all_workflow_ids = [
        private_workflow_id,
        public_workflow_id,
        team_workflow_id,
    ]

    class _Collection:
        _documents = {
            public_workflow_id: {
                "concept_id": public_workflow_id,
                "relationships": {},
            },
            team_workflow_id: {
                "concept_id": team_workflow_id,
                "relationships": {
                    "#V#specific_to_organisation": ["#V#team_org"]
                },
            },
            private_workflow_id: {
                "concept_id": private_workflow_id,
                "relationships": {
                    "#V#specific_to_user": ["#V#private_owner"]
                },
            },
        }

        def find(self, query, _projection):
            requested_ids = set(query["concept_id"]["$in"])
            return [
                document
                for concept_id, document in self._documents.items()
                if concept_id in requested_ids
            ]

    class _WarmRegistry:
        def all_workflow_ids(self):
            # This intentionally remains global and actor-agnostic.
            return list(all_workflow_ids)

    monkeypatch.setattr(
        access_control,
        "get_concepts_collection",
        lambda: _Collection(),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        lambda **_kwargs: _WarmRegistry(),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_or_build_workflow_registry_inventory_snapshot",
        lambda **_kwargs: {
            "build_state": "ready",
            "registry_workflow_ids": list(all_workflow_ids),
            "graph_warnings_by_workflow_id": {
                public_workflow_id: [],
                private_workflow_id: ["private metadata"],
            },
            "summary_text": f"Includes {private_workflow_id}",
        },
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.build_workflow_listing_entry",
        lambda **kwargs: {
            "workflow_id": kwargs["workflow_id"],
            "description": f"Description for {kwargs['workflow_id']}",
        },
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_baseline_telemetry.get_workflow_baseline_telemetry_snapshot",
        lambda: {
            "recent_mutation_guardrail_events": [
                {
                    "workflow_id": private_workflow_id,
                    "conversation_session_id": "private-session-2580",
                    "turn_id": "private-turn-2580",
                }
            ]
        },
    )
    monkeypatch.setattr(
        catalogue_module,
        "build_default_catalogue",
        lambda: SimpleNamespace(list_methods=lambda: []),
    )
    monkeypatch.setattr(
        catalogue_module,
        "build_workflow_surface_capability_matrix",
        lambda *, internal_method_names: {"surfaces": {}},
    )

    handler = build_default_catalogue().get("workflow_list_definitions").handler
    with access_control.override_current_actor(
        user_concept_id="#V#team_member",
        organisation_concept_id="#V#team_org",
    ):
        result = handler(
            limit=10,
            # Caller-supplied identity hints must not override ambient authority.
            user_concept_id="#V#private_owner",
            organisation_concept_id="#V#other_org",
        )

    assert result["success"] is True
    assert [item["workflow_id"] for item in result["definitions"]] == [
        public_workflow_id,
        team_workflow_id,
    ]
    assert result["count"] == 2
    serialised = json.dumps(result, sort_keys=True)
    assert private_workflow_id not in serialised
    assert "private metadata" not in serialised
    assert "private-session-2580" not in serialised
    assert "private-turn-2580" not in serialised
    assert result["baseline_telemetry"] == {
        "available": False,
        "scope": "trusted_operator_only",
    }

    untrusted_gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    forged = untrusted_gateway.invoke(
        "workflow_list_definitions",
        {
            "limit": 10,
            "user_id": "#V#private_owner",
            "org_id": "#V#team_org",
        },
    ).payload

    assert forged["success"] is True
    assert [item["workflow_id"] for item in forged["definitions"]] == [
        public_workflow_id,
    ]
    forged_serialised = json.dumps(forged, sort_keys=True)
    assert private_workflow_id not in forged_serialised
    assert team_workflow_id not in forged_serialised
    assert "private metadata" not in forged_serialised
    assert "private-session-2580" not in forged_serialised
    assert "private-turn-2580" not in forged_serialised
    assert forged["baseline_telemetry"] == {
        "available": False,
        "scope": "trusted_operator_only",
    }


def test_workflow_list_definitions_requests_pending_inventory_when_snapshot_absent(
    monkeypatch,
):
    class _Registry:
        def __init__(self) -> None:
            self._definition = SimpleNamespace(
                initial_state="start",
                purpose="Testing workflow",
                metadata={},
            )
            self._registration = SimpleNamespace(
                definition=self._definition,
                source="vontology",
                purpose="Testing workflow",
            )

        def all_workflow_ids(self) -> list[str]:
            return ["#V#meeting_invitation_testing_workflow"]

        def peek_registration(self, workflow_id: str):
            if workflow_id == "#V#meeting_invitation_testing_workflow":
                return self._registration
            return None

        def get_registration_source(
            self, workflow_id: str, *, resolve_lazy: bool = False
        ):
            if workflow_id == "#V#meeting_invitation_testing_workflow":
                return "vontology"
            return None

    handler = build_default_catalogue().get("workflow_list_definitions").handler
    calls: dict[str, Any] = {}

    def _build_registry(**kwargs):
        calls["registry_kwargs"] = dict(kwargs)
        return _Registry()

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        _build_registry,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_or_build_workflow_registry_inventory_snapshot",
        lambda **kwargs: (
            calls.update(kwargs)
            or {
                "build_state": "pending_background_build",
                "counts": {},
                "summary_text": "pending",
                "diagnostics": {"reason_codes": ["inventory_pending_background_build"]},
                "parity_policy": {"mode": "fail", "drift_detected": False},
                "workflow_purity": {"counters": {}, "baseline": {}},
            }
        ),
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.build_workflow_listing_entry",
        lambda **_kwargs: {
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "description": "Testing workflow",
            "description_source": "registration",
            "initial_state": "start",
            "source": "vontology",
            "background_launch_policy": None,
            "background_launch_policy_source": "none",
            "definition_identity": {
                "schema_version": "workflow_definition_identity.v1",
                "definition_hash": "pending-hash",
            },
            "definition_loaded": False,
        },
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_baseline_telemetry.get_workflow_baseline_telemetry_snapshot",
        lambda: {},
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue.build_default_catalogue",
        lambda: SimpleNamespace(list_methods=lambda: []),
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue.build_workflow_surface_capability_matrix",
        lambda *, internal_method_names: {"surfaces": {}},
    )

    result = handler(limit=10)

    assert result.get("success") is True
    assert calls["registry_kwargs"]["defer_parity_work"] is True
    assert calls["allow_sync_build"] is False
    assert result["parity_inventory"]["build_state"] == "pending_background_build"
    assert result["baseline_telemetry"] == {}


def test_workflow_list_definitions_does_not_resolve_lazy_definitions_for_summary(
    monkeypatch,
):
    class _LazyRegistration:
        purpose = "Lazy workflow"
        source = "vontology"

    class _Registry:
        def all_workflow_ids(self) -> list[str]:
            return ["#V#meeting_invitation_testing_workflow"]

        def peek_registration(self, workflow_id: str):
            if workflow_id == "#V#meeting_invitation_testing_workflow":
                return _LazyRegistration()
            return None

        def get_registration(self, workflow_id: str):
            raise AssertionError("lazy definition resolution should not be required")

        def get(self, workflow_id: str):
            raise AssertionError("workflow definition should not be loaded")

        def get_registration_source(
            self, workflow_id: str, *, resolve_lazy: bool = False
        ):
            if workflow_id == "#V#meeting_invitation_testing_workflow":
                return "vontology"
            return None

    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.build_durable_workflow_registry_read_only",
        lambda **_kwargs: _Registry(),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.get_or_build_workflow_registry_inventory_snapshot",
        lambda **_kwargs: {"build_state": "pending_background_build"},
    )
    monkeypatch.setattr(
        "src.backend.workflows.vontology_loader.resolve_workflow_description",
        lambda *_args, **_kwargs: ("Lazy workflow", "registration"),
    )
    monkeypatch.setattr(
        "src.backend.workflows.vontology_loader.resolve_workflow_initial_step",
        lambda _workflow_id: "#V#start",
    )
    monkeypatch.setattr(
        "src.backend.workflows.vontology_loader.resolve_workflow_background_launch_policy",
        lambda _workflow_id: (None, "none"),
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_baseline_telemetry.get_workflow_baseline_telemetry_snapshot",
        lambda: {"workflow_discovery_executable_hit_ratio": 1.0},
    )

    handler = build_default_catalogue().get("workflow_list_definitions").handler
    result = handler(limit=10)

    assert result.get("success") is True
    definition = result["definitions"][0]
    assert definition["definition_loaded"] is False
    identity = definition["definition_identity"]
    assert identity["build_state"] == "pending_lazy_definition"
    assert identity["reason_code"] == "lazy_definition_not_loaded"
    assert identity["definition_hash"] is None


def test_workflow_list_instances_gateway_invoke_error_path():
    gateway = _build_gateway()
    result = gateway.invoke("workflow_list_instances", {"status": "invalid-status"})
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_status"


def test_workflow_create_list_get_instance_gateway_paths(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": workflow_id,
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
        },
    ).payload
    assert created.get("success") is True
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    listed = gateway.invoke(
        "workflow_list_instances",
        {
            "namespace": "#V#user/#V#org",
            "status": "pending",
            "limit": 10,
        },
    ).payload
    assert listed.get("success") is True
    assert listed.get("count") == 1
    assert listed["instances"][0]["instance_id"] == instance_id

    detail = gateway.invoke(
        "workflow_get_instance",
        {"instance_id": instance_id},
    ).payload
    assert detail.get("success") is True
    assert detail.get("instance_id") == instance_id
    assert detail.get("workflow_id") == workflow_id
    assert detail.get("namespace") == "#V#user@org"
    assert detail.get("inputs") == {"seed": "value"}


def test_workflow_get_instance_can_await_without_restarting_instance(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()
    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
        },
    ).payload
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)
    awaited: dict[str, object] = {}

    def _await_existing(
        awaited_manager,
        awaited_instance_id,
        *,
        timeout_seconds,
        poll_interval_seconds,
    ):
        awaited.update(
            {
                "manager": awaited_manager,
                "instance_id": awaited_instance_id,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
            }
        )
        instance = awaited_manager.get_instance(awaited_instance_id)
        assert instance is not None
        instance.status = WorkflowInstanceStatus.COMPLETED
        instance.outputs = {"reused": True}
        return SimpleNamespace(instance=instance, poll_count=4, timed_out=False)

    monkeypatch.setattr(
        "src.backend.workflows.durable.execution_observability.await_workflow_terminal_state",
        _await_existing,
    )

    detail = gateway.invoke(
        "workflow_get_instance",
        {
            "instance_id": instance_id,
            "await_terminal": True,
            "timeout_seconds": 100,
            "poll_interval_seconds": 0.5,
        },
    ).payload

    assert manager._counter == 1
    assert awaited == {
        "manager": manager,
        "instance_id": instance_id,
        "timeout_seconds": 90.0,
        "poll_interval_seconds": 0.5,
    }
    assert detail.get("success") is True
    assert detail.get("status") == "completed"
    assert detail.get("outputs") == {"reused": True}
    assert detail.get("await_terminal") is True
    assert detail.get("poll_count") == 4
    assert detail.get("timed_out") is False


def test_workflow_get_instance_wait_expiry_returns_current_state_without_restart(
    monkeypatch,
):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()
    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
        },
    ).payload
    instance_id = created["instance_id"]
    awaited: list[dict[str, object]] = []

    def _await_existing(
        awaited_manager,
        awaited_instance_id,
        *,
        timeout_seconds,
        poll_interval_seconds,
    ):
        awaited.append(
            {
                "manager": awaited_manager,
                "instance_id": awaited_instance_id,
                "timeout_seconds": timeout_seconds,
                "poll_interval_seconds": poll_interval_seconds,
            }
        )
        return SimpleNamespace(
            instance=awaited_manager.get_instance(awaited_instance_id),
            poll_count=3,
            timed_out=True,
            elapsed_time_enforcement="advisory",
            advisory_exceeded=True,
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.execution_observability.await_workflow_terminal_state",
        _await_existing,
    )

    detail = gateway.invoke(
        "workflow_get_instance",
        {
            "instance_id": instance_id,
            "await_terminal": True,
            "timeout_seconds": 0,
            "poll_interval_seconds": 0,
        },
    ).payload

    assert manager._counter == 1
    assert awaited == [
        {
            "manager": manager,
            "instance_id": instance_id,
            "timeout_seconds": 0.0,
            "poll_interval_seconds": 0.05,
        }
    ]
    assert detail.get("success") is True
    assert detail.get("status") == "pending"
    assert detail.get("poll_count") == 3
    assert detail.get("timed_out") is False
    assert detail.get("elapsed_time_enforcement") == "advisory"
    assert detail.get("advisory_exceeded") is True
    assert detail.get("hard_timeout_seconds") is None

    for field, value in (
        ("timeout_seconds", float("inf")),
        ("poll_interval_seconds", float("nan")),
    ):
        invalid = gateway.invoke(
            "workflow_get_instance",
            {
                "instance_id": instance_id,
                "await_terminal": True,
                field: value,
            },
        ).payload
        assert invalid.get("success") is False
        assert invalid.get("error_code") == "invalid_parameter"
        assert invalid.get("error_details") == {"invalid": [field]}
        assert invalid.get("status") == "not_started"
        assert invalid.get("mutation_outcome") == "not_started"
        assert invalid.get("changed") is False
    assert len(awaited) == 1


def test_workflow_get_instance_exposes_failed_outputs(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": workflow_id,
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
        },
    ).payload
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    assert manager.mark_failed(
        instance_id,
        error="metadata_validation_failed",
        error_step="infer_expected_outcome",
        outputs={
            "schema_version": "workflow_failed_outputs.v1",
            "failed_action_diagnostics": {"action_id": "llm.action"},
        },
    )

    detail = gateway.invoke(
        "workflow_get_instance",
        {"instance_id": instance_id},
    ).payload
    assert detail.get("success") is True
    assert detail.get("status") == "failed"
    assert detail.get("outputs") == {
        "schema_version": "workflow_failed_outputs.v1",
        "failed_action_diagnostics": {"action_id": "llm.action"},
    }


def test_workflow_instance_tools_are_exactly_scoped_to_preexisting_actor(
    monkeypatch,
):
    from src.backend.security import access_control

    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    own = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#shared_user",
            "org_id": "#V#own_org",
            "namespace": "#V#shared_user@own_org",
        },
    ).payload
    foreign = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#shared_user",
            "org_id": "#V#foreign_org",
            "namespace": "#V#shared_user@foreign_org",
        },
    ).payload
    own_id = own["instance_id"]
    foreign_id = foreign["instance_id"]

    def _unexpected_wait(*_args, **_kwargs):
        raise AssertionError("an unauthorised instance must be rejected before waiting")

    monkeypatch.setattr(
        "src.backend.workflows.durable.execution_observability.await_workflow_terminal_state",
        _unexpected_wait,
    )

    with access_control.override_current_actor(
        user_concept_id="#V#shared_user",
        organisation_concept_id="#V#own_org",
    ):
        listed = gateway.invoke("workflow_list_instances", {}).payload
        denied = [
            gateway.invoke(
                "workflow_get_instance",
                {"instance_id": foreign_id, "await_terminal": True},
            ).payload,
            gateway.invoke(
                "workflow_cancel_instance",
                {"instance_id": foreign_id},
            ).payload,
            gateway.invoke(
                "workflow_retry_instance",
                {"instance_id": foreign_id},
            ).payload,
        ]

    assert listed["success"] is True
    assert [item["instance_id"] for item in listed["instances"]] == [own_id]
    assert all(result["success"] is False for result in denied)
    assert all(result["error_code"] == "not_found" for result in denied)
    assert manager.instances[foreign_id].status == WorkflowInstanceStatus.PENDING


def test_workflow_get_instance_revalidates_actor_after_wait(monkeypatch):
    from src.backend.security import access_control

    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()
    own = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#shared_user",
            "org_id": "#V#own_org",
            "namespace": "#V#shared_user@own_org",
        },
    ).payload
    own_id = own["instance_id"]

    def _await_then_change_actor(
        awaited_manager,
        awaited_instance_id,
        *,
        timeout_seconds,
        poll_interval_seconds,
    ):
        instance = awaited_manager.get_instance(awaited_instance_id)
        instance.org_id = "#V#foreign_org"
        instance.namespace = "#V#shared_user@foreign_org"
        return SimpleNamespace(instance=instance, poll_count=1, timed_out=False)

    monkeypatch.setattr(
        "src.backend.workflows.durable.execution_observability.await_workflow_terminal_state",
        _await_then_change_actor,
    )

    with access_control.override_current_actor(
        user_concept_id="#V#shared_user",
        organisation_concept_id="#V#own_org",
    ):
        detail = gateway.invoke(
            "workflow_get_instance",
            {"instance_id": own_id, "await_terminal": True},
        ).payload

    assert detail.get("success") is False
    assert detail.get("error_code") == "not_found"


def test_workflow_list_instances_supports_turn_and_date_filters(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created_a = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user/#V#org",
            "inputs": {
                "conversation_session_id": "session-A",
                "turn_id": "req-A",
            },
        },
    ).payload
    created_b = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user/#V#org",
            "inputs": {
                "conversation_session_id": "session-B",
                "turn_id": "req-B",
            },
        },
    ).payload
    assert created_a.get("success") is True
    assert created_b.get("success") is True
    instance_a = manager.instances[str(created_a.get("instance_id"))]
    instance_b = manager.instances[str(created_b.get("instance_id"))]
    instance_a.created_at = datetime(2026, 2, 18, 12, 0, tzinfo=timezone.utc)
    instance_b.created_at = datetime(2026, 2, 19, 12, 0, tzinfo=timezone.utc)

    listed = gateway.invoke(
        "workflow_list_instances",
        {
            "namespace": "#V#user/#V#org",
            "session_id": "session-B",
            "request_id": "req-B",
            "from_utc": "2026-02-19T00:00:00Z",
            "to_utc": "2026-02-19T23:59:59Z",
            "limit": 10,
        },
    ).payload

    assert listed.get("success") is True
    assert listed.get("count") == 1
    assert listed["instances"][0]["instance_id"] == created_b.get("instance_id")


def test_workflow_create_instance_normalises_inputs(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": workflow_id,
            "user_id": "   ",
            "org_id": "  ",
            "namespace": " #V#anonymous@default ",
            "inputs": None,
            "max_retries": 999,
        },
    ).payload
    assert created.get("success") is True
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    instance = manager.instances[instance_id]
    # An explicit canonical namespace is the actor claim on a trusted unscoped
    # operator path; its actor components remain canonical and consistent.
    assert instance.user_id == "#V#anonymous"
    assert instance.org_id == "#V#default"
    assert instance.namespace == "#V#anonymous@default"
    assert instance.inputs == {}
    assert instance.max_retries == 50


def test_workflow_launch_tools_reject_forged_actor_against_ambient_authority():
    from src.backend.security import access_control

    gateway = _build_gateway()
    submission_path = (
        "src.backend.workflows.durable.workflow_instance_submission_service."
        "submit_verified_workflow_instance"
    )

    with patch(submission_path) as submit:
        with access_control.override_current_actor(
            user_concept_id="#V#synthetic_outsider",
            organisation_concept_id="#V#synthetic_outsider_org",
        ):
            expected_error_codes = {
                "workflow_create_instance": "workflow_actor_scope_mismatch",
                "workflow_execute": "workflow_actor_scope_mismatch",
                "experiment_execute_target_workflow": (
                    "workflow_global_admin_authority_required"
                ),
            }
            for method_name, expected_error_code in expected_error_codes.items():
                result = gateway.invoke(
                    method_name,
                    {
                        "workflow_id": "#V#synthetic_restricted_workflow",
                        "user_id": "#V#synthetic_owner",
                        "org_id": "#V#synthetic_trusted_org",
                        "namespace": "#V#synthetic_owner@synthetic_trusted_org",
                    },
                ).payload

                assert result["success"] is False
                assert result["error_code"] == expected_error_code
                if expected_error_code == "workflow_actor_scope_mismatch":
                    assert set(result["error_details"]["mismatch_fields"]) == {
                        "user_id",
                        "org_id",
                        "namespace",
                    }

    submit.assert_not_called()


def test_workflow_launch_tools_reject_untrusted_payload_only_actor():
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    submission_path = (
        "src.backend.workflows.durable.workflow_instance_submission_service."
        "submit_verified_workflow_instance"
    )

    with patch(submission_path) as submit:
        expected_error_codes = {
            "workflow_create_instance": "workflow_actor_authority_required",
            "workflow_execute": "workflow_actor_authority_required",
            "experiment_execute_target_workflow": (
                "workflow_global_admin_authority_required"
            ),
        }
        for method_name, expected_error_code in expected_error_codes.items():
            result = gateway.invoke(
                method_name,
                {
                    "workflow_id": "#V#synthetic_restricted_workflow",
                    "user_id": "#V#synthetic_owner",
                    "org_id": "#V#synthetic_trusted_org",
                    "namespace": "#V#synthetic_owner@synthetic_trusted_org",
                },
            ).payload

            assert result["success"] is False
            assert result["error_code"] == expected_error_code

    submit.assert_not_called()


def test_honest_outsider_cannot_execute_restricted_workflow_or_persist_instance(
    monkeypatch,
):
    from src.backend.security import access_control

    class _NoPersistenceManager:
        def create_instance(self, *_args, **_kwargs):
            raise AssertionError("denied outsider launch must not persist an instance")

        def create_instance_for_event(self, *_args, **_kwargs):
            raise AssertionError("denied outsider launch must not persist an instance")

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: _NoPersistenceManager(),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.registry_factory.resolve_workflow_definition_from_authority",
        lambda *_args, **_kwargs: SimpleNamespace(
            registry=None,
            definition=None,
            registration_source="unknown",
            known_workflow_ids=(),
            error_code="workflow_concept_not_accessible",
        ),
    )
    gateway = _build_gateway()

    with access_control.override_current_actor(
        user_concept_id="#V#honest_outsider",
        organisation_concept_id="#V#outsider_org",
    ):
        result = gateway.invoke(
            "workflow_execute",
            {
                "workflow_id": "#V#restricted_workflow_for_outsider_test",
                "user_id": "#V#honest_outsider",
                "org_id": "#V#outsider_org",
                "namespace": "#V#honest_outsider@outsider_org",
            },
        ).payload

    assert result["success"] is False
    assert result["error_code"] == "workflow_not_runnable"
    assert result.get("instance_id") is None


def test_workflow_schedule_create_requires_preexisting_actor_authority(monkeypatch):
    from src.backend.security import access_control

    class _UnexpectedManager:
        def __init__(self):
            raise AssertionError("unauthorised schedule must not reach persistence")

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        _UnexpectedManager,
    )
    arguments = {
        "workflow_id": "#V#synthetic_restricted_workflow",
        "schedule_type": "interval",
        "interval_seconds": 60,
        "user_id": "#V#synthetic_owner",
        "org_id": "#V#synthetic_trusted_org",
        "namespace": "#V#synthetic_owner@synthetic_trusted_org",
    }

    untrusted_gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    untrusted_result = untrusted_gateway.invoke(
        "workflow_create_schedule",
        arguments,
    ).payload

    assert untrusted_result["success"] is False
    assert untrusted_result["error_code"] == "workflow_actor_authority_required"

    with access_control.override_current_actor(
        user_concept_id="#V#synthetic_outsider",
        organisation_concept_id="#V#synthetic_outsider_org",
    ):
        forged_result = _build_gateway().invoke(
            "workflow_create_schedule",
            arguments,
        ).payload

    assert forged_result["success"] is False
    assert forged_result["error_code"] == "workflow_actor_scope_mismatch"
    assert set(forged_result["error_details"]["mismatch_fields"]) == {
        "user_id",
        "org_id",
        "namespace",
    }


def test_workflow_launch_rejects_org_claim_when_ambient_actor_has_no_org():
    from src.backend.security import access_control

    gateway = _build_gateway()
    submission_path = (
        "src.backend.workflows.durable.workflow_instance_submission_service."
        "submit_verified_workflow_instance"
    )

    with patch(submission_path) as submit:
        with access_control.override_current_actor(
            user_concept_id="#V#synthetic_outsider",
            organisation_concept_id=None,
        ):
            result = gateway.invoke(
                "workflow_execute",
                {
                    "workflow_id": "#V#synthetic_restricted_workflow",
                    "user_id": "#V#synthetic_outsider",
                    "org_id": "#V#synthetic_trusted_org",
                    "namespace": "#V#synthetic_outsider@synthetic_trusted_org",
                },
            ).payload

    assert result["success"] is False
    assert result["error_code"] == "workflow_actor_scope_mismatch"
    assert set(result["error_details"]["mismatch_fields"]) == {
        "org_id",
        "namespace",
    }
    submit.assert_not_called()


def test_experiment_execute_target_workflow_uses_verified_submission_path(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    payload = gateway.invoke(
        "experiment_execute_target_workflow",
        {
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
            "workflow_inputs": {"fixture_id": "fixture-1"},
            "max_retries": 2,
        },
    ).payload

    assert payload.get("success") is True
    assert payload.get("workflow_id") == "#V#meeting_invitation_testing_workflow"
    instance_id = payload.get("instance_id")
    assert isinstance(instance_id, str)
    assert payload.get("workflow_execution") == {
        "workflow_id": "#V#meeting_invitation_testing_workflow",
        "instance_id": instance_id,
        "launch_mode": "durable_instance",
        "workflow_inputs": {"fixture_id": "fixture-1"},
    }

    instance = manager.instances[instance_id]
    assert instance.user_id == "#V#user"
    assert instance.org_id == "#V#org"
    assert instance.namespace == "#V#user@org"
    assert instance.inputs == {"fixture_id": "fixture-1"}
    assert instance.max_retries == 2


def test_workflow_execute_can_await_terminal_and_inline_trace(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )

    trace_doc = {
        "execution_id": "trace-1550",
        "workflow_id": "#V#meeting_invitation_testing_workflow",
        "instance_id": "#V#wf_instance_1",
        "status": "completed",
        "start_time": datetime(2026, 3, 21, tzinfo=timezone.utc),
        "end_time": datetime(2026, 3, 21, 0, 0, 2, tzinfo=timezone.utc),
        "steps": [
            {
                "step_id": "dispatch",
                "status": "success",
                "timestamp": datetime(2026, 3, 21, 0, 0, 1, tzinfo=timezone.utc),
            }
        ],
    }
    monkeypatch.setattr(
        "src.backend.workflows.durable.execution_observability.get_workflow_execution_trace",
        lambda execution_id: trace_doc if execution_id == "trace-1550" else None,
    )

    original_get_instance = manager.get_instance

    def _get_instance(instance_id: str):
        instance = original_get_instance(instance_id)
        if instance is not None and instance.status == WorkflowInstanceStatus.PENDING:
            instance.status = WorkflowInstanceStatus.COMPLETED
            instance.current_state = "done"
            instance.outputs = {"result": "ok"}
            instance.execution_trace_id = "trace-1550"
            instance.workflow_data = {
                "workflow_result_envelope": {
                    "workflow_id": "#V#meeting_invitation_testing_workflow",
                    "completed": True,
                    "final_state": "done",
                },
                "workflow_step_result_envelopes": [
                    {
                        "workflow_id": "#V#meeting_invitation_testing_workflow",
                        "state_id": "done",
                        "action_id": "dispatch",
                    }
                ],
                "last_workflow_step_result_envelope": {
                    "workflow_id": "#V#meeting_invitation_testing_workflow",
                    "state_id": "done",
                    "action_id": "dispatch",
                },
                "workflow_metadata_validation_events": [
                    {
                        "state_id": "done",
                        "phase": "post_action",
                        "ok": True,
                        "applied": True,
                    }
                ],
                "last_metadata_validation": {
                    "state_id": "done",
                    "phase": "post_action",
                    "ok": True,
                    "applied": True,
                },
            }
        return instance

    manager.get_instance = _get_instance
    gateway = _build_gateway()

    payload = gateway.invoke(
        "workflow_execute",
        {
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
            "inputs": {"fixture_id": "fixture-1"},
            "await_terminal": "yes",
            "timeout_seconds": 500,
            "poll_interval_seconds": 0,
            "include_step_result_envelopes": "yes",
            "include_trace": 1,
        },
    ).payload

    execution = payload.get("workflow_execution") or {}
    assert payload.get("success") is True
    assert execution.get("instance_id") == "#V#wf_instance_1"
    assert execution.get("await_terminal") is True
    assert execution.get("timeout_seconds") == 90.0
    assert execution.get("final_status") == "completed"
    assert execution.get("poll_count") == 1
    assert execution.get("timed_out") is False
    assert execution.get("workflow_result_envelope", {}).get("final_state") == "done"
    assert execution.get("step_result_envelope_count") == 1
    assert execution.get("step_result_envelopes", [])[0]["action_id"] == "dispatch"
    metadata = execution.get("metadata_validation") or {}
    assert metadata.get("summary", {}).get("event_count") == 1
    assert execution.get("execution_trace_id") == "trace-1550"
    execution_trace = payload.get("execution_trace", {})
    assert execution_trace.get("execution_id") == "trace-1550"
    assert execution_trace.get("start_time") == "2026-03-21T00:00:00+00:00"
    assert execution_trace.get("end_time") == "2026-03-21T00:00:02+00:00"
    assert execution_trace.get("steps", [])[0].get("timestamp") == (
        "2026-03-21T00:00:01+00:00"
    )
    json.dumps(payload, allow_nan=False)
    boolean_fields = (
        execution.get("input_normalisation", {}).get("boolean_fields", {})
    )
    assert boolean_fields["await_terminal"] == {
        "supplied": True,
        "normalised": True,
        "default": False,
        "raw_type": "str",
        "recognised": True,
        "coerced": True,
        "used_default": False,
    }
    assert boolean_fields["include_step_result_envelopes"]["normalised"] is True
    assert boolean_fields["include_step_result_envelopes"]["raw_type"] == "str"
    assert boolean_fields["include_trace"]["normalised"] is True
    assert boolean_fields["include_trace"]["raw_type"] == "int"


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("poll_interval_seconds", float("-inf")),
    ],
)
def test_workflow_execute_rejects_nonfinite_wait_before_submission(
    monkeypatch,
    field_name: str,
    invalid_value: float,
):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    payload = gateway.invoke(
        "workflow_execute",
        {
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
            "await_terminal": True,
            field_name: invalid_value,
        },
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_parameter"
    assert payload.get("error_details") == {"invalid": [field_name]}
    assert payload.get("status") == "not_started"
    assert payload.get("effect_status") == "not_started"
    assert payload.get("mutation_outcome") == "not_started"
    assert payload.get("changed") is False
    assert manager._counter == 0


def test_workflow_execute_outer_timeout_preserves_durable_instance_receipt(
    monkeypatch,
):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )

    def _slow_await(*_args, **_kwargs):
        time.sleep(0.1)
        return SimpleNamespace(
            instance=None,
            poll_count=1,
            timed_out=True,
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.execution_observability."
        "await_workflow_terminal_state",
        _slow_await,
    )
    gateway = _build_gateway()

    result = gateway.invoke(
        "workflow_execute",
        {
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
            "inputs": {"fixture_id": "fixture-timeout"},
            "await_terminal": True,
            "timeout_seconds": 5,
            "poll_interval_seconds": 0,
        },
        deadline_monotonic=time.monotonic() + 0.03,
    )

    assert result.outcome == "timed_out"
    assert result.payload["error_code"] == (
        "tool_timeout_after_durable_submission"
    )
    assert result.payload["effect_status"] == "partial"
    assert result.payload["mutation_outcome"] == "partial"
    assert result.payload["changed"] is True
    assert result.payload["workflow_id"] == (
        "#V#meeting_invitation_testing_workflow"
    )
    assert result.payload["instance_id"] in manager.instances
    assert result.payload["durable_effect_receipt"]["instance_id"] == (
        result.payload["instance_id"]
    )


def test_workflow_execute_forwards_exact_required_worker_build(monkeypatch):
    from src.backend.workflows.durable.workflow_instance_submission_service import (
        WorkflowInstanceSubmissionResult,
    )

    manager = _StubWorkflowManager()
    captured: dict[str, Any] = {}

    def _submit(**kwargs: Any) -> WorkflowInstanceSubmissionResult:
        captured.update(kwargs)
        return WorkflowInstanceSubmissionResult(
            success=True,
            workflow_id=str(kwargs["workflow_id"]),
            status="pending",
            instance_id=None,
            verification={
                "preflight_passed": True,
                "postflight_passed": True,
                "runnable_verification_success": True,
            },
        )

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.workflow_instance_submission_service."
        "submit_verified_workflow_instance",
        _submit,
    )
    exact_commit = "abcdef1234567890abcdef1234567890abcdef12"

    payload = _build_gateway().invoke(
        "workflow_execute",
        {
            "workflow_id": "#V#meeting_invitation_testing_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
            "required_worker_build": exact_commit,
        },
    ).payload

    assert payload.get("success") is True
    assert captured["required_worker_build"] == exact_commit


def test_workflow_execute_reports_queued_timeout_as_not_started(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.startup.get_system_status",
        lambda: {
            "worker_running": False,
            "scheduler_running": False,
            "instances": {"pending": 1, "running": 0},
        },
    )
    gateway = _build_gateway()

    payload = gateway.invoke(
        "workflow_execute",
        {
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
            "inputs": {"arxiv_id": "2406.15341"},
            "await_terminal": True,
            "timeout_seconds": 0,
            "poll_interval_seconds": 0,
        },
    ).payload

    execution = payload.get("workflow_execution") or {}
    assert payload.get("success") is False
    assert payload.get("error_code") == "workflow_worker_unavailable"
    assert execution.get("execution_state") == "not_started"
    assert execution.get("failure_family") == "workflow_instance_never_started"
    assert execution.get("current_status") == "pending"
    assert execution.get("durable_system_status", {}).get("worker_running") is False


def test_workflow_get_execution_trace_resolves_instance_link(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
        },
    ).payload
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)
    manager.instances[instance_id].execution_trace_id = "trace-lookup-1"

    monkeypatch.setattr(
        "src.backend.workflows.get_workflow_execution_trace",
        lambda execution_id: (
            {
                "execution_id": execution_id,
                "instance_id": instance_id,
                "workflow_id": "#V#enrichment_workflow",
                "user_namespace": "#V#user/#V#org",
                "org_id": "#V#org",
                "status": "completed",
            }
            if execution_id == "trace-lookup-1"
            else None
        ),
    )

    payload = gateway.invoke(
        "workflow_get_execution_trace",
        {"instance_id": instance_id},
    ).payload

    assert payload.get("success") is True
    assert payload.get("execution_id") == "trace-lookup-1"
    assert payload.get("execution_trace", {}).get("status") == "completed"


def test_workflow_get_execution_trace_reports_unlinked_instance_state(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#arxiv_paper_representation_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user@org",
        },
    ).payload
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    payload = gateway.invoke(
        "workflow_get_execution_trace",
        {"instance_id": instance_id},
    ).payload

    assert payload.get("success") is False
    assert payload.get("error_code") == "workflow_execution_trace_not_linked"
    details = payload.get("error_details") or {}
    assert details.get("instance_id") == instance_id
    assert details.get("workflow_id") == "#V#arxiv_paper_representation_workflow"
    assert details.get("status") == "pending"
    assert details.get("trace_missing_reason") == "instance_has_no_execution_trace_id"


def test_workflow_get_execution_trace_hides_other_actor_trace(monkeypatch):
    from src.backend.security import access_control

    monkeypatch.setattr(
        "src.backend.workflows.get_workflow_execution_trace",
        lambda execution_id: {
            "execution_id": execution_id,
            "workflow_id": "#V#restricted_workflow",
            "user_namespace": "#V#other_user@other_org",
            "org_id": "#V#other_org",
            "status": "completed",
        },
    )
    gateway = _build_gateway()

    with access_control.override_current_actor(
        user_concept_id="#V#own_user",
        organisation_concept_id="#V#own_org",
    ):
        payload = gateway.invoke(
            "workflow_get_execution_trace",
            {"execution_id": "trace-foreign"},
        ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "not_found"


def test_workflow_get_execution_trace_rejects_cross_linked_foreign_trace(
    monkeypatch,
):
    from src.backend.security import access_control

    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()
    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#shared_user",
            "org_id": "#V#org_a",
            "namespace": "#V#shared_user/#V#org_a",
        },
    ).payload
    instance_id = created["instance_id"]
    manager.instances[instance_id].execution_trace_id = "trace-cross-linked"
    monkeypatch.setattr(
        "src.backend.workflows.get_workflow_execution_trace",
        lambda _execution_id: {
            "execution_id": "trace-cross-linked",
            "instance_id": "foreign-instance",
            "workflow_id": "#V#enrichment_workflow",
            "user_namespace": "#V#shared_user/#V#org_b",
            "org_id": "#V#org_b",
            "status": "completed",
        },
    )

    with access_control.override_current_actor(
        user_concept_id="#V#shared_user",
        organisation_concept_id="#V#org_a",
    ):
        payload = gateway.invoke(
            "workflow_get_execution_trace",
            {"instance_id": instance_id},
        ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "not_found"


def test_workflow_get_execution_trace_rejects_ambiguous_legacy_actor_record(
    monkeypatch,
):
    from src.backend.security import access_control

    monkeypatch.setattr(
        "src.backend.workflows.get_workflow_execution_trace",
        lambda execution_id: {
            "execution_id": execution_id,
            "workflow_id": "#V#restricted_workflow",
            "user_namespace": "#V#same_user",
            "status": "completed",
        },
    )
    gateway = _build_gateway()

    with access_control.override_current_actor(
        user_concept_id="#V#same_user",
        organisation_concept_id="#V#current_org",
    ):
        payload = gateway.invoke(
            "workflow_get_execution_trace",
            {"execution_id": "trace-legacy-user-only"},
        ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "not_found"


def test_turn_execution_get_critic_bundle_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.episode_critic_evidence_service.build_episode_critic_evidence_bundle",
        lambda **kwargs: {
            "success": True,
            "schema_version": "episode_critic_evidence_bundle.v1",
            "ready_for_critic": True,
            "episode_locator": {
                "request_id": kwargs.get("request_id"),
                "instance_id": kwargs.get("instance_id"),
            },
        },
    )

    payload = gateway.invoke(
        "turn_execution_get_critic_bundle",
        {"request_id": "req-bundle-1", "neighbour_turn_count": 1},
    ).payload

    assert payload["success"] is True
    assert payload["ready_for_critic"] is True
    assert payload["episode_locator"]["request_id"] == "req-bundle-1"


def test_turn_execution_get_diagnostics_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **kwargs: {
            "success": True,
            "schema_version": "turn_execution_diagnostics.v1",
            "request_id": kwargs.get("request_id"),
            "generated_at_utc": "2026-04-01T00:00:00Z",
            "progress_events": [],
            "activity_history": [],
            "phase_history": [],
            "tool_history": [],
            "stage_diagnostics": [],
            "workflow_stage_model": {
                "schema_version": "conversation_turn_stage_model.v1",
                "stages": [],
            },
            "workflow_stage_path": {
                "schema_version": "conversation_turn_stage_path.v1",
                "path": [],
            },
            "timing_breakdown": {
                "schema_version": "conversation_turn_timing_breakdown.v1",
                "stages": [],
                "llm_calls_by_stage_model": [],
                "totals": {
                    "elapsed_ms": None,
                    "llm_elapsed_ms": 0,
                    "llm_call_count": 0,
                },
            },
        },
    )

    payload = gateway.invoke(
        "turn_execution_get_diagnostics",
        {"request_id": "req-diag-1"},
    ).payload

    assert payload["success"] is True
    assert payload["request_id"] == "req-diag-1"
    assert payload["schema_version"] == "turn_execution_diagnostics.v1"


def test_turn_execution_get_diagnostics_returns_not_found_when_service_misses(
    monkeypatch,
):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.turn_execution_diagnostics_service.get_turn_execution_diagnostics_payload",
        lambda **kwargs: None,
    )

    payload = gateway.invoke(
        "turn_execution_get_diagnostics",
        {"request_id": "req-diag-missing"},
    ).payload

    assert payload["success"] is False
    assert payload["error_code"] == "not_found"
    assert payload["error_details"]["request_id"] == "req-diag-missing"


def test_episode_critique_memory_list_forwards_to_rag_collection(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._rag_list_indexed",
        lambda **kwargs: {
            "success": True,
            "collection": kwargs.get("collection"),
            "items": [],
        },
    )

    payload = gateway.invoke(
        "episode_critique_memory_list",
        {"namespace": "#V#user@org", "limit": 5},
    ).payload

    assert payload["success"] is True
    assert payload["collection"] == "episode_critique_memories"


def test_episode_critique_build_benchmark_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._resolve_rag_namespace_from_kwargs",
        lambda kwargs: {
            "namespace": kwargs.get("namespace"),
            "namespace_source": "argument",
        },
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._rag_namespace_resolution_error",
        lambda _report: None,
    )
    monkeypatch.setattr(
        "src.backend.services.episode_critique_benchmark_service.build_episode_critique_benchmark_report",
        lambda **kwargs: {
            "success": True,
            "collection": "episode_critique_memories",
            "namespace": kwargs.get("namespace"),
            "benchmark_fingerprint": "bench1609abcd1234",
            "sampled_meta_audit": {
                "mode": "on_demand_sample",
                "non_recursive": True,
                "case_count": 1,
                "cases": [
                    {
                        "case_id": "episode_critique_audit_case_01",
                        "memory_id": "#V#episode_critique_memory_1",
                    }
                ],
            },
        },
    )

    payload = gateway.invoke(
        "episode_critique_build_benchmark",
        {"namespace": "#V#user@org", "max_audit_cases": 1},
    ).payload

    assert payload["success"] is True
    assert payload["benchmark_fingerprint"] == "bench1609abcd1234"
    assert payload["sampled_meta_audit"]["non_recursive"] is True


def test_episode_critique_memory_get_forwards_to_rag_collection(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._rag_get_item",
        lambda **kwargs: {
            "success": True,
            "collection": kwargs.get("collection"),
            "session_id": kwargs.get("session_id"),
        },
    )

    payload = gateway.invoke(
        "episode_critique_memory_get",
        {"memory_id": "#V#episode_critique_memory_xyz", "namespace": "#V#user@org"},
    ).payload

    assert payload["success"] is True
    assert payload["collection"] == "episode_critique_memories"
    assert payload["session_id"] == "#V#episode_critique_memory_xyz"


def test_repo_dossier_file_snapshot_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.repo_dossier_service.repo_dossier_file_snapshot",
        lambda **kwargs: {
            "success": True,
            "path": kwargs.get("path"),
            "receipt": {"source_system": "local.git_repository"},
        },
    )

    payload = gateway.invoke(
        "repo_dossier_file_snapshot",
        {"path": "src/backend/example.py"},
    ).payload

    assert payload["success"] is True
    assert payload["path"] == "src/backend/example.py"


def test_repo_dossier_search_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.repo_dossier_service.repo_dossier_search",
        lambda **kwargs: {
            "success": True,
            "query": kwargs.get("query"),
            "match_count_returned": 0,
        },
    )

    payload = gateway.invoke(
        "repo_dossier_search", {"query": "WorkflowDefinition"}
    ).payload

    assert payload["success"] is True
    assert payload["query"] == "WorkflowDefinition"


def test_repo_dossier_git_metadata_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.repo_dossier_service.repo_dossier_git_metadata",
        lambda **kwargs: {
            "success": True,
            "branch": "main",
            "paths": kwargs.get("paths") or [],
        },
    )

    payload = gateway.invoke(
        "repo_dossier_git_metadata",
        {"paths": ["src/backend/example.py"]},
    ).payload

    assert payload["success"] is True
    assert payload["paths"] == ["src/backend/example.py"]


def test_context_bundle_resolve_effective_context_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.context_bundle_service.resolve_effective_context",
        lambda **kwargs: {
            "success": True,
            "subject_id": kwargs.get("subject_id"),
            "effective_context_bundle_ids": ["#V#bundle_parent_specificity"],
        },
    )

    payload = gateway.invoke(
        "context_bundle_resolve_effective_context",
        {"subject_kind": "concept", "subject_id": "#V#graph_theorist"},
    ).payload

    assert payload["success"] is True
    assert payload["subject_id"] == "#V#graph_theorist"
    assert payload["effective_context_bundle_ids"] == ["#V#bundle_parent_specificity"]


def test_context_bundle_assemble_context_dossier_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.context_bundle_service.assemble_context_dossier",
        lambda **kwargs: {
            "success": True,
            "dossier_id": "#V#context_dossier_graph_theorist",
            "subject_id": kwargs.get("subject_id"),
        },
    )

    payload = gateway.invoke(
        "context_bundle_assemble_context_dossier",
        {
            "name": "Concept dossier",
            "subject_kind": "concept",
            "subject_id": "#V#graph_theorist",
        },
    ).payload

    assert payload["success"] is True
    assert payload["dossier_id"] == "#V#context_dossier_graph_theorist"
    assert payload["subject_id"] == "#V#graph_theorist"


def test_context_bundle_update_report_revision_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.context_bundle_service.update_context_report_revision",
        lambda **kwargs: {
            "success": True,
            "dossier_id": kwargs.get("dossier_id"),
            "report_revision_id": "#V#workflow_report_revision_1",
        },
    )

    payload = gateway.invoke(
        "context_bundle_update_report_revision",
        {
            "dossier_id": "#V#context_dossier_graph_theorist",
            "report_text": "Revision text",
        },
    ).payload

    assert payload["success"] is True
    assert payload["dossier_id"] == "#V#context_dossier_graph_theorist"
    assert payload["report_revision_id"] == "#V#workflow_report_revision_1"


def test_context_bundle_build_reconstructed_workspace_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.context_bundle_service.build_reconstructed_workspace",
        lambda **kwargs: {
            "success": True,
            "workspace": {
                "subject_id": kwargs.get("subject_id"),
                "workspace_fingerprint": "fp-context-bundle",
            },
        },
    )

    payload = gateway.invoke(
        "context_bundle_build_reconstructed_workspace",
        {"subject_kind": "concept", "subject_id": "#V#graph_theorist"},
    ).payload

    assert payload["success"] is True
    assert payload["workspace"]["subject_id"] == "#V#graph_theorist"
    assert payload["workspace"]["workspace_fingerprint"] == "fp-context-bundle"


def test_context_bundle_build_benchmark_invokes_service(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.services.context_bundle_benchmark_service.build_context_bundle_benchmark_report",
        lambda **kwargs: {
            "success": True,
            "corpus": {"case_set": kwargs.get("case_set") or "phase2_seed"},
            "metrics": {"case_count": 4},
        },
    )

    payload = gateway.invoke(
        "context_bundle_build_benchmark",
        {"case_set": "phase2_seed"},
    ).payload

    assert payload["success"] is True
    assert payload["corpus"]["case_set"] == "phase2_seed"
    assert payload["metrics"]["case_count"] == 4


def test_workflow_list_execution_traces_returns_bounded_summaries(monkeypatch):
    gateway = _build_gateway()
    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    monkeypatch.setattr(
        "src.backend.workflows.list_recent_workflow_execution_traces",
        lambda **kwargs: [
            {
                "execution_id": "trace-1",
                "workflow_id": "#V#workflow_a",
                "instance_id": "wf-instance-1",
                "status": "completed",
                "start_time": "2026-03-21T00:00:00+00:00",
                "end_time": "2026-03-21T00:00:02+00:00",
                "state_transitions": [{"from": "a", "to": "b"}],
                "actions": [{"action_id": "tool.call"}],
                "steps": [{"step_id": "dispatch", "status": "success"}],
            }
        ],
    )

    payload = gateway.invoke(
        "workflow_list_execution_traces",
        {"namespace": "#V#user@org", "workflow_id": "#V#workflow_a", "limit": 5},
    ).payload

    assert payload.get("success") is True
    assert payload.get("count") == 1
    summary = payload.get("execution_traces", [])[0]
    assert summary["execution_id"] == "trace-1"
    assert summary["instance_id"] == "wf-instance-1"
    assert summary["action_count"] == 1
    assert summary["step_count"] == 1


def test_workflow_prediction_envelope_forces_preexisting_actor_namespace(
    monkeypatch,
):
    from src.backend.security import access_control

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.backend.services.workflow_prediction_service.build_workflow_prediction_envelope",
        lambda **kwargs: calls.append(dict(kwargs))
        or {
            "success": True,
            "schema_version": "workflow_prediction_envelope.v1",
            "workflow_id": kwargs["workflow_id"],
            "filters": {},
            "sample_window": {},
            "prediction_envelope": {},
        },
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    gateway = _build_gateway()

    with access_control.override_current_actor(
        user_concept_id="#V#prediction_user",
        organisation_concept_id="#V#org_a",
    ):
        forged = gateway.invoke(
            "workflow_build_prediction_envelope",
            {
                "workflow_id": "#V#prediction_workflow",
                "namespace": "#V#prediction_user/#V#org_b",
            },
        ).payload
        accepted = gateway.invoke(
            "workflow_build_prediction_envelope",
            {"workflow_id": "#V#prediction_workflow"},
        ).payload

    assert forged["success"] is False
    assert forged["error_code"] == "workflow_actor_scope_mismatch"
    assert accepted["success"] is True
    assert calls == [
        {
            "workflow_id": "#V#prediction_workflow",
            "namespace": "#V#prediction_user@org_a",
            "model": None,
            "provider": None,
            "limit": 50,
        }
    ]


def test_workflow_create_instance_preserves_event_idempotency_submission(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    payload = {
        "workflow_id": workflow_id,
        "user_id": "#V#user",
        "org_id": "#V#org",
        "namespace": "#V#user@org",
        "source_event_type": "turn_execution.completion_gate",
        "source_event_id": "req-1548",
        "event_idempotency_key": "evt:turn_execution.completion_gate:req-1548",
        "inputs": {"request_id": "req-1548"},
    }

    created = gateway.invoke("workflow_create_instance", payload).payload
    reused = gateway.invoke("workflow_create_instance", payload).payload

    assert created.get("success") is True
    assert reused.get("success") is True
    assert created.get("instance_id") == reused.get("instance_id")
    assert reused.get("status") == "reused"

    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)
    instance = manager.instances[instance_id]
    assert instance.source_event_type == "turn_execution.completion_gate"
    assert instance.source_event_id == "req-1548"
    assert (
        instance.event_idempotency_key == "evt:turn_execution.completion_gate:req-1548"
    )


def test_workflow_create_instance_rejects_unrunnable_workflow(monkeypatch):
    manager = _StubWorkflowManager()
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#definitely_missing_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
        },
    ).payload
    assert created.get("success") is False
    assert created.get("status") == "rejected_preflight"
    assert created.get("error_code") == "workflow_not_runnable"
    verification = created.get("verification") or {}
    preflight = verification.get("preflight") or {}
    assert "workflow_definition_not_registered" in (preflight.get("errors") or [])
    assert manager.instances == {}


def test_workflow_cancel_instance_gateway_paths(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": workflow_id,
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
        },
    ).payload
    assert created.get("success") is True
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    cancelled = gateway.invoke(
        "workflow_cancel_instance",
        {"instance_id": instance_id},
    ).payload
    assert cancelled.get("success") is True
    assert cancelled.get("instance_id") == instance_id
    assert cancelled.get("status") == "cancelled"

    detail = gateway.invoke(
        "workflow_get_instance",
        {"instance_id": instance_id},
    ).payload
    assert detail.get("success") is True
    assert detail.get("status") == "cancelled"

    repeated = gateway.invoke(
        "workflow_cancel_instance",
        {"instance_id": instance_id},
    ).payload
    assert repeated.get("success") is False
    assert repeated.get("error_code") == "already_terminal"


def test_workflow_resume_instance_preserves_identity_and_bounded_receipts(
    monkeypatch,
):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": "#V#enrichment_workflow",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
        },
    ).payload
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)
    assert manager.checkpoint(
        instance_id,
        state="checkpoint_after_first_step",
        step_index=2,
        workflow_data={"persisted": True},
    )
    raw_claim_secret = "raw-worker-claim-must-not-escape"
    instance = manager.instances[instance_id]
    instance.claim_token = raw_claim_secret
    instance.status = WorkflowInstanceStatus.PAUSED
    instance.manual_resume_required = True
    instance.checkpoint_pause_receipt = {
        "schema_version": "workflow_checkpoint_pause_receipt.v1",
        "instance_id": instance_id,
        "workflow_id": instance.workflow_id,
        "status": "paused",
        "checkpoint_state": instance.current_state,
        "checkpoint_step_index": instance.step_index,
        "pause_mode": "cooperative_checkpoint",
        "manual_resume_required": True,
        "claim_token_sha256": "safe-digest",
        "claim_token": raw_claim_secret,
    }

    resumed = gateway.invoke(
        "workflow_resume_instance",
        {"instance_id": instance_id},
    ).payload

    assert resumed["success"] is True
    assert resumed["schema_version"] == "workflow_instance_control_result.v1"
    assert resumed["operation"] == "resume"
    assert resumed["instance_id"] == instance_id
    assert resumed["status"] == "pending"
    assert resumed["same_instance_resume"] is True
    assert resumed["checkpoint_resume_receipt"] == {
        "schema_version": "workflow_checkpoint_resume_receipt.v1",
        "instance_id": instance_id,
        "workflow_id": "#V#enrichment_workflow",
        "status": "pending",
        "checkpoint_state": "checkpoint_after_first_step",
        "checkpoint_step_index": 2,
        "resume_count": 1,
        "same_instance_resume": True,
    }
    assert resumed["instance_status"]["current_state"] == (
        "checkpoint_after_first_step"
    )
    assert resumed["instance_status"]["step_index"] == 2
    assert resumed["instance_status"]["manual_resume_required"] is False
    assert raw_claim_secret not in json.dumps(resumed, sort_keys=True)

    repeated = gateway.invoke(
        "workflow_resume_instance",
        {"instance_id": instance_id},
    ).payload
    assert repeated["success"] is False
    assert repeated["error_code"] == "not_paused"


def test_workflow_resume_instance_hides_other_actor_records(monkeypatch):
    from src.backend.security import access_control

    manager = _StubWorkflowManager()
    paused_id = manager.create_instance(
        "#V#enrichment_workflow",
        user_id="#V#shared_user",
        org_id="#V#foreign_org",
        namespace="#V#shared_user@foreign_org",
    )
    manager.instances[paused_id].status = WorkflowInstanceStatus.PAUSED
    manager.instances[paused_id].manual_resume_required = True
    manager.instances[paused_id].checkpoint_pause_receipt = {
        "schema_version": "workflow_checkpoint_pause_receipt.v1"
    }
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    with access_control.override_current_actor(
        user_concept_id="#V#shared_user",
        organisation_concept_id="#V#own_org",
    ):
        resume_denied = gateway.invoke(
            "workflow_resume_instance", {"instance_id": paused_id}
        ).payload

    assert resume_denied["success"] is False
    assert resume_denied["error_code"] == "not_found"
    assert manager.instances[paused_id].status == WorkflowInstanceStatus.PAUSED


def test_workflow_retry_instance_restores_pending_and_keeps_checkpoint_state(
    monkeypatch,
):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": workflow_id,
            "user_id": "#V#user",
            "org_id": "#V#org",
            "inputs": {"seed": "value"},
            "max_retries": 2,
        },
    ).payload
    assert created.get("success") is True
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    assert manager.checkpoint(
        instance_id,
        state="tool_calling",
        step_index=3,
        workflow_data={"checkpoint_token": "cpt-1"},
    )
    assert manager.mark_failed(
        instance_id,
        error="tool_timeout",
        error_step="tool_calling",
    )

    retried = gateway.invoke(
        "workflow_retry_instance",
        {"instance_id": instance_id},
    ).payload
    assert retried.get("success") is True
    assert retried.get("instance_id") == instance_id
    assert retried.get("status") == "pending"

    detail = gateway.invoke(
        "workflow_get_instance",
        {"instance_id": instance_id},
    ).payload
    assert detail.get("success") is True
    assert detail.get("status") == "pending"
    assert detail.get("error") is None
    assert detail.get("error_step") is None
    # Retry should preserve workflow progress metadata for resume semantics.
    assert detail.get("current_state") == "tool_calling"
    assert detail.get("step_index") == 3
    assert detail.get("workflow_data") == {"checkpoint_token": "cpt-1"}


def test_workflow_retry_instance_rejects_non_failed_and_retry_limit(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    workflow_id = "#V#enrichment_workflow"
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_create_instance",
        {
            "workflow_id": workflow_id,
            "user_id": "#V#user",
            "org_id": "#V#org",
            "max_retries": 1,
        },
    ).payload
    assert created.get("success") is True
    instance_id = created.get("instance_id")
    assert isinstance(instance_id, str)

    not_failed = gateway.invoke(
        "workflow_retry_instance",
        {"instance_id": instance_id},
    ).payload
    assert not_failed.get("success") is False
    assert not_failed.get("error_code") == "not_failed"

    assert manager.mark_failed(
        instance_id,
        error="retry_budget_exhausted",
        increment_retry=True,
    )
    blocked = gateway.invoke(
        "workflow_retry_instance",
        {"instance_id": instance_id},
    ).payload
    assert blocked.get("success") is False
    assert blocked.get("error_code") == "retry_limit_exceeded"


def test_workflow_mcp_health_check_exists_and_runs():
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_mcp_health_check" in methods

    handler = catalogue.get("workflow_mcp_health_check").handler
    result = handler(include_introspection=False)
    assert result.get("success") is True
    checks = result.get("checks")
    assert isinstance(checks, list)
    assert len(checks) >= 4
    assert all(bool(check.get("ok")) for check in checks)


def test_workflow_mcp_health_check_gateway_invoke_success_path():
    gateway = _build_gateway()
    result = gateway.invoke(
        "workflow_mcp_health_check", {"include_introspection": False}
    )
    payload = result.payload
    assert payload.get("success") is True
    assert "checks" in payload
    assert "capability_matrix" in payload


def test_workflow_mcp_health_check_includes_helper_inventory_when_requested(
    monkeypatch,
):
    import src.backend.mcp_server.process_guard as process_guard

    monkeypatch.setattr(
        process_guard,
        "get_mcp_helper_inventory",
        lambda: {
            "success": True,
            "helper_count": 2,
            "live_helper_count": 1,
            "duplicate_group_count": 1,
            "duplicate_groups": [
                {
                    "helper_kind": "mcp_stdio_server",
                    "owner_token": "owner-a",
                    "pids": [101, 102],
                    "count": 2,
                }
            ],
            "helpers": [],
        },
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "workflow_mcp_health_check",
        {"include_introspection": True},
    ).payload

    assert payload.get("success") is True
    helper_inventory = payload.get("mcp_helper_inventory")
    assert isinstance(helper_inventory, dict)
    assert helper_inventory.get("helper_count") == 2
    assert helper_inventory.get("duplicate_group_count") == 1


def test_workflow_materialisation_diagnostics_exists_and_runs(monkeypatch):
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "build_workflow_materialisation_diagnostics",
        lambda **kwargs: {
            "success": True,
            "classification": {"state": "healthy"},
            "required_concept_ids": kwargs.get("required_concept_ids") or [],
        },
    )

    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_materialisation_diagnostics" in methods

    handler = catalogue.get("workflow_materialisation_diagnostics").handler
    result = handler(required_concept_ids=["#V#ephemeral_theory"])
    assert result.get("success") is True
    assert result.get("classification", {}).get("state") == "healthy"
    assert result.get("required_concept_ids") == ["#V#ephemeral_theory"]


def test_workflow_materialisation_diagnostics_gateway_invoke_success_path(monkeypatch):
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "build_workflow_materialisation_diagnostics",
        lambda **kwargs: {
            "success": True,
            "classification": {"state": "partial_bootstrap"},
            "required_concept_ids": kwargs.get("required_concept_ids") or [],
        },
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "workflow_materialisation_diagnostics",
        {"required_concept_ids": ["#V#ephemeral_theory"]},
    ).payload

    assert payload.get("success") is True
    assert payload.get("classification", {}).get("state") == "partial_bootstrap"
    assert payload.get("required_concept_ids") == ["#V#ephemeral_theory"]


def test_workflow_concept_parity_audit_exists_and_runs(monkeypatch):
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "build_workflow_concept_parity_audit",
        lambda **kwargs: {
            "success": True,
            "summary": {"audited_count": 2},
            "concepts": [
                {"concept_id": concept_id, "diagnostic_state": "present"}
                for concept_id in (kwargs.get("concept_ids") or [])
            ],
        },
    )

    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_concept_parity_audit" in methods

    handler = catalogue.get("workflow_concept_parity_audit").handler
    result = handler(concept_ids=["#V#alpha_workflow", "#V#beta_workflow"])
    assert result.get("success") is True
    assert result.get("summary", {}).get("audited_count") == 2
    assert [item["concept_id"] for item in result.get("concepts", [])] == [
        "#V#alpha_workflow",
        "#V#beta_workflow",
    ]


def test_workflow_concept_parity_audit_gateway_invoke_success_path(monkeypatch):
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "build_workflow_concept_parity_audit",
        lambda **kwargs: {
            "success": True,
            "summary": {"audited_count": 1},
            "concepts": [
                {
                    "concept_id": (kwargs.get("concept_ids") or ["#V#alpha_workflow"])[
                        0
                    ],
                    "diagnostic_state": "authority_drift",
                }
            ],
        },
    )

    gateway = _build_gateway()
    payload = gateway.invoke(
        "workflow_concept_parity_audit",
        {"concept_ids": ["#V#alpha_workflow"]},
    ).payload

    assert payload.get("success") is True
    assert payload.get("summary", {}).get("audited_count") == 1
    assert payload.get("concepts", [])[0]["diagnostic_state"] == "authority_drift"


def test_authenticated_actor_cannot_read_host_inventory_workflow_health():
    from src.backend.security import access_control

    gateway = _build_gateway()
    with access_control.override_current_actor(
        user_concept_id="#V#ordinary_user",
        organisation_concept_id="#V#ordinary_org",
    ):
        result = gateway.invoke(
            "workflow_mcp_health_check",
            {"include_introspection": False},
        ).payload

    assert result["success"] is False
    assert result["error_code"] == "workflow_global_admin_authority_required"


def test_authenticated_actor_cannot_read_deployment_global_workflow_diagnostics(
    monkeypatch,
):
    from src.backend.security import access_control
    import src.backend.services.workflow_materialisation_diagnostics_service as service

    monkeypatch.setattr(
        service,
        "build_workflow_materialisation_diagnostics",
        lambda **_kwargs: {
            "success": True,
            "classification": {"state": "healthy"},
        },
    )
    monkeypatch.setattr(
        service,
        "build_workflow_concept_parity_audit",
        lambda **_kwargs: {
            "success": True,
            "summary": {"audited_count": 0},
            "concepts": [],
        },
    )

    gateway = _build_gateway()
    with access_control.override_current_actor(
        user_concept_id="#V#ordinary_user",
        organisation_concept_id="#V#ordinary_org",
    ):
        materialisation = gateway.invoke(
            "workflow_materialisation_diagnostics",
            {},
        ).payload
        parity = gateway.invoke("workflow_concept_parity_audit", {}).payload

    assert materialisation["success"] is False
    assert parity["success"] is False
    assert (
        materialisation["error_code"]
        == "workflow_global_admin_authority_required"
    )
    assert parity["error_code"] == "workflow_global_admin_authority_required"


def test_workflow_surface_capability_tools_exist_in_internal_catalogue():
    methods = set(build_default_catalogue().list_methods())
    tracked = set(tracked_workflow_surface_tool_names())
    missing = sorted(tracked - methods)
    assert (
        not missing
    ), f"Tracked workflow surface tools missing from catalogue: {missing}"


def test_workflow_bind_event_and_list_event_bindings_gateway_paths(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_read_only_registry(monkeypatch, "#V#enrichment_workflow")
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.get_instance_manager",
        lambda: manager,
    )
    gateway = _build_gateway()

    bind_payload = gateway.invoke(
        "workflow_bind_event",
        {
            "event_type": "concept.created",
            "workflow_id": "#V#enrichment_workflow",
            "input_mapping": {"concept_id": "event.concept_id"},
            "condition": {"kind": "context_exists", "key": "event.concept_id"},
        },
    ).payload
    assert bind_payload.get("success") is True
    assert bind_payload.get("created") is True
    binding = bind_payload.get("binding") or {}
    assert binding.get("event_type") == "concept.created"
    assert binding.get("workflow_id") == "#V#enrichment_workflow"
    assert binding.get("condition") == {
        "kind": "context_exists",
        "key": "event.concept_id",
        "expected": True,
    }

    listed = gateway.invoke(
        "workflow_list_event_bindings",
        {"event_type": "concept.created"},
    ).payload
    assert listed.get("success") is True
    assert listed.get("count") == 1
    assert listed.get("bindings")[0]["workflow_id"] == "#V#enrichment_workflow"
    assert listed.get("bindings")[0]["condition"] == binding.get("condition")
    assert listed.get("diagnostics") == []
    assert listed.get("has_conflicts") is False


def test_workflow_bind_event_conflict_requires_replace(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_read_only_registry(monkeypatch, "#V#enrichment_workflow")
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    first = gateway.invoke(
        "workflow_bind_event",
        {
            "event_type": "concept.updated",
            "workflow_id": "#V#enrichment_workflow",
            "input_mapping": {"concept_id": "event.concept_id"},
        },
    ).payload
    assert first.get("success") is True
    assert first.get("created") is True

    conflict = gateway.invoke(
        "workflow_bind_event",
        {
            "event_type": "concept.updated",
            "workflow_id": "#V#enrichment_workflow",
            "input_mapping": {"different": "event.concept_id"},
        },
    ).payload
    assert conflict.get("success") is False
    assert conflict.get("error_code") == "binding_conflict"

    replaced = gateway.invoke(
        "workflow_bind_event",
        {
            "event_type": "concept.updated",
            "workflow_id": "#V#enrichment_workflow",
            "input_mapping": {"different": "event.concept_id"},
            "replace_existing": True,
        },
    ).payload
    assert replaced.get("success") is True
    assert replaced.get("updated") is True


def test_workflow_event_binding_enable_disable_and_delete_gateway_paths(monkeypatch):
    manager = _StubWorkflowManager()
    _patch_read_only_registry(monkeypatch, "#V#file_copy_upload_handler_workflow")
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.get_instance_manager",
        lambda: manager,
    )
    gateway = _build_gateway()

    created = gateway.invoke(
        "workflow_bind_event",
        {
            "event_type": "file_copy.uploaded",
            "workflow_id": "#V#file_copy_upload_handler_workflow",
            "input_mapping": {"concept_id": "event.file_copy_concept_id"},
        },
    ).payload
    assert created.get("success") is True
    binding = created.get("binding") or {}
    binding_id = binding.get("binding_id")
    assert isinstance(binding_id, str) and binding_id

    disabled = gateway.invoke(
        "workflow_set_event_binding_enabled",
        {
            "binding_id": binding_id,
            "enabled": False,
            "actor": "test.operator",
        },
    ).payload
    assert disabled.get("success") is True
    assert disabled.get("binding_id") == binding_id
    assert disabled.get("enabled") is False
    assert disabled.get("updated") is True

    listed = gateway.invoke(
        "workflow_list_event_bindings",
        {"event_type": "file_copy.uploaded"},
    ).payload
    assert listed.get("success") is True
    diagnostics = listed.get("diagnostics") or []
    assert any(
        item.get("reason_code") == "all_persistent_bindings_disabled"
        for item in diagnostics
    )
    assert listed.get("has_conflicts") is True

    deleted = gateway.invoke(
        "workflow_delete_event_binding",
        {"binding_id": binding_id},
    ).payload
    assert deleted.get("success") is True
    assert deleted.get("deleted") is True
    assert deleted.get("binding_id") == binding_id

    missing = gateway.invoke(
        "workflow_delete_event_binding",
        {"binding_id": binding_id},
    ).payload
    assert missing.get("success") is False
    assert missing.get("error_code") == "not_found"


def test_authenticated_actor_cannot_inspect_or_mutate_global_event_bindings(
    monkeypatch,
):
    from src.backend.security import access_control

    manager = _StubWorkflowManager()
    _patch_read_only_registry(monkeypatch, "#V#shared_workflow")
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_event_integration_service.get_instance_manager",
        lambda: manager,
    )
    gateway = _build_gateway()
    created = gateway.invoke(
        "workflow_bind_event",
        {
            "event_type": "shared.event",
            "workflow_id": "#V#shared_workflow",
        },
    ).payload
    binding_id = created["binding"]["binding_id"]

    with access_control.override_current_actor(
        user_concept_id="#V#ordinary_user",
        organisation_concept_id="#V#shared_org",
    ):
        listed = gateway.invoke("workflow_list_event_bindings", {}).payload
        mutations = [
            gateway.invoke(
                "workflow_bind_event",
                {
                    "event_type": "other.event",
                    "workflow_id": "#V#shared_workflow",
                },
            ).payload,
            gateway.invoke(
                "workflow_set_event_binding_enabled",
                {"binding_id": binding_id, "enabled": False},
            ).payload,
            gateway.invoke(
                "workflow_delete_event_binding",
                {"binding_id": binding_id},
            ).payload,
        ]

    assert listed["success"] is False
    assert listed["error_code"] == "workflow_global_admin_authority_required"
    assert all(result["success"] is False for result in mutations)
    assert all(
        result["error_code"] == "workflow_global_admin_authority_required"
        for result in mutations
    )
    assert manager.get_event_binding(binding_id) is not None
    assert manager.get_event_binding(binding_id).enabled is True


def test_workflow_schedule_gateway_tools_integrate_with_scheduler(monkeypatch):
    manager = _InMemoryScheduleWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )

    gateway = _build_gateway()
    run_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    create_result = gateway.invoke(
        "workflow_create_schedule",
        {
            "workflow_id": "#V#enrichment_workflow",
            "schedule_type": "once",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user/#V#org",
            "run_at": run_at,
            "default_inputs": {"task": "e2e"},
            "description": "WS8 gateway+scheduler integration test",
        },
    ).payload

    assert create_result.get("success") is True
    schedule_id = create_result.get("schedule_id")
    assert isinstance(schedule_id, str)
    assert manager.schedules[schedule_id].namespace == "#V#user@org"

    scheduler = WorkflowScheduler(manager, check_interval_seconds=0.01)  # type: ignore[arg-type]
    scheduler._process_due_schedules()
    metrics = scheduler.get_poll_metrics()
    assert metrics["due_count"] == 1
    assert metrics["triggered_count"] == 1
    assert metrics["due_schedules_seen_total"] == 1
    assert len(manager.instances) == 1
    assert manager.instances[0]["schedule_id"] == schedule_id
    assert manager.instances[0]["namespace"] == "#V#user@org"

    trigger_result = gateway.invoke(
        "workflow_trigger_schedule",
        {"schedule_id": schedule_id},
    ).payload
    assert trigger_result.get("success") is True
    assert trigger_result.get("schedule_id") == schedule_id
    assert trigger_result.get("status") == "triggered"
    assert len(manager.instances) == 2


def test_ordinary_actor_cannot_access_unowned_experiment_or_turn_control_records(
    monkeypatch,
):
    from src.backend.security import access_control
    from src.backend.integrations.internal_mcp import catalogue as catalogue_module

    catalogue = build_default_catalogue()
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(),
        enabled=True,
    )
    actor_safe_reads = {
        "experiment_run_list",
        "experiment_run_get",
        "chat_history_get_segments",
        "chat_history_get_debug_entry",
        "conversation_telemetry_get_locator",
        "turn_execution_list",
        "turn_execution_get",
        "turn_execution_search_failures",
    }
    monkeypatch.setattr(
        catalogue_module,
        "_rag_list_indexed",
        lambda **_kwargs: {
            "success": False,
            "error_code": "not_found",
        },
    )
    monkeypatch.setattr(
        catalogue_module,
        "_rag_get_item",
        lambda **_kwargs: {
            "success": False,
            "error_code": "not_found",
        },
    )
    monkeypatch.setattr(
        catalogue_module,
        "_resolve_chat_history_read_target",
        lambda _kwargs: {
            "success": False,
            "error_code": "not_found",
        },
    )
    calls = [
        ("testing_theory_create_slice", {"name": "guessed theory"}),
        (
            "testing_theory_import_canonical_context",
            {"theory_id": "guessed-theory"},
        ),
        (
            "testing_theory_assert_local_claims",
            {"theory_id": "guessed-theory", "claims": []},
        ),
        ("testing_theory_compute_diff", {"theory_id": "guessed-theory"}),
        (
            "testing_theory_rollback_local_writes",
            {"theory_id": "guessed-theory"},
        ),
        (
            "testing_theory_promote_validated_claims",
            {"theory_id": "guessed-theory"},
        ),
        ("testing_theory_gc_expired", {}),
        ("experiment_run_list", {}),
        ("experiment_run_get", {"run_id": "guessed-run"}),
        ("experiment_create_spec", {"name": "guessed experiment"}),
        ("experiment_start_run", {"experiment_spec_id": "guessed-spec"}),
        ("experiment_record_observation", {"run_id": "guessed-run"}),
        ("experiment_compute_verdict", {"run_id": "guessed-run"}),
        ("experiment_emit_learning_signal", {"run_id": "guessed-run"}),
        (
            "experiment_execute_target_workflow",
            {"workflow_id": "#V#guessed_workflow"},
        ),
        ("experiment_execute_regression_suite", {}),
        ("testing_prepare_experiment_spec", {}),
        ("testing_prepare_meeting_invitation_spec", {}),
        ("testing_prepare_arxiv_paper_ingestion_fixture", {}),
        ("testing_verify_arxiv_paper_ingestion_result", {}),
        (
            "testing_cleanup_arxiv_paper_ingestion_artifacts",
            {"paper_concept_id": "#V#guessed_paper"},
        ),
        ("chat_history_get_segments", {"session_id": "guessed-session"}),
        (
            "chat_history_get_debug_entry",
            {"session_id": "guessed-session", "history_index": 0},
        ),
        (
            "conversation_telemetry_get_locator",
            {"session_id": "guessed-session"},
        ),
        ("turn_execution_list", {}),
        ("turn_execution_get", {"request_id": "guessed-turn"}),
        ("turn_execution_get_diagnostics", {"request_id": "guessed-turn"}),
        ("turn_execution_get_live_progress", {"request_id": "guessed-turn"}),
        ("turn_execution_get_critic_bundle", {"request_id": "guessed-turn"}),
        ("turn_execution_search_failures", {}),
    ]

    with access_control.override_current_actor(
        user_concept_id="#V#ordinary_user",
        organisation_concept_id="#V#ordinary_org",
    ):
        results = {
            name: (
                gateway.invoke(name, arguments).payload
                if name in actor_safe_reads
                else catalogue.get(name).handler(**arguments)
            )
            for name, arguments in calls
        }

    still_global_reads = {
        "turn_execution_get_diagnostics",
        "turn_execution_get_live_progress",
        "turn_execution_get_critic_bundle",
    }
    mutation_names = set(results) - actor_safe_reads - still_global_reads

    assert all(
        results[name].get("error_code")
        != "workflow_global_admin_authority_required"
        for name in actor_safe_reads
    )
    assert all(results[name]["success"] is False for name in still_global_reads)
    assert all(
        results[name]["error_code"] == "workflow_global_admin_authority_required"
        for name in still_global_reads
    )
    assert all(results[name]["success"] is False for name in mutation_names)
    assert all(
        results[name]["error_code"] == "workflow_global_admin_authority_required"
        for name in mutation_names
    )


def test_trusted_operator_gateway_retains_testing_control_plane_access(monkeypatch):
    from src.backend.services import (
        arxiv_ingestion_testing_service,
        experiment_run_service,
        testing_theory_service,
    )

    calls: list[str] = []
    monkeypatch.setattr(
        testing_theory_service,
        "create_testing_theory_slice",
        lambda **_kwargs: calls.append("theory")
        or {"success": True, "theory_id": "#V#operator_theory"},
    )
    monkeypatch.setattr(
        experiment_run_service,
        "create_experiment_spec",
        lambda **_kwargs: calls.append("experiment")
        or {"success": True, "experiment_spec_id": "#V#operator_spec"},
    )
    monkeypatch.setattr(
        arxiv_ingestion_testing_service,
        "cleanup_arxiv_paper_ingestion_test_artifacts",
        lambda **_kwargs: calls.append("cleanup")
        or {"success": True, "cleanup_passed": True},
    )

    gateway = _build_gateway()
    results = [
        gateway.invoke(
            "testing_theory_create_slice",
            {"name": "Operator theory"},
        ).payload,
        gateway.invoke(
            "experiment_create_spec",
            {"name": "Operator experiment"},
        ).payload,
        gateway.invoke(
            "testing_cleanup_arxiv_paper_ingestion_artifacts",
            {"paper_concept_id": "#V#operator_fixture"},
        ).payload,
    ]

    assert all(result["success"] is True for result in results)
    assert calls == ["theory", "experiment", "cleanup"]


def test_revoked_workflow_visibility_hides_persisted_instances_and_schedules(
    monkeypatch,
):
    from src.backend.security import access_control

    instance_manager = _StubWorkflowManager()
    instance_id = instance_manager.create_instance(
        "#V#revoked_workflow",
        user_id="#V#actor",
        org_id="#V#org",
        namespace="#V#actor@org",
    )
    schedule_manager = _InMemoryScheduleWorkflowManager()
    schedule = WorkflowSchedule.create_interval(
        "#V#revoked_workflow",
        interval_seconds=60,
        user_id="#V#actor",
        org_id="#V#org",
        namespace="#V#actor@org",
    )
    schedule_manager.create_schedule(schedule)

    managers = iter([instance_manager, instance_manager, schedule_manager, schedule_manager])
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: next(managers),
    )
    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.filter_workflow_ids_for_current_actor",
        lambda _workflow_ids: [],
    )
    catalogue = build_default_catalogue()

    with access_control.override_current_actor(
        user_concept_id="#V#actor",
        organisation_concept_id="#V#org",
    ):
        instance_list = catalogue.get("workflow_list_instances").handler()
        instance_detail = catalogue.get("workflow_get_instance").handler(
            instance_id=instance_id
        )
        schedule_list = catalogue.get("workflow_list_schedules").handler()
        schedule_detail = catalogue.get("workflow_get_schedule").handler(
            schedule_id=schedule.schedule_id
        )

    assert instance_list == {"success": True, "instances": [], "count": 0}
    assert instance_detail["success"] is False
    assert instance_detail["error_code"] == "not_found"
    assert schedule_list == {"success": True, "schedules": [], "count": 0}
    assert schedule_detail["success"] is False
    assert schedule_detail["error_code"] == "not_found"


def test_workflow_schedule_tools_are_exactly_scoped_to_preexisting_actor(monkeypatch):
    from src.backend.security import access_control

    manager = _InMemoryScheduleWorkflowManager()
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()
    base_arguments = {
        "workflow_id": "#V#enrichment_workflow",
        "schedule_type": "interval",
        "interval_seconds": 60,
        "user_id": "#V#shared_user",
    }
    own_result = gateway.invoke(
        "workflow_create_schedule",
        {
            **base_arguments,
            "org_id": "#V#own_org",
            "namespace": "#V#shared_user@own_org",
        },
    ).payload
    foreign_result = gateway.invoke(
        "workflow_create_schedule",
        {
            **base_arguments,
            "org_id": "#V#foreign_org",
            "namespace": "#V#shared_user@foreign_org",
        },
    ).payload

    assert own_result["success"] is True
    assert foreign_result["success"] is True
    own_schedule_id = own_result["schedule_id"]
    foreign_schedule_id = foreign_result["schedule_id"]

    # Explicitly trusted, in-process operator calls retain their legacy
    # payload-authority fallback for administrative schedule access.
    trusted_detail = gateway.invoke(
        "workflow_get_schedule",
        {"schedule_id": foreign_schedule_id},
    ).payload
    assert trusted_detail["success"] is True
    assert trusted_detail["schedule_id"] == foreign_schedule_id

    submission_path = (
        "src.backend.workflows.durable.workflow_instance_submission_service."
        "submit_verified_workflow_instance"
    )
    with patch(submission_path) as submit:
        with access_control.override_current_actor(
            user_concept_id="#V#shared_user",
            organisation_concept_id="#V#own_org",
        ):
            listed = gateway.invoke("workflow_list_schedules", {}).payload
            denied_results = [
                gateway.invoke(
                    "workflow_get_schedule",
                    {"schedule_id": foreign_schedule_id},
                ).payload,
                gateway.invoke(
                    "workflow_set_schedule_enabled",
                    {"schedule_id": foreign_schedule_id, "enabled": False},
                ).payload,
                gateway.invoke(
                    "workflow_delete_schedule",
                    {"schedule_id": foreign_schedule_id},
                ).payload,
                gateway.invoke(
                    "workflow_trigger_schedule",
                    {"schedule_id": foreign_schedule_id},
                ).payload,
            ]

    assert listed["success"] is True
    assert [item["schedule_id"] for item in listed["schedules"]] == [
        own_schedule_id
    ]
    for result in denied_results:
        assert result["success"] is False
        assert result["error_code"] == "workflow_actor_scope_mismatch"
        assert set(result["error_details"]["mismatch_fields"]) == {
            "org_id",
            "namespace",
        }
    submit.assert_not_called()
    assert foreign_schedule_id in manager.schedules


def test_workflow_schedule_execute_checkpoint_fail_retry_resume(monkeypatch):
    manager = _InMemoryScheduleWorkflowManager()
    _patch_submit_verified_instance_success(monkeypatch)
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )
    gateway = _build_gateway()

    run_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    created_schedule = gateway.invoke(
        "workflow_create_schedule",
        {
            "workflow_id": "#V#enrichment_workflow",
            "schedule_type": "once",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user/#V#org",
            "run_at": run_at,
            "default_inputs": {"task": "resume-check"},
            "description": "JVNAUTOSCI-926 schedule->retry e2e",
        },
    ).payload
    assert created_schedule.get("success") is True
    schedule_id = created_schedule.get("schedule_id")
    assert isinstance(schedule_id, str)
    assert manager.schedules[schedule_id].namespace == "#V#user@org"

    scheduler = WorkflowScheduler(manager, check_interval_seconds=0.01)  # type: ignore[arg-type]
    scheduler._process_due_schedules()
    assert len(manager.instances) == 1
    instance_id = manager.instances[0]["instance_id"]
    assert isinstance(instance_id, str)

    assert manager.checkpoint(
        instance_id,
        state="awaiting_tool_result",
        step_index=2,
        workflow_data={"checkpoint_token": "checkpoint-1"},
    )
    assert manager.mark_failed(
        instance_id,
        error="transient_tool_failure",
        error_step="awaiting_tool_result",
    )

    retried = gateway.invoke(
        "workflow_retry_instance",
        {"instance_id": instance_id},
    ).payload
    assert retried.get("success") is True
    assert retried.get("status") == "pending"

    detail = gateway.invoke(
        "workflow_get_instance",
        {"instance_id": instance_id},
    ).payload
    assert detail.get("success") is True
    assert detail.get("status") == "pending"
    assert detail.get("schedule_id") == schedule_id
    assert detail.get("current_state") == "awaiting_tool_result"
    assert detail.get("step_index") == 2
    assert detail.get("workflow_data") == {"checkpoint_token": "checkpoint-1"}


def test_workflow_trigger_schedule_rejects_unrunnable_workflow(monkeypatch):
    manager = _InMemoryScheduleWorkflowManager()
    monkeypatch.setattr(
        "src.backend.workflows.workflow_listing_service.filter_workflow_ids_for_current_actor",
        lambda workflow_ids: list(workflow_ids),
    )
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )

    gateway = _build_gateway()
    run_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    create_result = gateway.invoke(
        "workflow_create_schedule",
        {
            "workflow_id": "#V#definitely_missing_workflow",
            "schedule_type": "once",
            "user_id": "#V#user",
            "org_id": "#V#org",
            "namespace": "#V#user/#V#org",
            "run_at": run_at,
            "default_inputs": {"task": "e2e"},
            "description": "WS8 gateway schedule rejection test",
        },
    ).payload

    assert create_result.get("success") is True
    schedule_id = create_result.get("schedule_id")
    assert isinstance(schedule_id, str)

    trigger_result = gateway.invoke(
        "workflow_trigger_schedule",
        {"schedule_id": schedule_id},
    ).payload
    assert trigger_result.get("success") is False
    assert trigger_result.get("schedule_id") == schedule_id
    assert trigger_result.get("status") != "triggered"
    assert trigger_result.get("error_code") == "workflow_not_runnable"
    verification = trigger_result.get("verification") or {}
    preflight = verification.get("preflight") or {}
    assert "workflow_definition_not_registered" in (preflight.get("errors") or [])
    assert len(manager.instances) == 0
