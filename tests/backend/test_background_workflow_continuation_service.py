from __future__ import annotations

import importlib
from datetime import timedelta

import pytest

from src.backend.db import mongo_client
from src.backend.services import background_workflow_continuation_service as service
from src.backend.services import chat_prompt_queue_service as queue
from src.backend.workflows.durable import WorkflowInstanceManager
from src.backend.workflows.durable.models import WorkflowInstance


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    monkeypatch.setenv("VON_DB_NAME", "test_workflow_continuation")
    mongo_client.close_connection()
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_turn_execution_record_projection",
        lambda **kw: {
            "request_id": "request",
            "user_id": "#V#user",
            "namespace": "#V#user@org",
            "effect_observation_journal": {
                "event": {"status": "succeeded", "concept_id": "#V#existing_event"}
            },
        },
    )
    scope = queue.build_queue_scope(
        user_concept_id="#V#user",
        organisation_concept_id="#V#org",
        namespace="#V#user@org",
    )
    source = queue.create_queue_record(
        scope=scope,
        prompt_raw="Represent the attached event",
        session_id="session",
        conversation_key="conversation",
        status=queue.STATUS_IN_PROGRESS,
        client_request_id="request",
        attempt_id="attempt",
    )
    manager = WorkflowInstanceManager()
    instance = WorkflowInstance.create(
        "#V#interpret",
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
        source_event_type="conversation_turn",
        source_event_id="request",
        inputs={"file_copy_concept_id": "#V#poster"},
    )
    workflows = manager._get_instances_collection()
    workflows.insert_one(instance.to_doc())

    def register(**kwargs):
        return service.register_continuation(
            scope=scope,
            request_id="request",
            instance_id=instance.instance_id,
            workflow_id=instance.workflow_id,
            continuation_prompt="Read the completed extraction, reconcile existing event and finish representation",
            execution_envelope={
                "model": "gpt-6-astra",
                "model_parameters": {"reasoning_effort": "high"},
            },
            **kwargs,
        )

    def complete():
        queue.finish_prompt_record(
            scope=scope, queue_id=source["queue_id"], attempt_id="attempt"
        )
        workflows.update_one(
            {"instance_id": instance.instance_id},
            {
                "$set": {
                    "status": "completed",
                    "outputs": {"text": "Workshop schedule", "indexed": 1},
                }
            },
        )

    def due():
        queue._collection().update_many(
            {"workflow_continuation.status": "waiting"},
            {
                "$set": {
                    "workflow_continuation.next_check_at": queue._now()
                    - timedelta(seconds=1)
                }
            },
        )

    yield scope, source, instance, workflows, register, complete, due
    mongo_client.close_connection()


def test_restart_and_duplicate_completion_release_only_one_successor(scenario):
    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register()
    assert receipt["persisted"] is True
    assert register()["queue_id"] == receipt["queue_id"]
    assert service.reconcile_one_continuation() is False  # source still executing
    complete()
    due()
    importlib.reload(service)  # all continuation state must survive in the queue
    assert service.reconcile_one_continuation() is True
    assert service.reconcile_one_continuation() is False
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    assert record["dispatch_ready"] is True
    inputs = record["execution_envelope"]["workflow_inputs"]
    assert inputs["file_copy_concept_id"] == "#V#poster"
    observation = inputs["background_workflow_continuation"]["observations"][0]
    assert observation["outputs"]["text"] == "Workshop schedule"
    assert observation["domain_postconditions"] == "require_canonical_readback"
    assert (
        inputs["background_workflow_continuation"]["prior_turn_effects"][
            "effect_observation_journal"
        ]["event"]["concept_id"]
        == "#V#existing_event"
    )
    service.validate_continuation_dispatch(record)
    first = queue.reserve_next_server_dispatch(server_instance_id="server")
    assert first["queue_id"] == receipt["queue_id"]
    assert queue.reserve_next_server_dispatch(server_instance_id="server") is None
    assert queue._collection().count_documents({"source": "workflow_continuation"}) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "#V#other"),
        ("org_id", "#V#other"),
        ("namespace", "#V#other@org"),
        ("source_event_id", "other"),
    ],
)
def test_registration_rejects_foreign_workflow(scenario, field, value):
    scope, source, instance, workflows, register, complete, due = scenario
    workflows.update_one(
        {"instance_id": instance.instance_id}, {"$set": {field: value}}
    )
    assert register() == {
        "persisted": False,
        "reason": "workflow_source_scope_mismatch",
    }
    assert queue._collection().count_documents({"source": "workflow_continuation"}) == 0


@pytest.mark.parametrize("cancel_target", ["source", "held", "workflow"])
def test_cancellation_prevents_continuation(scenario, cancel_target):
    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register()
    complete()
    if cancel_target == "source":
        queue._collection().update_one(
            {"queue_id": source["queue_id"]},
            {"$set": {"cancellation_requested_at": queue._now()}},
        )
    elif cancel_target == "held":
        queue.cancel_prompt_record(scope=scope, queue_id=receipt["queue_id"])
    else:
        workflows.update_one(
            {"instance_id": instance.instance_id}, {"$set": {"status": "cancelled"}}
        )
    due()
    service.reconcile_one_continuation()
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    assert record["status"] == queue.STATUS_CANCELLED
    assert queue.reserve_next_server_dispatch(server_instance_id="server") is None


def test_cancellation_after_ready_is_rechecked_before_submission(scenario):
    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register()
    complete()
    assert service.reconcile_one_continuation()
    queue._collection().update_one(
        {"queue_id": source["queue_id"]},
        {"$set": {"cancellation_requested_at": queue._now()}},
    )
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    with pytest.raises(ValueError, match="cancelled"):
        service.validate_continuation_dispatch(record)


