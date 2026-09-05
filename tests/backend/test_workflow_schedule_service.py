from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from src.backend.services import workflow_schedule_service as service
from src.backend.services.workflow_actor_scope_service import WorkflowActorScope


class Manager:
    def __init__(self):
        self.schedules = {}
        self.creates = 0

    def create_schedule(self, schedule):
        self.creates += 1
        schedule = deepcopy(schedule)
        schedule.schedule_id = "#V#schedule_" + schedule.schedule_id
        if schedule.schedule_id in self.schedules:
            raise RuntimeError("duplicate")
        self.schedules[schedule.schedule_id] = schedule
        return schedule.schedule_id

    def get_schedule(self, schedule_id):
        return deepcopy(self.schedules.get(schedule_id))

    def set_schedule_enabled(self, schedule_id, enabled):
        self.schedules[schedule_id].enabled = enabled
        return True


@pytest.fixture
def actor():
    return WorkflowActorScope("#V#owner", "#V#org", "#V#owner@org", "test")


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(
        service.workflow_listing_service,
        "filter_workflow_ids_for_current_actor",
        lambda ids: ids,
    )
    monkeypatch.setattr(
        service,
        "verify_workflow_runnable",
        lambda *a, **kw: SimpleNamespace(
            runnable_verification_success=True,
            definition_identity={"definition_hash": "version-a"},
        ),
    )
    monkeypatch.setattr(
        service,
        "_resolve_submission_launch_inputs",
        lambda **kw: (
            object(),
            SimpleNamespace(unresolved_required_inputs=(), resolved_inputs={}),
        ),
    )
    return Manager()


def command(**kwargs):
    return {
        "workflow_id": "#V#digest",
        "schedule_type": "interval",
        "interval_seconds": 3600,
        "default_inputs": {"prompt": "Maintain the current brief"},
        **kwargs,
    }


def test_creation_retry_readback_and_pause_resume_share_identity(ready, actor):
    first = service.create_actor_schedule(
        ready, command(), actor, request_id="turn-1", conversation_id="chat-1"
    )
    again = service.create_actor_schedule(
        ready, command(), actor, request_id="turn-1", conversation_id="chat-1"
    )
    assert first["changed"] is True and again["idempotent_replay"] is True
    assert first["schedule_id"] == again["schedule_id"] and ready.creates == 1
    assert first["origin"] == "actor_owned"
    assert first["creation_context"]["conversation_id"] == "chat-1"
    assert first["default_inputs"]["user_concept_id"] == actor.user_concept_id
    assert datetime.fromisoformat(first["next_run_at"]) > datetime.now(
        timezone.utc
    ) + timedelta(minutes=59)
    paused = service.set_owned_schedule_enabled(
        ready, first["schedule_id"], False, actor
    )
    assert paused["enabled"] is False and paused["changed"] is True
    assert (
        service.set_owned_schedule_enabled(ready, first["schedule_id"], False, actor)[
            "changed"
        ]
        is False
    )
    assert (
        service.set_owned_schedule_enabled(ready, first["schedule_id"], True, actor)[
            "schedule_id"
        ]
        == first["schedule_id"]
    )
    assert ready.creates == 1


@pytest.mark.parametrize(
    "change",
    [{"interval_seconds": 60}, {"default_inputs": {"prompt": "Another brief"}}],
)
def test_materially_different_intent_has_a_distinct_schedule(ready, actor, change):
    a = service.create_actor_schedule(ready, command(), actor, request_id="turn-1")
    b = service.create_actor_schedule(
        ready, command(**change), actor, request_id="turn-1"
    )
    assert a["schedule_id"] != b["schedule_id"]


@pytest.mark.parametrize(
    "scope",
    [
        WorkflowActorScope("#V#other", "#V#org", "#V#other@org", "test"),
        WorkflowActorScope("#V#owner", "#V#other_org", "#V#owner@other_org", "test"),
    ],
)
def test_schedule_read_and_pause_are_exactly_actor_scoped(ready, actor, scope):
    created = service.create_actor_schedule(ready, command(), actor)
    with pytest.raises(service.ScheduleCommandError, match="current actor scope"):
        service.get_owned_schedule(ready, created["schedule_id"], scope)
    with pytest.raises(service.ScheduleCommandError):
        service.set_owned_schedule_enabled(ready, created["schedule_id"], False, scope)
    assert ready.get_schedule(created["schedule_id"]).enabled


@pytest.mark.parametrize(
    "change",
    [
        {"interval_seconds": 0},
        {"interval_seconds": -1},
        {"interval_seconds": True},
        {"schedule_type": "cron", "cron_expression": "99 * * * *"},
        {"schedule_type": "cron", "cron_expression": "*/0 * * * *"},
        {"schedule_type": "once", "run_at": "tomorrow"},
        {"default_inputs": []},
    ],
)
def test_invalid_cadence_or_inputs_do_not_create_state(ready, actor, change):
    with pytest.raises(service.ScheduleCommandError):
        service.create_actor_schedule(ready, command(**change), actor)
    assert ready.creates == 0


