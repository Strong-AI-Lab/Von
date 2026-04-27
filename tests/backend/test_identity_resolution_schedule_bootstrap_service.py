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
        namespace="#V#system@default",
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
        namespace="#V#system@default",
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


def test_bootstrap_recreates_legacy_namespace_schedule(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    _reset_bootstrap_state(mod)
    legacy = WorkflowSchedule.create_interval(
        mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
        interval_seconds=21600,
        user_id="#V#system",
        org_id="#V#default",
        namespace="#V#system/#V#default",
        default_inputs={
            "managed_schedule_key": mod.IDENTITY_RESOLUTION_SCHEDULE_MANAGED_KEY,
        },
        description="managed",
    )
    legacy.enabled = True

    fake = _FakeManager([legacy])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_SCHEDULE_ENABLE", "1")
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_SCHEDULE_INTERVAL_SECONDS", "21600")

    report = mod.ensure_identity_resolution_background_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert report["disabled_count"] == 1
    assert (legacy.schedule_id, False) in fake.enabled_updates
    assert fake.created[0].namespace == "#V#system@default"


# --- Event-binding bootstrap (JVNAUTOSCI-2150 phase 1) -----------------------


class _FakeBindingManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.created = True
        self.updated = False

    def upsert_event_binding(self, **kwargs):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        self.calls.append(kwargs)
        binding = SimpleNamespace(
            binding_id="binding_test_1",
            event_type=kwargs["event_type"],
            workflow_id=kwargs["workflow_id"],
            input_mapping=dict(kwargs.get("input_mapping") or {}),
            enabled=bool(kwargs.get("enabled", True)),
            revision=1,
            to_status_dict=lambda self=None: {
                "binding_id": "binding_test_1",
                "event_type": kwargs["event_type"],
                "workflow_id": kwargs["workflow_id"],
                "enabled": bool(kwargs.get("enabled", True)),
                "input_mapping": dict(kwargs.get("input_mapping") or {}),
                "revision": 1,
            },
        )
        return binding, self.created, self.updated


def test_event_binding_bootstrap_registers_persistent_binding(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    fake = _FakeBindingManager()
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_EVENT_BINDING_ENABLE", "1")

    report = mod.ensure_identity_resolution_event_bindings()

    assert report["success"] is True
    assert report["ensured"] is True
    assert report["created_count"] == 1
    assert report["updated_count"] == 0
    assert report["binding_count"] == 1
    assert report["event_type"] == mod.IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE
    assert report["workflow_id"] == mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["event_type"] == mod.IDENTITY_RESOLUTION_REQUESTED_EVENT_TYPE
    assert call["workflow_id"] == mod.ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID
    assert call["enabled"] is True
    assert call["replace_existing"] is True
    assert call["actor"] == mod.IDENTITY_RESOLUTION_EVENT_BINDING_MANAGED_BY


def test_event_binding_bootstrap_idempotent_when_already_present(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    fake = _FakeBindingManager()
    fake.created = False
    fake.updated = False
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_EVENT_BINDING_ENABLE", "1")

    report = mod.ensure_identity_resolution_event_bindings()

    assert report["success"] is True
    assert report["created_count"] == 0
    assert report["updated_count"] == 0
    assert report["binding_count"] == 1


def test_event_binding_bootstrap_disabled_via_env(monkeypatch) -> None:
    from src.backend.services import identity_resolution_schedule_bootstrap_service as mod

    fake = _FakeBindingManager()
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_IDENTITY_RESOLUTION_EVENT_BINDING_ENABLE", "0")

    report = mod.ensure_identity_resolution_event_bindings()

    assert report["success"] is True
    assert report["ensured"] is False
    assert report["reason"] == "event_binding_bootstrap_disabled"
    assert report["binding_count"] == 0
    assert fake.calls == []
