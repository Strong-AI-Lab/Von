"""Resume a canonical task through the same queue used by the Tasks UI.

This action submits work. Its receipt says nothing about domain completion;
the linked TaskExecution and conversation retain progress and outcome evidence.
"""

from ...services.chat_prompt_queue_service import ChatPromptQueueTaskAlreadyActive
from ...services.task_execution_submission_service import submit_task_execution
from ...services.workflow_actor_scope_service import (
    resolve_authoritative_workflow_actor_scope,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)

TASK_SUBMIT_EXECUTION_ACTION_ID = "task.submit_execution"


def submit_task_execution_action(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    try:
        # The durable worker installs the actor persisted on the instance.
        # Workflow inputs cannot replace that actor or choose another owner.
        scope = resolve_authoritative_workflow_actor_scope(allow_unscoped_claims=False)
        if not scope.user_concept_id or not scope.organisation_concept_id:
            raise ValueError("task_execution_actor_and_organisation_required")
        instance_id = getattr(request.trace, "instance_id", None)
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("task_execution_durable_instance_required")
        task_id = request.inputs.get("task_concept_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_concept_id_required")
        model = request.inputs.get("model")
        provider = request.inputs.get("model_provider")
        if (
            not isinstance(model, str)
            or not model.strip()
            or not isinstance(provider, str)
            or not provider.strip()
        ):
            raise ValueError("task_execution_explicit_model_and_provider_required")
        parameters = request.inputs.get("model_parameters")
        if parameters is not None and not isinstance(parameters, dict):
            raise ValueError("task_execution_model_parameters_invalid")
        result, status = submit_task_execution(
            task_id.strip(),
            actor_context={
                "actor_concept_id": scope.user_concept_id,
                "organisation_concept_id": scope.organisation_concept_id,
                "namespace": scope.namespace,
            },
            payload={
                "launch_request_id": f"workflow-task:{instance_id}",
                "continuation_instruction": request.inputs.get(
                    "continuation_instruction"
                )
                or "",
            },
            model=model.strip(),
            model_provider=provider.strip(),
            model_parameters=parameters,
            source_workflow_instance_id=instance_id,
        )
        queue = result["queue_item"]
        return WorkflowActionResult(
            status="success",
            outputs={
                "task_launch_status": "accepted",
                "task_concept_id": task_id.strip(),
                "queue_id": queue["queue_id"],
                "task_execution_concept_id": queue.get("task_execution_concept_id"),
                "conversation_session_id": queue.get("session_id"),
                "queue_status": queue.get("status"),
                "idempotent_replay": bool(result.get("idempotent_replay")),
                "reconciliation_pending": status == 202,
                "domain_completion_claim": False,
            },
        )
    except ChatPromptQueueTaskAlreadyActive as exc:
        # UI and schedule launches meet at the queue's atomic task key. A
        # later occurrence coalesces here even after this launch workflow ends.
        return WorkflowActionResult(
            status="success",
            outputs={
                "task_launch_status": "already_active",
                "queue_id": exc.queue_id,
                "task_execution_concept_id": exc.task_execution_concept_id,
                "domain_completion_claim": False,
            },
        )
    except Exception as exc:  # noqa: BLE001 - preserve the durable action failure receipt
        return WorkflowActionResult(status="failed", error=str(exc))


def register_task_execution_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=TASK_SUBMIT_EXECUTION_ACTION_ID,
            handler=submit_task_execution_action,
            description="Submit one actor-owned task continuation through the canonical Tasks queue; coalesce an already active execution.",
        )
    )
