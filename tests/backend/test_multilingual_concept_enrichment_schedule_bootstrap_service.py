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
    from src.backend.services import (
        multilingual_concept_enrichment_schedule_bootstrap_service as mod,
    )

    _reset_bootstrap_state(mod)
    fake = _FakeManager()
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_ENABLE", "1")

    report = mod.ensure_multilingual_concept_enrichment_background_schedule()

    assert report["success"] is True
    assert report["created_count"] == 1
    assert len(fake.created) == 1
    created_schedule = fake.created[0]
    assert created_schedule.workflow_id == mod.MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID
    assert created_schedule.default_inputs["managed_schedule_key"] == (
        mod.MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY
    )
    assert created_schedule.default_inputs["target_languages"] == ["zh-Hans", "es", "fr"]


def test_bootstrap_reuses_existing_matching_schedule(monkeypatch) -> None:
    from src.backend.services import (
        multilingual_concept_enrichment_schedule_bootstrap_service as mod,
    )

    _reset_bootstrap_state(mod)
    existing = WorkflowSchedule.create_interval(
        mod.MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
        interval_seconds=86400,
        user_id="#V#system",
        org_id="#V#default",
        namespace="#V#system@default",
        default_inputs={
            "managed_schedule_key": mod.MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_MANAGED_KEY,
            "scan_limit": 24,
            "max_mutations_per_run": 6,
            "min_confidence": 0.86,
            "minimum_total_usage": 4,
            "min_description_chars": 160,
            "reanalyse_after_hours": 720,
            "target_languages": ["zh-Hans", "es", "fr"],
        },
        description="managed",
    )
    existing.enabled = False
    fake = _FakeManager([existing])
    monkeypatch.setattr(mod, "get_instance_manager", lambda: fake)
    monkeypatch.setenv("VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_ENABLE", "1")
    monkeypatch.setenv(
        "VON_MULTILINGUAL_CONCEPT_ENRICHMENT_SCHEDULE_INTERVAL_SECONDS",
        "86400",
    )

    report = mod.ensure_multilingual_concept_enrichment_background_schedule()

    assert report["success"] is True
    assert report["created_count"] == 0
    assert report["updated_count"] == 1
    assert fake.enabled_updates == [(existing.schedule_id, True)]
