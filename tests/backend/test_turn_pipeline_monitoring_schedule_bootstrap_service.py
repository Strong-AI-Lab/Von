from __future__ import annotations

from src.backend.workflows.durable.models import WorkflowSchedule


class _FakeManager:
    def __init__(self, schedules: list[WorkflowSchedule] | None = None) -> None:
        self.schedules = schedules or []
        self.created: list[WorkflowSchedule] = []
        self.enabled_updates: list[tuple[str, bool]] = []

    def list_schedules(self, limit: int = 50):
        return list(self.schedules)[:limit]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> bool:
        self.enabled_updates.append((schedule_id, enabled))
        for schedule in self.schedules:
            if schedule.schedule_id == schedule_id:
                schedule.enabled = enabled
                return True
        return False

    def create_schedule(self, schedule: WorkflowSchedule) -> str:
        self.created.append(schedule)
        self.schedules.append(schedule)
        return schedule.schedule_id


def _reset_bootstrap_state(mod) -> None:
    mod._bootstrap_completed = False


def test_bootstrap_creates_turn_pipeline_monitoring_schedules_when_missing(
    monkeypatch,
) -> None:
    from src.backend.services import turn_pipeline_monitoring_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    fake = _FakeManager()
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_TURN_PIPELINE_MONITORING_SCHEDULE_ENABLE", "1")

    report = mod.ensure_turn_pipeline_monitoring_schedules()

    assert report["success"] is True
    assert report["created_count"] == 2
    assert report["ensured"] is True
    assert len(fake.created) == 2
    created_workflow_ids = {schedule.workflow_id for schedule in fake.created}
    assert created_workflow_ids == {
        mod.TURN_PIPELINE_MONITORING_WORKFLOW_ID,
        mod.TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
    }
    schedule_reports = report["schedule_reports"]
    assert schedule_reports["monitoring"]["managed_schedule_key"] == (
        mod.TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY
    )
    assert schedule_reports["tier1_regression"]["managed_schedule_key"] == (
        mod.TURN_PIPELINE_TIER1_REGRESSION_SCHEDULE_MANAGED_KEY
    )


def test_bootstrap_reuses_existing_matching_turn_pipeline_monitoring_schedules(
    monkeypatch,
) -> None:
    from src.backend.services import turn_pipeline_monitoring_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    desired = mod._desired_schedule_configs()
    monitoring = WorkflowSchedule.create_interval(
        mod.TURN_PIPELINE_MONITORING_WORKFLOW_ID,
        interval_seconds=desired["monitoring"]["interval_seconds"],
        user_id=desired["monitoring"]["user_id"],
        org_id=desired["monitoring"]["org_id"],
        namespace=desired["monitoring"]["namespace"],
        default_inputs=dict(desired["monitoring"]["default_inputs"]),
        description="managed",
    )
    monitoring.enabled = False
    regression = WorkflowSchedule.create_interval(
        mod.TURN_PIPELINE_TIER1_REGRESSION_WORKFLOW_ID,
        interval_seconds=desired["tier1_regression"]["interval_seconds"],
        user_id=desired["tier1_regression"]["user_id"],
        org_id=desired["tier1_regression"]["org_id"],
        namespace=desired["tier1_regression"]["namespace"],
        default_inputs=dict(desired["tier1_regression"]["default_inputs"]),
        description="managed",
    )
    regression.enabled = False

    fake = _FakeManager([monitoring, regression])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_TURN_PIPELINE_MONITORING_SCHEDULE_ENABLE", "1")

    report = mod.ensure_turn_pipeline_monitoring_schedules()

    assert report["success"] is True
    assert report["created_count"] == 0
    assert report["updated_count"] == 2
    assert (monitoring.schedule_id, True) in fake.enabled_updates
    assert (regression.schedule_id, True) in fake.enabled_updates


def test_bootstrap_disables_mismatched_turn_pipeline_monitoring_schedule(
    monkeypatch,
) -> None:
    from src.backend.services import turn_pipeline_monitoring_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    mismatched = WorkflowSchedule.create_interval(
        mod.TURN_PIPELINE_MONITORING_WORKFLOW_ID,
        interval_seconds=900,
        user_id="#V#system",
        org_id="#V#default",
        namespace="#V#system@default",
        default_inputs={
            "managed_schedule_key": mod.TURN_PIPELINE_MONITORING_SCHEDULE_MANAGED_KEY,
            "namespace": "#V#system@default",
            "limit": 10,
            "max_cases": 2,
        },
        description="managed",
    )
    mismatched.enabled = True

    fake = _FakeManager([mismatched])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_TURN_PIPELINE_MONITORING_SCHEDULE_ENABLE", "1")

    report = mod.ensure_turn_pipeline_monitoring_schedules()

    assert report["success"] is True
    assert report["created_count"] == 2
    assert report["disabled_count"] == 1
    assert (mismatched.schedule_id, False) in fake.enabled_updates
