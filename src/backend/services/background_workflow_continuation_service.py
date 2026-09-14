"""Durable workflow-to-conversation handoff using the existing prompt queue.

The model chooses remaining work. A held queue row persists that choice before
a continuation promise is exposed, survives restarts, and uses normal queue
cancellation, FIFO, idempotency and current dispatch authorisation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from pymongo import ReturnDocument

from . import chat_prompt_queue_service as queue


def _instance_matches(instance, *, scope, request_id, workflow_id):
    return bool(
        instance
        and instance.user_id == scope["user_concept_id"]
        and instance.org_id == scope.get("organisation_concept_id")
        and instance.namespace == scope["namespace"]
        and instance.source_event_type == "conversation_turn"
        and instance.source_event_id == request_id
        and instance.workflow_id == workflow_id
    )


def register_continuation(
    *,
    scope: Mapping[str, Any],
    request_id: str,
    instance_id: str,
    workflow_id: str,
    continuation_prompt: str,
    execution_envelope: Mapping[str, Any],
    ready_when: str = "workflow_terminal",
) -> dict[str, Any]:
    """Issue one held successor for the exact admitted source turn.

    Neither a model-supplied source locator nor workflow inputs grant authority:
    both the queue attempt and durable workflow must match the server's scope.
    Registration is internal to the represented-workflow adapter.
    """
    from ..workflows.durable import WorkflowInstanceManager

    coll = queue._collection()
    source = coll.find_one(
        {
            **queue._scope_query(scope),
            "client_request_id": request_id,
            "status": queue.STATUS_IN_PROGRESS,
            "cancellation_requested_at": {"$exists": False},
        }
    )
    if not source or not source.get("session_id") or not source.get("conversation_key"):
        return {"persisted": False, "reason": "active_source_turn_unavailable"}
    if source.get("task_execution_concept_id"):
        return {
            "persisted": False,
            "reason": "task_execution_requires_task_continuation",
        }
    instance = WorkflowInstanceManager().get_instance(instance_id)
    if not _instance_matches(
        instance, scope=scope, request_id=request_id, workflow_id=workflow_id
    ):
        return {"persisted": False, "reason": "workflow_source_scope_mismatch"}
    if (
        not isinstance(continuation_prompt, str)
        or not continuation_prompt.strip()
        or len(continuation_prompt) > 8000
    ):
        return {"persisted": False, "reason": "invalid_remaining_work"}
    if ready_when not in {"workflow_terminal", "source_text_available"}:
        return {"persisted": False, "reason": "invalid_continuation_condition"}
    if ready_when == "source_text_available" and not (
        instance.inputs.get("file_copy_concept_id")
        or instance.inputs.get("file_copy_concept_ids")
    ):
        return {"persisted": False, "reason": "source_file_reference_required"}

    submission_id = "workflow-continuation:" + source["queue_id"]
    envelope = dict(source.get("execution_envelope") or execution_envelope)
    # A continuation is a new turn, not a replay of browser/task initiation.
    envelope.pop("initiation_id", None)
    existing = coll.find_one(
        {**queue._scope_query(scope), "enqueue_submission_id": submission_id}
    )
    dependency = {
        "instance_id": instance_id,
        "workflow_id": workflow_id,
        "remaining_work": continuation_prompt.strip(),
        "ready_when": ready_when,
    }
    if not existing:
        created = queue.create_queue_record(
            scope=scope,
            prompt_raw=source["prompt_raw"],
            session_id=source["session_id"],
            session_name=source.get("session_name"),
            conversation_key=source["conversation_key"],
            source="workflow_continuation",
            dispatch_mode=queue.DISPATCH_MODE_SERVER,
            execution_envelope_version=1,
            execution_envelope=envelope,
            enqueue_submission_id=submission_id,
            server_dispatch_ready=False,
            workflow_continuation={
                "source_queue_id": source["queue_id"],
                "source_request_id": request_id,
                "dependencies": [dependency],
                "status": "waiting",
                "next_check_at": queue._now(),
            },
        )
        existing = coll.find_one({"queue_id": created["queue_id"]})
    if (
        not existing
        or existing.get("status") != queue.STATUS_QUEUED
        or existing.get("dispatch_ready")
    ):
        return {
            "persisted": False,
            "reason": "continuation_already_dispatched_or_cancelled",
        }
    existing_binding = existing.get("workflow_continuation") or {}
    if (
        existing_binding.get("source_queue_id") != source["queue_id"]
        or existing_binding.get("source_request_id") != request_id
        or existing_binding.get("status") != "waiting"
    ):
        return {"persisted": False, "reason": "continuation_binding_conflict"}
    updated = coll.update_one(
        {
            "queue_id": existing["queue_id"],
            "status": queue.STATUS_QUEUED,
            "dispatch_ready": False,
        },
        {"$addToSet": {"workflow_continuation.dependencies": dependency}},
    )
    return {
        "persisted": bool(updated.matched_count),
        "queue_id": existing["queue_id"],
        "status": "waiting",
        "source_request_id": request_id,
    }


def validate_continuation_dispatch(record: Mapping[str, Any]) -> None:
    """Recheck cancellation and source binding after a held row becomes ready."""
    binding = record.get("workflow_continuation")
    if not isinstance(binding, Mapping):
        return
    scope = queue.build_queue_scope(
        user_concept_id=record["user_concept_id"],
        organisation_concept_id=record.get("organisation_concept_id"),
        namespace=record["namespace"],
    )
    source = queue._collection().find_one(
        {
            **queue._scope_query(scope),
            "queue_id": binding["source_queue_id"],
            "client_request_id": binding["source_request_id"],
            "status": queue.STATUS_COMPLETED,
            "cancellation_requested_at": {"$exists": False},
            "conversation_key": record["conversation_key"],
            "session_id": record["session_id"],
        }
    )
    if not source:
        raise ValueError(
            "workflow continuation source was cancelled or is no longer bound"
        )


def reconcile_one_continuation() -> bool:
    """Check one held row. No in-memory callback is required for recovery."""
    from ..workflows.durable import WorkflowInstanceManager

    coll = queue._collection()
    now = queue._now()
    # Throttle pending checks and rotate fairly; competing reconcilers are safe
    # because only one compare-and-set can release this existing queue row.
    record = coll.find_one_and_update(
        {
            "status": queue.STATUS_QUEUED,
            "dispatch_ready": False,
            "workflow_continuation.status": "waiting",
            "workflow_continuation.next_check_at": {"$lte": now},
        },
        {"$set": {"workflow_continuation.next_check_at": now + timedelta(seconds=5)}},
        sort=[("workflow_continuation.next_check_at", 1), ("queue_id", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not record:
        return False
    scope = queue.build_queue_scope(
        user_concept_id=record["user_concept_id"],
        organisation_concept_id=record.get("organisation_concept_id"),
        namespace=record["namespace"],
    )
    binding = record["workflow_continuation"]
    source = coll.find_one(
        {
            **queue._scope_query(scope),
            "queue_id": binding["source_queue_id"],
            "client_request_id": binding["source_request_id"],
        }
    )
    try:
        if (
            not source
            or source.get("cancellation_requested_at")
            or source["status"] in {queue.STATUS_FAILED, queue.STATUS_CANCELLED}
        ):
            queue.cancel_prompt_record(scope=scope, queue_id=record["queue_id"])
            return True
        if source["status"] != queue.STATUS_COMPLETED:
            return False
        from .turn_execution_record_service import get_turn_execution_record_projection

        prior_turn = get_turn_execution_record_projection(
            request_id=binding["source_request_id"],
            namespace=scope["namespace"],
        )
        if not prior_turn:
            raise RuntimeError("source_turn_effect_readback_unavailable")
        if (
            prior_turn.get("user_id") != scope["user_concept_id"]
            or prior_turn.get("namespace") != scope["namespace"]
        ):
            raise ValueError("source_turn_effect_scope_mismatch")
        prior_effects = {
            key: prior_turn.get(key)
            for key in (
                "request_id",
                "effect_observation_journal",
                "required_effects",
                "terminal_outcome_receipt",
                "completion_report",
            )
            if prior_turn.get(key) is not None
        }
        if len(json.dumps(prior_effects, default=str)) > 16_000:
            prior_effects = {
                "request_id": binding["source_request_id"],
                "details": "read_exact_turn_execution_record",
                "truncated": True,
            }
        manager = WorkflowInstanceManager()
        observations = []
        source_ids: list[str] = []
        output_budget = 24_000
        for dependency in binding["dependencies"]:
            instance = manager.get_instance(dependency["instance_id"])
            if not _instance_matches(
                instance,
                scope=scope,
                request_id=binding["source_request_id"],
                workflow_id=dependency["workflow_id"],
            ):
                raise ValueError("workflow_source_scope_mismatch")
            status = getattr(instance.status, "value", instance.status)
            if status == "cancelled":
                queue.cancel_prompt_record(scope=scope, queue_id=record["queue_id"])
                return True
            sources = []
            if status not in {"completed", "failed"}:
                if dependency.get("ready_when") != "source_text_available":
                    return False
                from .workflow_file_source_service import project_workflow_file_sources

                sources = project_workflow_file_sources(
                    instance.inputs,
                    user_concept_id=scope["user_concept_id"],
                    organisation_concept_id=scope.get("organisation_concept_id"),
                    namespace=scope["namespace"],
                )
                if not sources or any(
                    item.get("status") != "available" for item in sources
                ):
                    return False
            # Retain original source references. The next turn's actor-scoped
            # file projection can reuse extraction without running indexing.
            file_ids = [instance.inputs.get("file_copy_concept_id")]
            if isinstance(instance.inputs.get("file_copy_concept_ids"), list):
                file_ids.extend(instance.inputs["file_copy_concept_ids"])
            for file_id in file_ids:
                if isinstance(file_id, str) and file_id not in source_ids:
                    source_ids.append(file_id)
            outputs = instance.outputs
            output_size = len(json.dumps(outputs, default=str))
            if output_size > output_budget:
                outputs = {"details": "read_exact_workflow_instance", "truncated": True}
            else:
                output_budget -= output_size
            observations.append(
                {
                    **dependency,
                    "status": status,
                    "outputs": outputs,
                    "source_documents": sources,
                    "error": instance.error,
                    "error_step": instance.error_step,
                    "domain_postconditions": "require_canonical_readback",
                }
            )
        envelope = dict(record["execution_envelope"])
        launch_inputs = {"file_copy_concept_ids": source_ids} if source_ids else {}
        if len(source_ids) == 1:
            launch_inputs["file_copy_concept_id"] = source_ids[0]
        envelope["workflow_inputs"] = {
            **dict(envelope.get("workflow_inputs") or {}),
            **launch_inputs,
            "background_workflow_continuation": {
                "source_request_id": binding["source_request_id"],
                "source_queue_id": binding["source_queue_id"],
                "intent": "reconcile_existing_effects_then_complete_remaining_authorised_work",
                "observations": observations,
                "prior_turn_effects": prior_effects,
            },
        }
        envelope = queue._normalise_execution_envelope(envelope)
        result = coll.update_one(
            {
                "queue_id": record["queue_id"],
                "status": queue.STATUS_QUEUED,
                "dispatch_ready": False,
                "workflow_continuation.dependencies": binding["dependencies"],
            },
            {
                "$set": {
                    "execution_envelope": envelope,
                    "dispatch_ready": True,
                    "workflow_continuation.status": "ready",
                    "updated_at": now,
                }
            },
        )
        return bool(result.modified_count)
    except ValueError as exc:
        return queue.fail_held_workflow_continuation(
            scope=scope,
            queue_id=record["queue_id"],
            source_request_id=binding["source_request_id"],
            error=f"Workflow continuation cannot resume: {exc}",
        )
    except Exception as exc:
        # Keep the cancellable row and exact dependencies for retry. Never
        # replace a failed read with an assumed completion or a new effect.
        coll.update_one(
            {"queue_id": record["queue_id"]},
            {
                "$set": {
                    "workflow_continuation.last_error": type(exc).__name__
                    + ": "
                    + str(exc)[:500]
                }
            },
        )
        return False