def test_failure_output_resumes_for_honest_result_instead_of_reexecuting_workflow(
    scenario,
):
    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register()
    complete()
    workflows.update_one(
        {"instance_id": instance.instance_id},
        {"$set": {"status": "failed", "outputs": {"error": "unsupported document"}}},
    )
    assert service.reconcile_one_continuation()
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    observation = record["execution_envelope"]["workflow_inputs"][
        "background_workflow_continuation"
    ]["observations"][0]
    assert observation["status"] == "failed"
    assert workflows.count_documents({}) == 1


def test_release_failure_recovers_without_another_queue_entry(scenario, monkeypatch):
    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register()
    complete()
    coll = queue._collection()
    original = coll.update_one

    def fail_once(query, update, *args, **kwargs):
        if update.get("$set", {}).get("dispatch_ready"):
            raise RuntimeError("interrupted before release")
        return original(query, update, *args, **kwargs)

    monkeypatch.setattr(coll, "update_one", fail_once)
    assert not service.reconcile_one_continuation()
    monkeypatch.setattr(coll, "update_one", original)
    due()
    assert service.reconcile_one_continuation()
    assert coll.count_documents({"source": "workflow_continuation"}) == 1


@pytest.mark.parametrize(
    "conversation_attachment,cached",
    [(False, True), (True, True), (True, False)],
)
def test_extracted_text_releases_consumer_while_indexing_is_still_pending(
    scenario, monkeypatch, conversation_attachment, cached
):
    import fitz
    from types import SimpleNamespace

    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register(ready_when="source_text_available")
    queue.finish_prompt_record(
        scope=scope, queue_id=source["queue_id"], attempt_id="attempt"
    )
    workflows.update_one(
        {"instance_id": instance.instance_id},
        {"$set": {"status": "running", "current_state": "index"}},
    )
    text = "Workshop schedule"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), text)
        data = pdf.tobytes()
    rows = []
    available = False
    fetches = []

    def get_bytes(key):
        fetches.append(key)
        if not available:
            raise FileNotFoundError(key)
        return data

    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service._load_file_copy_concept_doc",
        lambda **kw: {
            "concept_id": "#V#poster",
            "relationships": {"specific_to_user": ["#V#user"]},
            "attributes": {
                "conversation_image": conversation_attachment,
                "user_concept_id": "#V#user",
                "file_copy_metadata_storage": "attributes.v1",
                "blob_key": "poster",
                "content_type": "application/pdf",
                "original_filename": "schedule.pdf",
            },
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.get_texts_for_concept",
        lambda *a, **kw: rows,
    )
    monkeypatch.setattr(
        "src.backend.services.blob_store.get_blob_store_from_env",
        lambda: SimpleNamespace(get_bytes=get_bytes),
    )
    assert not service.reconcile_one_continuation()
    if cached:
        rows.append({"text": text})
    available = True
    fetches.clear()
    due()
    importlib.reload(service)
    assert service.reconcile_one_continuation()
    assert not service.reconcile_one_continuation()
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    inputs = record["execution_envelope"]["workflow_inputs"]
    observation = inputs["background_workflow_continuation"]["observations"][0]
    assert observation["status"] == "running"
    document = observation["source_documents"][0]
    assert document["text"].strip() == text
    assert document["source"] == ("persisted_hasContent" if cached else "file_bytes")
    assert fetches == ([] if cached else ["poster"])
    assert (
        workflows.find_one({"instance_id": instance.instance_id})["current_state"]
        == "index"
    )
    assert workflows.count_documents({}) == 1
    assert queue._collection().count_documents({"source": "workflow_continuation"}) == 1

    # The next event consumer receives usable source text on its normal launch
    # path even when the interpretation has never persisted hasContent.
    from src.backend.services.workflow_turn_capability_service import (
        WorkflowTurnCapability,
        build_workflow_execution_arguments,
    )

    arguments, _ = build_workflow_execution_arguments(
        WorkflowTurnCapability(
            name="event",
            workflow_id="#V#event",
            display_name="Event",
            description="Represent an event",
            relevance_score=1,
            input_schema={},
        ),
        {},
        prompt="Represent this event",
        context=[],
        conversation_situation=None,
        request_workflow_launch_inputs=inputs,
        user_concept_id=scope["user_concept_id"],
        organisation_concept_id=scope["organisation_concept_id"],
        namespace=scope["namespace"],
        maximum_wait_seconds=1,
    )
    assert text in arguments["inputs"]["prompt"]


def test_missing_effect_readback_waits_and_foreign_effect_readback_fails(
    scenario, monkeypatch
):
    scope, source, instance, workflows, register, complete, due = scenario
    receipt = register()
    complete()
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_turn_execution_record_projection",
        lambda **kw: None,
    )
    assert not service.reconcile_one_continuation()
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    assert record["status"] == queue.STATUS_QUEUED
    assert (
        "source_turn_effect_readback_unavailable"
        in record["workflow_continuation"]["last_error"]
    )
    monkeypatch.setattr(
        "src.backend.services.turn_execution_record_service.get_turn_execution_record_projection",
        lambda **kw: {"user_id": "#V#foreign", "namespace": "#V#user@org"},
    )
    due()
    assert service.reconcile_one_continuation()
    record = queue._collection().find_one({"queue_id": receipt["queue_id"]})
    assert record["status"] == queue.STATUS_FAILED
    assert "source_turn_effect_scope_mismatch" in record["last_error"]
    assert "queued_user_slot" not in record
