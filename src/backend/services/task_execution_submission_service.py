"""Canonical task launch shared by Tasks and durable schedule occurrences.

Callers supply an already authenticated actor context. Task/conversation
ownership, active-execution deduplication and the queue outbox are checked here;
task descriptions and changing work products remain canonical task references.
An accepted launch is not a claim that the task's work has succeeded.
"""

import json
import logging
from typing import Any

from . import chat_history_service, chat_prompt_queue_service
from .chat_prompt_queue_dispatch_service import wake_chat_prompt_queue_dispatcher
from .conversation_concept_service import get_conversation_concept
from .conversation_turn_admission_service import build_conversation_key
from .namespace_service import resolve_canonical_namespace
from .task_execution_service import (
    VON_SYSTEM_CONCEPT_ID,
    InvalidTaskExecutionData,
    TaskExecutionAccessError,
    reconcile_task_execution_queue_record,
    task_execution_concept_id_for_launch,
    task_execution_enqueue_submission_id_for_launch,
)
from .task_management_service import get_task

logger = logging.getLogger(__name__)


def _resolve_task_execution_conversation(
    *,
    task: dict[str, Any],
    actor_context: dict[str, str],
) -> dict[str, Any]:
    """Read back the task's actor-owned originating conversation."""

    task_org_id = task.get("organisation_concept_id")
    if task_org_id != actor_context["organisation_concept_id"]:
        raise TaskExecutionAccessError(
            "task_organisation_mismatch",
            "The task does not belong to the active organisation",
        )
    if task.get("assignee_concept_id") != VON_SYSTEM_CONCEPT_ID:
        raise TaskExecutionAccessError(
            "task_not_assigned_to_von",
            "The task must be assigned to Von before it can be executed",
        )
    if task.get("created_by_concept_id") != actor_context["actor_concept_id"]:
        raise TaskExecutionAccessError(
            "task_creator_mismatch",
            "Only the task creator can authorise Von to execute this task",
        )
    if str(task.get("status") or "").strip().lower() not in {
        "pending",
        "in_progress",
    }:
        raise TaskExecutionAccessError(
            "task_not_executable",
            "Only a pending or in-progress task can be executed",
        )
    conversation_id = task.get("originating_conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise TaskExecutionAccessError(
            "originating_conversation_required",
            "The task must be associated with an originating conversation",
        )
    conversation = get_conversation_concept(conversation_id.strip())
    if not isinstance(conversation, dict):
        raise TaskExecutionAccessError(
            "originating_conversation_unavailable",
            "The originating conversation is not accessible",
        )
    actor_id = actor_context["actor_concept_id"]
    org_id = actor_context["organisation_concept_id"]
    namespace = actor_context["namespace"]
    if conversation.get("owner_concept_id") != actor_id:
        raise TaskExecutionAccessError(
            "originating_conversation_owner_mismatch",
            "The originating conversation belongs to a different actor",
        )
    conversation_org_id = conversation.get("organisation_concept_id")
    if conversation_org_id != org_id:
        raise TaskExecutionAccessError(
            "originating_conversation_organisation_mismatch",
            "The originating conversation belongs to a different organisation",
        )
    conversation_namespace = conversation.get("namespace")
    if conversation_namespace:
        canonical_conversation_namespace = resolve_canonical_namespace(
            conversation_namespace,
            actor_id,
            org_id,
        )
        if canonical_conversation_namespace != namespace:
            raise TaskExecutionAccessError(
                "originating_conversation_namespace_mismatch",
                "The originating conversation belongs to a different namespace",
            )
    session_id = conversation.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise TaskExecutionAccessError(
            "originating_conversation_session_required",
            "The originating conversation has no session identifier",
        )
    session_id = session_id.strip()
    task_session_id = task.get("conversation_session_id")
    if task_session_id and task_session_id != session_id:
        raise TaskExecutionAccessError(
            "originating_conversation_session_mismatch",
            "The task and originating conversation session identifiers differ",
        )
    if not chat_history_service.has_chat_history_session(
        actor_id,
        session_id,
        namespace=namespace,
        include_legacy=False,
    ):
        raise TaskExecutionAccessError(
            "originating_conversation_history_unavailable",
            "The originating conversation history is not available in this scope",
        )
    resolved = dict(task)
    resolved["originating_conversation_id"] = conversation_id.strip()
    resolved["conversation_session_id"] = session_id
    resolved["conversation_name"] = (
        conversation.get("name")
        or conversation.get("topic")
        or task.get("conversation_name")
    )
    return resolved


