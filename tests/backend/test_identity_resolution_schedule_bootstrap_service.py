"""Tests for identity-resolution schedule bootstrap service (JVNAUTOSCI-1253)."""

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


def test_bootstrap_creates_schedule_when_missing(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    fake = _FakeManager()
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_SCHEDULE_ENABLE", "1")

    report = mod.ensure_identity_resolution_background_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert report["ensured"] is True
    assert len(fake.created) == 1
    created_schedule = fake.created[0]
    assert created_schedule.workflow_id == mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    assert created_schedule.default_inputs.get("managed_schedule_key") == (
        mod.IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY
    )


def test_bootstrap_reuses_existing_matching_schedule(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    interval_seconds = 21600
    existing = WorkflowSchedule.create_interval(
        mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        interval_seconds=interval_seconds,
        user_id="#V#system",
        org_id="#V#default",
        namespace="#V#system/#V#default",
        default_inputs={
            "managed_schedule_key": mod.IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY,
        },
        description="managed",
    )
    existing.enabled = False

    fake = _FakeManager([existing])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_SCHEDULE_ENABLE", "1")
    monkeypatch.setenv(
        "VON_IDENTITY_RESOLUTION_SCHEDULE_INTERVAL_SECONDS",
        str(interval_seconds),
    )

    report = mod.ensure_identity_resolution_background_schedule()

    assert report["success"] is True
    assert report["created_count"] == 0
    assert report["updated_count"] == 1
    assert fake.enabled_updates == [(existing.schedule_id, True)]


def test_bootstrap_disables_mismatch_and_recreates(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    mismatched = WorkflowSchedule.create_interval(
        mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        interval_seconds=1800,
        user_id="#V#system",
        org_id="#V#default",
        namespace="#V#system/#V#default",
        default_inputs={
            "managed_schedule_key": mod.IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY,
        },
        description="managed",
    )
    mismatched.enabled = True

    fake = _FakeManager([mismatched])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_SCHEDULE_ENABLE", "1")
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_SCHEDULE_INTERVAL_SECONDS", "21600")

    report = mod.ensure_identity_resolution_background_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert report["disabled_count"] == 1
    assert (mismatched.schedule_id, False) in fake.enabled_updates