def test_unrunnable_and_missing_required_input_do_not_create(ready, actor, monkeypatch):
    monkeypatch.setattr(
        service,
        "verify_workflow_runnable",
        lambda *a, **kw: SimpleNamespace(runnable_verification_success=False),
    )
    with pytest.raises(service.ScheduleCommandError, match="not executable"):
        service.create_actor_schedule(ready, command(), actor)
    monkeypatch.setattr(
        service,
        "verify_workflow_runnable",
        lambda *a, **kw: SimpleNamespace(runnable_verification_success=True),
    )
    monkeypatch.setattr(
        service,
        "_resolve_submission_launch_inputs",
        lambda **kw: (
            object(),
            SimpleNamespace(unresolved_required_inputs=("prompt",)),
        ),
    )
    with pytest.raises(service.ScheduleCommandError, match="prompt"):
        service.create_actor_schedule(ready, command(), actor)
    assert ready.creates == 0


def test_once_timezone_normalisation_and_retry_after_due(ready, actor):
    a = service.create_actor_schedule(
        ready, command(schedule_type="once", run_at="2026-01-01T12:00:00+12:00"), actor
    )
    b = service.create_actor_schedule(
        ready, command(schedule_type="once", run_at="2026-01-01T00:00:00Z"), actor
    )
    assert a["schedule_id"] == b["schedule_id"] and b["idempotent_replay"]
    assert a["run_at"] == "2026-01-01T00:00:00+00:00"


def test_explicitly_unpublished_executable_creates_no_schedule(
    ready, actor, monkeypatch
):
    monkeypatch.setattr(
        service,
        "_resolve_submission_launch_inputs",
        lambda **kw: (
            SimpleNamespace(metadata={"publication_lifecycle": {"published": False}}),
            SimpleNamespace(unresolved_required_inputs=(), resolved_inputs={}),
        ),
    )
    with pytest.raises(service.ScheduleCommandError, match="unpublished"):
        service.create_actor_schedule(ready, command(), actor)
    assert ready.creates == 0


def test_retry_does_not_claim_success_for_changed_persisted_configuration(ready, actor):
    created = service.create_actor_schedule(ready, command(), actor)
    ready.schedules[created["schedule_id"]].default_inputs[
        "prompt"
    ] = "Changed elsewhere"
    with pytest.raises(service.ScheduleCommandError, match="requested configuration"):
        service.create_actor_schedule(ready, command(), actor)
    assert ready.creates == 1


def test_cron_preview_matches_existing_scheduler_semantics(ready, actor):
    proposed = service.prepare_actor_schedule(
        command(schedule_type="cron", cron_expression="0 9 * * 0-4"), actor
    )
    assert proposed.next_run_at.hour == 9
    assert proposed.next_run_at.minute == 0
    assert proposed.next_run_at.weekday() < 5
    assert ready.creates == 0


def test_ordinary_schedule_binding_hides_and_replaces_model_identity(
    ready, actor, monkeypatch
):
    from src.backend.integrations.internal_mcp import build_default_catalogue
    from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
    from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
    from src.backend.services.adaptive_turn_service import (
        _trusted_tool_payload,
        _model_visible_input_schema,
    )
    from src.backend.security.access_control import override_current_actor

    monkeypatch.setattr(
        "src.backend.workflows.durable.WorkflowInstanceManager", lambda: ready
    )
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    trusted = {
        "actor_user_concept_id": actor.user_concept_id,
        "actor_organisation_concept_id": actor.organisation_concept_id,
        "turn_namespace": actor.namespace,
        "conversation_id": "chat-1",
        "turn_id": "turn-1",
    }
    definition = gateway.get_method_definition("workflow_create_schedule")
    assert definition.ordinary_turn_effect is True
    schema = _model_visible_input_schema(definition, trusted)
    assert not {
        "user_id",
        "org_id",
        "namespace",
        "conversation_id",
        "request_id",
    } & set(schema["properties"])
    bound = _trusted_tool_payload(
        gateway=gateway,
        tool_name="workflow_create_schedule",
        model_payload=command(
            user_id="#V#outsider",
            org_id="#V#foreign",
            namespace="#V#outsider@foreign",
            request_id="forged",
        ),
        trusted_argument_values=trusted,
    )
    with override_current_actor(actor.user_concept_id, actor.organisation_concept_id):
        result = gateway.invoke("workflow_create_schedule", bound).payload
    assert result["success"] is True
    assert result["user_id"] == actor.user_concept_id
    assert result["creation_context"]["request_id"] == "turn-1"
    assert result["effect_status"] == "succeeded"
    assert (
        gateway.get_method_definition(
            "workflow_set_schedule_enabled"
        ).ordinary_turn_effect
        is True
    )
    assert (
        gateway.get_method_definition("workflow_delete_schedule").ordinary_turn_effect
        is False
    )
    assert (
        gateway.get_method_definition("workflow_trigger_schedule").ordinary_turn_effect
        is False
    )
    assert gateway.get_method_definition("task_create").ordinary_turn_effect is True
