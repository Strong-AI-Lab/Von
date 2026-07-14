"""Durable workflow execution system with checkpoint/resume and scheduling.

This package extends the core workflow engine with:
- Persistent workflow instances in MongoDB
- Checkpoint-before-action for safe interruption
- Automatic resume of interrupted workflows
- Scheduled/cron workflow triggers
- Distributed worker support with lock-based coordination

Primary use cases:
- RAG text relation sync (long-running batch operations)
- Rumination workflows (proactive knowledge enrichment)
- Scheduled report generation
- Multi-step research workflows

See docs/engineering/durable_workflow_system_design.md for full specification.
"""

from .models import (
    WorkflowInstance,
    WorkflowSchedule,
    WorkflowInstanceStatus,
    ScheduleType,
)
from .instance_manager import WorkflowInstanceManager
from .durable_executor import (
    DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY,
    DurableWorkflowExecutor,
    DurableWorkflowResult,
)
from .worker import DurableWorkflowWorker, AsyncDurableWorkflowWorker
from .scheduler import WorkflowScheduler, AsyncWorkflowScheduler
from .startup import (
    get_instance_manager,
    create_durable_executor,
    create_worker,
    create_scheduler,
    start_worker_and_scheduler,
    stop_worker_and_scheduler,
    recover_orphaned_instances,
    get_system_status,
)

__all__ = [
    # Models
    "WorkflowInstance",
    "WorkflowSchedule",
    "WorkflowInstanceStatus",
    "ScheduleType",
    # Manager
    "WorkflowInstanceManager",
    # Executor
    "DurableWorkflowExecutor",
    "DurableWorkflowResult",
    "DURABLE_EXECUTED_WORKFLOW_DEFINITION_IDENTITY_KEY",
    # Worker
    "DurableWorkflowWorker",
    "AsyncDurableWorkflowWorker",
    # Scheduler
    "WorkflowScheduler",
    "AsyncWorkflowScheduler",
    # Startup
    "get_instance_manager",
    "create_durable_executor",
    "create_worker",
    "create_scheduler",
    "start_worker_and_scheduler",
    "stop_worker_and_scheduler",
    "recover_orphaned_instances",
    "get_system_status",
]
