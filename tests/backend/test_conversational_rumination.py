"""Inquiry provenance, retry and source/product isolation at the service boundary."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.backend.services import conversational_rumination_service as rumination
from src.backend.services import chat_history_service, task_execution_submission_service
from src.backend.integrations.internal_mcp import catalogue


@pytest.fixture
def inquiry(monkeypatch):
    scope = SimpleNamespace(
        user_concept_id="#V#alice", organisation_concept_id="#V#lab"
    )
    monkeypatch.setattr(
        catalogue, "_resolve_task_actor_scope", lambda *a, **k: (scope, None)
    )
    state = {"origin_kind": "user"}
    monkeypatch.setattr(
        chat_history_service, "get_chat_history_session_state", lambda **k: state
    )
    stored = {}

    def create(**kw):
        key = kw["idempotency_key"]
        if key not in stored:
            stored[key] = {**kw, "task_concept_id": "#V#inquiry", "status": "pending"}
        return {"success": True, "canonical_read_back": stored[key]}

    monkeypatch.setattr(catalogue, "_task_create", create)
    launch = MagicMock(
        return_value=(
            {
                "success": True,
                "queue_item": {
                    "status": "queued",
                    "queue_id": "queue",
                    "prompt_raw": "Private source text",
                },
            },
            201,
        )
    )
    monkeypatch.setattr(
        task_execution_submission_service, "submit_task_execution", launch
    )
    kwargs = dict(
        mention="Scone",
        question="How does Scone represent context?",
        relevant_context="Planning a seminar about symbolic systems.",
        source_prompt="We discussed Scone yesterday.",
        originating_session_id="conversation",
        request_id="turn-1",
        namespace="#V#alice@lab",
    )
    return kwargs, stored, launch, state


def test_submission_returns_pending_without_waiting_and_retains_exact_source(inquiry):
    args, stored, launch, _ = inquiry
    result = rumination.start_inquiry(**args)
    assert result["success"] and result["status"] == "queued"
    assert "Private source text" not in json.dumps(result)
    assert launch.call_args.kwargs["independent"] is True
    assert launch.call_args.kwargs["actor_context"]["actor_concept_id"] == "#V#alice"
    source = json.loads(next(iter(stored.values()))["notes"])
    assert source == {
        "session_id": "conversation",
        "turn_id": "turn-1",
        "mention": args["mention"],
        "question": args["question"],
        "relevant_context": args["relevant_context"],
    }


def test_retry_reuses_task_and_launch_without_replacing_corrected_original(inquiry):
    args, stored, launch, _ = inquiry
    rumination.start_inquiry(**args)
    first = launch.call_args
    rumination.start_inquiry(
        **{**args, "question": "Changed wording on transport retry"}
    )
    assert len(stored) == 1
    assert launch.call_args == first
    assert (
        json.loads(next(iter(stored.values()))["notes"])["question"] == args["question"]
    )


def test_completed_retry_does_not_launch_or_erase_product(inquiry):
    args, stored, launch, _ = inquiry
    rumination.start_inquiry(**args)
    task = next(iter(stored.values()))
    task.update(status="completed", current_work_product={"concept_id": "#V#product"})
    launch.reset_mock()
    result = rumination.start_inquiry(**args)
    launch.assert_not_called()
    assert result["current_work_product"]["concept_id"] == "#V#product"


def test_unverified_mention_does_not_create_task(inquiry):
    args, stored, launch, _ = inquiry
    assert (
        rumination.start_inquiry(**{**args, "mention": "Another person"})["error_code"]
        == "mention_not_in_source_turn"
    )
    assert not stored
    launch.assert_not_called()


def test_background_task_cannot_recursively_launch_inquiries(inquiry):
    args, stored, launch, state = inquiry
    state["origin_kind"] = "background_task"
    assert (
        rumination.start_inquiry(**args)["error_code"]
        == "nested_background_inquiry_not_supported"
    )
    assert not stored
    launch.assert_not_called()


def test_actor_scope_denial_has_no_task_effect(inquiry, monkeypatch):
    args, stored, launch, _ = inquiry
    monkeypatch.setattr(
        catalogue,
        "_resolve_task_actor_scope",
        lambda *a, **k: (None, {"success": False, "error_code": "actor_scope_denied"}),
    )
    assert rumination.start_inquiry(**args)["error_code"] == "actor_scope_denied"
    assert not stored
    launch.assert_not_called()


def test_product_projection_is_bounded_and_does_not_write_source_state(monkeypatch):
    from src.backend.services import task_management_service as tasks
    from src.backend.services import task_work_product_service as products
    from src.backend.services import conversation_concept_service as conversations
    from src.backend.db.repositories.concepts_repository import ConceptsRepository

    monkeypatch.setattr(
        conversations,
        "get_conversation_concept_by_session_id",
        lambda _: "#V#conversation",
    )
    cursor = MagicMock()
    cursor.sort.return_value.limit.return_value = [{"concept_id": "#V#task"}] * 4
    find = MagicMock(return_value=cursor)
    monkeypatch.setattr(ConceptsRepository, "find", find)
    monkeypatch.setattr(
        tasks,
        "get_task",
        lambda _: {
            "task_concept_id": "#V#task",
            "created_by_concept_id": "#V#alice",
            "organisation_concept_id": "#V#lab",
            "notes": "original source",
            "status": "completed",
        },
    )
    monkeypatch.setattr(tasks, "_get_task_doc", lambda _: ("#V#task", {}))
    monkeypatch.setattr(
        products,
        "resolve_task_work_product",
        lambda *a, **k: {
            "status": "ready",
            "concept_id": "#V#product",
            "content": "x" * 6000,
            "content_sha256": "full-body-hash",
        },
    )
    result = rumination.project_inquiries(
        actor="#V#alice",
        organisation="#V#lab",
        namespace="#V#alice@lab",
        session_id="conversation",
    )
    assert len(result["items"]) == 3 and result["omitted_older"]
    cursor.sort.return_value.limit.assert_called_once_with(4)
    query = find.call_args.args[0]
    assert query["metadata.organisation_concept_id"] == "#V#lab"
    assert query[f"relationships.{tasks.PREDICATE_HAS_CREATED_BY}"] == "#V#alice"
    product = result["items"][0]["product"]
    assert len(product["content"]) == 5000 and product["content_truncated"]
    assert product["content_sha256"] == "full-body-hash"
    assert result["items"][0]["source"] == "original source"


def test_capability_provenance_comes_from_trusted_turn():
    from src.backend.integrations.internal_mcp.conversational_rumination_tools import (
        definitions,
    )

    definition = definitions()[0]
    bindings = definition.ordinary_turn_trusted_argument_bindings
    assert bindings["source_prompt"] == "turn_prompt"
    assert bindings["request_id"] == "turn_id"
    assert bindings["originating_session_id"] == "conversation_id"
    assert bindings["acting_user_concept_id"] == "actor_user_concept_id"
