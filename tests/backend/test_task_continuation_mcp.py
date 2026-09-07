"""Ordinary chat can maintain a task without transferring its authority."""

from copy import deepcopy

import pytest

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp import catalogue as handlers
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import task_management_service
from src.backend.services.adaptive_turn_service import (
    _model_visible_input_schema,
    _trusted_tool_payload,
)


@pytest.fixture
def task_state(monkeypatch):
    task = {
        "task_concept_id": "#V#task_test",
        "created_by_concept_id": "#V#alice",
        "assignee_concept_id": "#V#alice",
        "organisation_concept_id": "#V#lab",
        "current_work_product": {"status": "missing"},
    }
    writes = []
    monkeypatch.setattr(task_management_service, "get_task", lambda _: deepcopy(task))

    def update(task_id, *, fields, actor_concept_id):
        assert task_id == task["task_concept_id"]
        assert actor_concept_id == "#V#alice"
        writes.append(deepcopy(fields))
        for key, value in fields.items():
            if key == "current_work_product_concept_id":
                task["current_work_product"] = {"concept_id": value, "status": "ready"}
            else:
                task[key] = value
        return {"task": deepcopy(task), "changed_fields": list(fields), "warnings": []}

    monkeypatch.setattr(task_management_service, "update_task_fields", update)
    return task, writes


def _payload(**fields):
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    return _trusted_tool_payload(
        gateway=gateway,
        tool_name="task_update_fields",
        model_payload={
            "task_concept_id": "#V#task_test",
            "fields": fields,
            "actor_bound_continuation": False,
            "actor_concept_id": "#V#spoofed",
        },
        trusted_argument_values={
            "actor_user_concept_id": "#V#alice",
            "actor_organisation_concept_id": "#V#lab",
            "turn_namespace": "#V#alice@lab",
        },
    )


def test_ordinary_continuation_links_product_and_delegates_without_duplicate_write(
    task_state,
):
    task, writes = task_state
    payload = _payload(
        current_work_product_concept_id="#V#paper_responsibility_brief",
        assignee_concept_id="#V#von_system",
        report_to_concept_id="#V#alice",
        next_checkpoint="Continue from the saved backlog checkpoint",
    )
    assert payload["actor_bound_continuation"] is True
    assert payload["actor_concept_id"] == "#V#alice"
    with override_current_actor("#V#alice", "#V#lab"):
        first = handlers._task_update_fields(**payload)
        repeat = handlers._task_update_fields(**payload)
    assert first["effect_status"] == "succeeded"
    assert first["canonical_read_back"]["current_work_product"]["status"] == "ready"
    assert first["canonical_read_back"]["assignee_concept_id"] == "#V#von_system"
    assert repeat["changed"] is False
    assert repeat["idempotent_replay"] is True
    assert len(writes) == 1
    assert task["created_by_concept_id"] == "#V#alice"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("organisation_concept_id", "#V#other_org", "task_continuation_fields_denied"),
        ("created_by_concept_id", "#V#bob", "task_continuation_fields_denied"),
        ("watcher_concept_ids", ["#V#bob"], "task_continuation_fields_denied"),
        ("assignee_concept_id", "#V#bob", "task_assignment_scope_denied"),
        ("report_to_concept_id", "#V#bob", "task_report_scope_denied"),
    ],
)
def test_continuation_rejects_scope_changes_before_any_edit(
    task_state, field, value, code
):
    _, writes = task_state
    with override_current_actor("#V#alice", "#V#lab"):
        result = handlers._task_update_fields(
            **_payload(notes="Should not persist", **{field: value})
        )
    assert result["error_code"] == code
    assert writes == []


@pytest.mark.parametrize(
    "changes",
    [
        {"organisation_concept_id": "#V#other_org"},
        {"created_by_concept_id": "#V#bob", "assignee_concept_id": "#V#bob"},
    ],
)
def test_continuation_requires_control_of_same_org_task(task_state, changes):
    task, writes = task_state
    task.update(changes)
    with override_current_actor("#V#alice", "#V#lab"):
        result = handlers._task_update_fields(**_payload(notes="Should not persist"))
    assert result["error_code"] == "task_actor_scope_denied"
    assert writes == []


def test_continuation_schema_exposes_product_and_hides_authority_switch():
    definition = build_default_catalogue().get("task_update_fields")
    assert definition.ordinary_turn_effect is True
    schema = _model_visible_input_schema(definition)
    assert "actor_bound_continuation" not in schema["properties"]
    assert "actor_concept_id" not in schema["properties"]
    assert "current_work_product_concept_id" in definition.description
