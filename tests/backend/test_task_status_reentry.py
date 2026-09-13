"""A current status must survive revisiting an earlier state."""

from unittest.mock import MagicMock

import pytest

from src.backend.services import task_management_service as tasks


def test_reentering_status_replaces_old_value_and_emits_actual_transition(monkeypatch):
    doc = {"concept_id": "#V#task_reentry", "relationships": {}, "metadata": {}}
    values = ["pending", "in_progress", "blocked"]

    def read(_):
        return {"task_concept_id": doc["concept_id"], "status": values[-1]}

    def additive(**kwargs):
        if kwargs["text"] not in values:
            values.append(kwargs["text"])

    def singleton(**kwargs):
        assert kwargs["garbage_collect"] is False
        values[:] = [kwargs["text"]]

    monkeypatch.setattr(tasks, "_get_task_doc", lambda _: (doc["concept_id"], doc))
    monkeypatch.setattr(tasks, "_build_task_response", lambda _: read(None))
    monkeypatch.setattr(tasks, "get_task", read)
    monkeypatch.setattr(tasks, "upsert_text_for_concept", additive)
    monkeypatch.setattr(tasks, "upsert_singleton_text_relation", singleton)
    monkeypatch.setattr(tasks, "_clear_task_text_relations", MagicMock())
    monkeypatch.setattr(tasks, "ConceptsRepository", MagicMock())
    history = MagicMock()
    launches = MagicMock(return_value={})
    monkeypatch.setattr(tasks, "_append_task_history_event", history)
    monkeypatch.setattr(tasks, "maybe_launch_task_status_workflow", launches)

    for status in ("in_progress", "deployed", "in_progress"):
        assert tasks.update_task_status(doc["concept_id"], status)["status"] == status
        assert values == [status]
    assert history.call_count == launches.call_count == 3
    tasks.update_task_status(doc["concept_id"], "in_progress")
    assert history.call_count == launches.call_count == 3


def test_failed_status_readback_does_not_emit_completion(monkeypatch):
    doc = {"concept_id": "#V#task_readback", "relationships": {}}
    stale = {"status": "blocked"}
    monkeypatch.setattr(tasks, "_get_task_doc", lambda _: (doc["concept_id"], doc))
    monkeypatch.setattr(tasks, "_build_task_response", lambda _: stale)
    monkeypatch.setattr(tasks, "get_task", lambda _: stale)
    monkeypatch.setattr(tasks, "_upsert_optional_task_text", MagicMock())
    repository = MagicMock()
    completion = MagicMock()
    monkeypatch.setattr(tasks, "ConceptsRepository", repository)
    monkeypatch.setattr(tasks, "maybe_launch_effort_unit_completed_workflow", completion)
    with pytest.raises(tasks.TaskManagementError, match="canonical read-back"):
        tasks.update_task_status(doc["concept_id"], "completed")
    repository.update_one.assert_not_called()
    completion.assert_not_called()


def test_deployed_transition_and_reopening(monkeypatch):
    current = {"task_concept_id": "#V#task_deploy", "status": "completed"}
    monkeypatch.setattr(tasks, "get_task", lambda _: current)
    update = MagicMock(return_value={**current, "status": "deployed"})
    monkeypatch.setattr(tasks, "update_task_status", update)
    monkeypatch.setattr(tasks, "_append_task_history_event", MagicMock())
    tasks.transition_task(current["task_concept_id"], transition_id="deploy")
    assert update.call_args.args[:2] == (current["task_concept_id"], "deployed")
    current["status"] = "deployed"
    destinations = {item["to_status"] for item in tasks.get_task_transitions(current["task_concept_id"])["transitions"]}
    assert destinations == {"completed", "pending", "in_progress"}
    assert "deployed" in tasks.VALID_TASK_STATUSES
