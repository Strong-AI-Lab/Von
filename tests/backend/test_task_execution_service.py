"""Focused tests for durable TaskExecution attempt semantics."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.backend.services import task_execution_service as service


def _task() -> dict:
    return {
        "task_concept_id": "#V#task_prepare_brief",
        "title": "Prepare brief",
        "description": "Prepare and read back the research brief.",
        "status": "pending",
        "assignee_concept_id": "#V#von_system",
        "created_by_concept_id": "#V#alice",
        "organisation_concept_id": "#V#research_lab",
        "originating_conversation_id": "#V#conversation_1",
        "conversation_session_id": "session-1",
        "conversation_name": "Research planning",
    }


def _execution_doc(
    *, status: str = "pending", queue_id: str | None = "queue-1"
) -> dict:
    return {
        "concept_id": "#V#task_execution_1",
        "relationships": {"is_an_instance_of": ["#V#task_execution"]},
        "metadata": {
            "concept_type": "task_execution",
            "task_concept_id": "#V#task_prepare_brief",
            "creator_concept_id": "#V#alice",
            "executor_concept_id": "#V#von_system",
            "organisation_concept_id": "#V#research_lab",
            "namespace": "#V#alice@research_lab",
            "originating_conversation_id": "#V#conversation_1",
            "conversation_session_id": "session-1",
            "conversation_name": "Research planning",
            "launch_request_id": "launch-1",
            "enqueue_submission_id": "enqueue-1",
            "queue_id": queue_id,
            "execution_status": status,
            "execution_envelope_version": 1,
            "execution_envelope": {
                "turn_kind": "task_execution",
                "workflow_inputs": {},
            },
        },
    }


def test_create_task_execution_preserves_user_authority_and_von_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: dict = {}
    text_projection = MagicMock()

    def _insert(doc: dict) -> None:
        inserted.update(deepcopy(doc))

    monkeypatch.setattr(service.ConceptsRepository, "insert_one", _insert)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", text_projection)

    execution = service.create_task_execution(
        task=_task(),
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        namespace="#V#alice@research_lab",
        launch_request_id="launch-1",
        enqueue_submission_id="enqueue-1",
        execution_envelope={
            "turn_kind": "task_execution",
            "workflow_inputs": {"task_concept_id": "#V#task_prepare_brief"},
        },
    )

    assert execution["created"] is True
    assert execution["creator_concept_id"] == "#V#alice"
    assert execution["executor_concept_id"] == "#V#von_system"
    assert execution["status"] == "pending"
    assert inserted["relationships"]["#V#hasCreatedBy"] == ["#V#alice"]
    assert inserted["relationships"]["#V#hasExecutor"] == ["#V#von_system"]
    assert inserted["relationships"]["#V#executesTask"] == ["#V#task_prepare_brief"]
    assert inserted["metadata"]["enqueue_submission_id"] == "enqueue-1"
    text_projection.assert_called_once()


def test_logical_launch_is_stable_but_retry_launch_gets_new_execution() -> None:
    common = {
        "task_concept_id": "#V#task_prepare_brief",
        "creator_concept_id": "#V#alice",
        "organisation_concept_id": "#V#research_lab",
    }
    first = service.task_execution_concept_id_for_launch(
        **common,
        launch_request_id="launch-1",
    )
    replay = service.task_execution_concept_id_for_launch(
        **common,
        launch_request_id="launch-1",
    )
    retry = service.task_execution_concept_id_for_launch(
        **common,
        launch_request_id="launch-2",
    )

    assert first == replay
    assert retry != first
    assert service.task_execution_enqueue_submission_id_for_launch(
        **common,
        launch_request_id="launch-1",
    ) == service.task_execution_enqueue_submission_id_for_launch(
        **common,
        launch_request_id="launch-1",
    )


def test_duplicate_logical_launch_reuses_persisted_execution_and_enqueue_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: dict = {}

    def _insert_first(doc: dict) -> None:
        inserted.update(deepcopy(doc))

    monkeypatch.setattr(service.ConceptsRepository, "insert_one", _insert_first)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", MagicMock())
    first = service.create_task_execution(
        task=_task(),
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        namespace="#V#alice@research_lab",
        launch_request_id="launch-1",
        enqueue_submission_id="enqueue-first",
        execution_envelope={"turn_kind": "user_message", "workflow_inputs": {}},
    )

    monkeypatch.setattr(
        service.ConceptsRepository,
        "insert_one",
        MagicMock(side_effect=service.DuplicateKeyError("duplicate execution")),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        MagicMock(return_value=inserted),
    )
    replay = service.create_task_execution(
        task=_task(),
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        namespace="#V#alice@research_lab",
        launch_request_id="launch-1",
        enqueue_submission_id="enqueue-first",
        execution_envelope={"turn_kind": "user_message", "workflow_inputs": {}},
    )

    assert replay["created"] is False
    assert replay["task_execution_concept_id"] == first["task_execution_concept_id"]
    assert replay["enqueue_submission_id"] == "enqueue-first"


def test_duplicate_logical_launch_rejects_changed_queue_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _execution_doc(status="pending", queue_id=None)
    existing["metadata"]["execution_envelope"] = {
        "turn_kind": "user_message",
        "workflow_inputs": {},
    }
    monkeypatch.setattr(
        service.ConceptsRepository,
        "insert_one",
        MagicMock(side_effect=service.DuplicateKeyError("duplicate execution")),
    )
    monkeypatch.setattr(
        service.ConceptsRepository,
        "find_one",
        MagicMock(return_value=existing),
    )

    with pytest.raises(
        service.TaskExecutionAccessError,
        match="launch identity is already bound",
    ):
        service.create_task_execution(
            task=_task(),
            creator_concept_id="#V#alice",
            organisation_concept_id="#V#research_lab",
            namespace="#V#alice@research_lab",
            launch_request_id="launch-1",
            enqueue_submission_id="different-enqueue",
            execution_envelope={"turn_kind": "user_message", "workflow_inputs": {}},
        )


def test_execution_transition_does_not_complete_task_specification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = _execution_doc(status="pending")
    in_progress = _execution_doc(status="in_progress")
    find_one = MagicMock(side_effect=[pending, in_progress])
    update_one = MagicMock(return_value=SimpleNamespace(modified_count=1))

    monkeypatch.setattr(service.ConceptsRepository, "find_one", find_one)
    monkeypatch.setattr(service.ConceptsRepository, "update_one", update_one)
    monkeypatch.setattr(service, "upsert_singleton_text_relation", MagicMock())

    result = service.transition_task_execution(
        "#V#task_execution_1",
        actor_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        enqueue_submission_id="enqueue-1",
        queue_id="queue-1",
        status="in_progress",
    )

    assert result["status"] == "in_progress"
    update_one.assert_called_once()


def test_queue_outbox_reconciliation_creates_and_binds_exact_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = service.task_execution_concept_id_for_launch(
        task_concept_id="#V#task_prepare_brief",
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        launch_request_id="launch-1",
    )
    enqueue_id = service.task_execution_enqueue_submission_id_for_launch(
        task_concept_id="#V#task_prepare_brief",
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        launch_request_id="launch-1",
    )
    envelope = {
        "initiation_id": "launch-1",
        "turn_kind": "user_message",
        "workflow_inputs": {
            "task_concept_id": "#V#task_prepare_brief",
            "task_execution_concept_id": execution_id,
            "originating_conversation_concept_id": "#V#conversation_1",
            "conversation_session_id": "session-1",
            "authority_actor_concept_id": "#V#alice",
            "authority_organisation_concept_id": "#V#research_lab",
            "authority_namespace": "#V#alice@research_lab",
            "executor_concept_id": "#V#von_system",
        },
    }
    create = MagicMock(
        return_value={
            "task_execution_concept_id": execution_id,
            "enqueue_submission_id": enqueue_id,
        }
    )
    bind = MagicMock(return_value={"task_execution_concept_id": execution_id})
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task",
        lambda _task_id: _task(),
    )
    monkeypatch.setattr(service, "create_task_execution", create)
    monkeypatch.setattr(service, "bind_task_execution_queue_record", bind)

    result = service.reconcile_task_execution_queue_record(
        {
            "queue_id": "queue-1",
            "task_concept_id": "#V#task_prepare_brief",
            "task_execution_concept_id": execution_id,
            "user_concept_id": "#V#alice",
            "organisation_concept_id": "#V#research_lab",
            "namespace": "#V#alice@research_lab",
            "enqueue_submission_id": enqueue_id,
            "session_id": "session-1",
            "execution_envelope_version": 1,
            "execution_envelope": envelope,
        }
    )

    assert result["task_execution_concept_id"] == execution_id
    create.assert_called_once()
    bind.assert_called_once()


def test_queue_outbox_reconciliation_rejects_task_cancelled_after_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = service.task_execution_concept_id_for_launch(
        task_concept_id="#V#task_prepare_brief",
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        launch_request_id="launch-1",
    )
    enqueue_id = service.task_execution_enqueue_submission_id_for_launch(
        task_concept_id="#V#task_prepare_brief",
        creator_concept_id="#V#alice",
        organisation_concept_id="#V#research_lab",
        launch_request_id="launch-1",
    )
    cancelled = _task()
    cancelled["status"] = "cancelled"
    monkeypatch.setattr(
        "src.backend.services.task_management_service.get_task",
        lambda _task_id: cancelled,
    )
    create = MagicMock(
        return_value={
            "task_execution_concept_id": execution_id,
            "enqueue_submission_id": enqueue_id,
        }
    )
    bind = MagicMock(return_value={"task_execution_concept_id": execution_id})
    monkeypatch.setattr(service, "create_task_execution", create)
    monkeypatch.setattr(service, "bind_task_execution_queue_record", bind)

    with pytest.raises(
        service.TaskExecutionAccessError,
        match="pending or in-progress",
    ):
        service.reconcile_task_execution_queue_record(
            {
                "queue_id": "queue-1",
                "task_concept_id": "#V#task_prepare_brief",
                "task_execution_concept_id": execution_id,
                "user_concept_id": "#V#alice",
                "organisation_concept_id": "#V#research_lab",
                "namespace": "#V#alice@research_lab",
                "enqueue_submission_id": enqueue_id,
                "session_id": "session-1",
                "execution_envelope_version": 1,
                "execution_envelope": {
                    "initiation_id": "launch-1",
                    "turn_kind": "user_message",
                    "workflow_inputs": {
                        "task_concept_id": "#V#task_prepare_brief",
                        "task_execution_concept_id": execution_id,
                        "originating_conversation_concept_id": "#V#conversation_1",
                        "conversation_session_id": "session-1",
                        "authority_actor_concept_id": "#V#alice",
                        "authority_organisation_concept_id": "#V#research_lab",
                        "authority_namespace": "#V#alice@research_lab",
                        "executor_concept_id": "#V#von_system",
                    },
                },
            }
        )
    create.assert_called_once()
    bind.assert_called_once()