def _build_task_execution_prompt(
    task: dict[str, Any], *, task_execution_concept_id: str | None = None
) -> str:
    """Project the selected task and canonical references, not a shadow product."""
    product = task.get("current_work_product") or {"status": "missing"}
    instruction = task.get("continuation_instruction", "")
    parts = [
        "Execute the task assigned to Von.",
        f"Task concept ID: {task.get('task_concept_id', '')}",
        f"Title: {task.get('title') or 'Untitled task'}",
    ]
    if task_execution_concept_id:
        parts.append(
            f"Current execution (this turn): {task_execution_concept_id}. "
            "Task submission has already succeeded. An in-progress observation "
            "of this execution refers to your own work in this turn, not another "
            "worker to wait for."
        )
    if instruction:
        parts.append(f"New user instruction: {instruction}")
    parts.extend(
        [
            f"Current work product: {json.dumps(product, ensure_ascii=False)}",
            f"Next checkpoint / unresolved questions: {task.get('next_checkpoint') or ''}",
            f"Evidence: {task.get('evidence') or ''}",
            f"Notes: {task.get('notes') or ''}",
            f"Description: {task.get('description') or ''}",
        ]
    )
    return "\n".join(parts)[: chat_prompt_queue_service.MAX_PROMPT_RAW_CHARS]


def _enqueue_task_execution(
    *,
    scope: dict[str, str],
    task: dict[str, Any],
    task_execution_concept_id: str,
    enqueue_submission_id: str,
    execution_envelope: dict[str, Any],
    conversation_key: str,
) -> dict[str, Any]:
    """Narrow adapter around the durable server-dispatch queue contract."""

    return chat_prompt_queue_service.create_queue_record(
        scope=scope,
        prompt_raw=_build_task_execution_prompt(
            task, task_execution_concept_id=task_execution_concept_id
        ),
        session_id=task["conversation_session_id"],
        session_name=task.get("conversation_name"),
        status=chat_prompt_queue_service.STATUS_QUEUED,
        source="von_task",
        conversation_key=conversation_key,
        enqueue_submission_id=enqueue_submission_id,
        dispatch_mode=chat_prompt_queue_service.DISPATCH_MODE_SERVER,
        execution_envelope_version=1,
        execution_envelope=execution_envelope,
        task_concept_id=task["task_concept_id"],
        task_execution_concept_id=task_execution_concept_id,
        server_dispatch_ready=False,
    )


