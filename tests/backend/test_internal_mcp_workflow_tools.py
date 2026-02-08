from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.integrations.internal_mcp.workflow_surface_capabilities import (
    tracked_workflow_surface_tool_names,
)
from src.backend.workflows.durable.scheduler import WorkflowScheduler


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def test_workflow_list_definitions_exists_and_returns_data():
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_list_definitions" in methods

    handler = catalogue.get("workflow_list_definitions").handler
    result = handler(limit=10)

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
    assert "#V#generate_considerations_workflow" in def_ids

    parity_inventory = result["parity_inventory"]
    assert isinstance(parity_inventory, dict)
    assert "counts" in parity_inventory
    assert "summary_text" in parity_inventory
    assert "diagnostics" in parity_inventory
    assert "parity_policy" in parity_inventory
    diagnostics = parity_inventory["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert "drift_detected" in diagnostics
    assert "severity" in diagnostics
    assert "reason_codes" in diagnostics

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
    gateway = _build_gateway()
    result = gateway.invoke("workflow_list_definitions", {"limit": 10})
    payload = result.payload
    assert payload.get("success") is True
    assert "definitions" in payload
    assert "parity_inventory" in payload
    assert "capability_matrix" in payload


def test_workflow_list_definitions_skips_bootstrap_writes():
    with patch(
        "src.backend.workflows.durable.registry_factory.bootstrap_workflow_concepts",
        side_effect=AssertionError("workflow_list_definitions should be read-only"),
    ):
        handler = build_default_catalogue().get("workflow_list_definitions").handler
        result = handler(limit=10)

    assert result.get("success") is True


def test_workflow_list_instances_gateway_invoke_error_path():
    gateway = _build_gateway()
    result = gateway.invoke("workflow_list_instances", {"status": "invalid-status"})
    payload = result.payload
    assert payload.get("success") is False
    assert payload.get("error_code") == "invalid_status"


def test_workflow_mcp_health_check_exists_and_runs():
    catalogue = build_default_catalogue()
    methods = catalogue.list_methods()
    assert "workflow_mcp_health_check" in methods

    handler = catalogue.get("workflow_mcp_health_check").handler
    result = handler(include_introspection=False)
    assert result.get("success") is True
    checks = result.get("checks")
    assert isinstance(checks, list)
    assert len(checks) >= 3
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


def test_workflow_surface_capability_tools_exist_in_internal_catalogue():
    methods = set(build_default_catalogue().list_methods())
    tracked = set(tracked_workflow_surface_tool_names())
    missing = sorted(tracked - methods)
    assert not missing, f"Tracked workflow surface tools missing from catalogue: {missing}"


def test_workflow_schedule_gateway_tools_integrate_with_scheduler(monkeypatch):
    class _InMemoryWorkflowManager:
        def __init__(self) -> None:
            self.schedules: dict[str, Any] = {}
            self.instances: list[dict[str, object]] = []
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
            return instance_id

        def update_schedule_after_run(self, schedule_id: str, *, next_run_at=None):
            schedule = self.schedules.get(schedule_id)
            if schedule is None:
                return False
            schedule.next_run_at = next_run_at
            schedule.last_run_at = datetime.now(timezone.utc)
            return True

    manager = _InMemoryWorkflowManager()
    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager",
        lambda: manager,
    )

    gateway = _build_gateway()
    run_at = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    create_result = gateway.invoke(
        "workflow_create_schedule",
        {
            "workflow_id": "#V#generate_considerations_workflow",
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

    scheduler = WorkflowScheduler(manager, check_interval_seconds=0.01)  # type: ignore[arg-type]
    scheduler._process_due_schedules()
    metrics = scheduler.get_poll_metrics()
    assert metrics["due_count"] == 1
    assert metrics["triggered_count"] == 1
    assert metrics["due_schedules_seen_total"] == 1
    assert len(manager.instances) == 1
    assert manager.instances[0]["schedule_id"] == schedule_id

    trigger_result = gateway.invoke(
        "workflow_trigger_schedule",
        {"schedule_id": schedule_id},
    ).payload
    assert trigger_result.get("success") is True
    assert trigger_result.get("schedule_id") == schedule_id
    assert trigger_result.get("status") == "triggered"
    assert len(manager.instances) == 2