def submit_task_execution(
    task_concept_id: str,
    *,
    actor_context: dict[str, str],
    payload: dict[str, Any],
    model: str | None = None,
    model_provider: str | None = None,
    model_parameters: dict[str, Any] | None = None,
    source_workflow_instance_id: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Submit/reconcile one launch; keep its outbox on an uncertain receipt."""

    task_execution: dict[str, Any] | None = None
    queue_record: dict[str, Any] | None = None
    task: dict[str, Any] | None = None
    try:
        # A task point read is not sufficient authority by itself. Bind it to
        # the trusted creator, active organisation, and actor-owned originating
        # conversation instead of accepting any request payload scope.
        task = _resolve_task_execution_conversation(
            task=dict(get_task(task_concept_id)),
            actor_context=actor_context,
        )
        payload = payload if isinstance(payload, dict) else {}
        instruction = payload.get("continuation_instruction", "")
        if not isinstance(instruction, str) or len(instruction) > 4000:
            raise InvalidTaskExecutionData(
                "continuation_instruction must be text of at most 4000 characters"
            )
        task["continuation_instruction"] = instruction.strip()
        requested_session_id = payload.get("originating_session_id")
        if requested_session_id is not None and requested_session_id != task.get(
            "conversation_session_id"
        ):
            raise TaskExecutionAccessError(
                "originating_conversation_session_mismatch",
                "The requested conversation does not match the task",
            )
        launch_request_id = payload.get("launch_request_id")
        if launch_request_id is None:
            raise InvalidTaskExecutionData("launch_request_id is required")
        if not isinstance(launch_request_id, str) or not launch_request_id.strip():
            raise InvalidTaskExecutionData("launch_request_id must be a string")
        launch_request_id = launch_request_id.strip()
        if len(launch_request_id) > 200:
            raise InvalidTaskExecutionData("launch_request_id is too long")

        execution_concept_id = task_execution_concept_id_for_launch(
            task_concept_id=task["task_concept_id"],
            creator_concept_id=actor_context["actor_concept_id"],
            organisation_concept_id=actor_context["organisation_concept_id"],
            launch_request_id=launch_request_id,
        )
        enqueue_submission_id = task_execution_enqueue_submission_id_for_launch(
            task_concept_id=task["task_concept_id"],
            creator_concept_id=actor_context["actor_concept_id"],
            organisation_concept_id=actor_context["organisation_concept_id"],
            launch_request_id=launch_request_id,
        )
        execution_envelope = {
            "initiation_id": launch_request_id,
            "turn_kind": "user_message",
            "workflow_inputs": {
                "task_concept_id": task["task_concept_id"],
                "task_execution_concept_id": execution_concept_id,
                "originating_conversation_concept_id": task[
                    "originating_conversation_id"
                ],
                "conversation_session_id": task["conversation_session_id"],
                "authority_actor_concept_id": actor_context["actor_concept_id"],
                "authority_organisation_concept_id": actor_context[
                    "organisation_concept_id"
                ],
                "authority_namespace": actor_context["namespace"],
                "executor_concept_id": VON_SYSTEM_CONCEPT_ID,
            },
        }
        if model:
            from ..languagemodels.llm_interface import assert_model_execution_allowed

            assert_model_execution_allowed(
                provider=model_provider,
                model=model,
                user_concept_id=actor_context["actor_concept_id"],
                org_concept_id=actor_context["organisation_concept_id"],
            )
            execution_envelope["model"] = model
            execution_envelope["model_provider"] = model_provider
        if model_parameters:
            execution_envelope["model_parameters"] = dict(model_parameters)
        if source_workflow_instance_id:
            execution_envelope["workflow_inputs"][
                "source_workflow_instance_id"
            ] = source_workflow_instance_id
        scope = {
            "user_concept_id": actor_context["actor_concept_id"],
            "organisation_concept_id": actor_context["organisation_concept_id"],
            "namespace": actor_context["namespace"],
        }
        conversation_key = build_conversation_key(
            owner_user_id=actor_context["actor_concept_id"],
            history_namespace=actor_context["namespace"],
            conversation_session_id=task["conversation_session_id"],
        )
        queue_record = _enqueue_task_execution(
            scope=scope,
            task=task,
            task_execution_concept_id=execution_concept_id,
            enqueue_submission_id=enqueue_submission_id,
            execution_envelope=execution_envelope,
            conversation_key=conversation_key,
        )
        queue_replayed = bool(queue_record.get("idempotent_replay"))
        task_execution = reconcile_task_execution_queue_record(
            {
                **queue_record,
                **scope,
                "execution_envelope": execution_envelope,
            }
        )
        if queue_record.get(
            "status"
        ) == chat_prompt_queue_service.STATUS_QUEUED and not queue_record.get(
            "dispatch_ready"
        ):
            queue_record = chat_prompt_queue_service.activate_server_dispatch_record(
                scope=scope,
                queue_id=str(queue_record["queue_id"]),
                enqueue_submission_id=enqueue_submission_id,
                task_execution_concept_id=execution_concept_id,
            )
            queue_record["idempotent_replay"] = queue_replayed
        wake_chat_prompt_queue_dispatcher()
        return (
            {
                "success": True,
                "task": {
                    "task_concept_id": task["task_concept_id"],
                    "title": task.get("title"),
                    "originating_conversation_id": task["originating_conversation_id"],
                    "conversation_session_id": task["conversation_session_id"],
                    "conversation_name": task.get("conversation_name"),
                },
                "task_execution": task_execution,
                "queue_item": queue_record,
                "idempotent_replay": queue_replayed,
            }
        ), (200 if queue_replayed else 201)
    except Exception as exc:
        # Once the unready queue outbox exists, do not delete it on an unknown
        # projection acknowledgement. The server dispatcher owns durable
        # reconciliation and the exact client launch ID makes retries safe.
        if (
            queue_record
            and actor_context
            and task
            and queue_record.get("status") == chat_prompt_queue_service.STATUS_QUEUED
            and not queue_record.get("dispatch_ready")
        ):
            logger.warning(
                "Task launch accepted for durable reconciliation queue_id=%s: %s",
                queue_record.get("queue_id"),
                exc,
            )
            wake_chat_prompt_queue_dispatcher()
            return (
                {
                    "success": True,
                    "reconciliation_pending": True,
                    "warning": "Task execution projection is being reconciled",
                    "task": {
                        "task_concept_id": task["task_concept_id"],
                        "title": task.get("title"),
                        "originating_conversation_id": task[
                            "originating_conversation_id"
                        ],
                        "conversation_session_id": task["conversation_session_id"],
                        "conversation_name": task.get("conversation_name"),
                    },
                    "task_execution": None,
                    "queue_item": queue_record,
                    "idempotent_replay": bool(queue_record.get("idempotent_replay")),
                }
            ), 202
        raise
