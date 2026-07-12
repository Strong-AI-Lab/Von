# Durable Workflow System Design

> **Document status: Historical design precursor; non-normative.** Current-state
> and gap descriptions reflect February 2026 and have been overtaken by later
> durable-workflow implementation. Use the current VWL manual and live
> code/Vontology state for workflow planning.

**Lifecycle**: Superseded design precursor
**Authority**: Historical only
**Version**: 1.0
**Date**: 2026-02-03
**Parent JIRA**: [JVNAUTOSCI-803](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-803) (LLM Workflows)
**Authors**: Von AI Assistant (GitHub Copilot), Michael Witbrock

---

## Executive Summary

This document extends the [LLM Workflows Design](llm_workflows_design.md) with **durable workflow execution** — the ability to persist workflow state, resume interrupted workflows, and schedule workflows for deferred/background execution.

**Key Capabilities**:
1. **Checkpoint/Resume**: Long-running workflows can be interrupted (server restart, timeout) and resumed from last checkpoint
2. **Background Execution**: Workflows run asynchronously, detached from user sessions
3. **Scheduling**: Workflows can be triggered at specified times or intervals
4. **Observability**: Live status queries for in-progress workflows

**Primary Use Cases**:
- RAG text relation sync (long-running batch operations)
- Rumination workflows (proactive knowledge enrichment)
- Scheduled report generation
- Multi-step research workflows

---

## Table of Contents

1. [Background & Motivation](#1-background--motivation)
2. [Current State Analysis](#2-current-state-analysis)
3. [Design Principles](#3-design-principles)
4. [Architecture Overview](#4-architecture-overview)
5. [Workflow Instance Model](#5-workflow-instance-model)
6. [Checkpoint & Resume](#6-checkpoint--resume)
7. [Scheduling System](#7-scheduling-system)
8. [Worker Architecture](#8-worker-architecture)
9. [API Design](#9-api-design)
10. [Vontology Integration](#10-vontology-integration)
11. [Migration Path](#11-migration-path)
12. [Implementation Plan](#12-implementation-plan)
13. [Testing Strategy](#13-testing-strategy)
14. [Operational Concerns](#14-operational-concerns)

---

## 1. Background & Motivation

### 1.1 Problem Statement

The current workflow system (per [llm_workflows_design.md](llm_workflows_design.md)) has limitations for long-running operations:

| Gap | Impact |
|-----|--------|
| **No persistence** | Server restart loses all in-progress workflows |
| **Synchronous execution** | Long workflows block conversation turns |
| **No scheduling** | No way to trigger workflows at specific times |
| **No live status** | Cannot query progress of background work |
| **Single-process** | Cannot distribute work across workers |

### 1.2 Motivating Use Cases

#### RAG Text Relation Sync (JVNAUTOSCI-1073 context)
- Syncing thousands of text relations to RAG store
- Can take 10+ minutes for large namespaces
- Currently blocks conversation turns
- Need: Background execution with progress tracking

#### Rumination Workflows (JVNAUTOSCI-923)
- Proactive knowledge enrichment cycles
- Iterates over concepts, identifies gaps, queries LLM, updates graph
- Runs during idle periods, not tied to user requests
- Need: Scheduled execution, checkpoint on interrupt

#### Research Report Generation
- Multi-step: gather sources → summarise → synthesise → format
- May take 30+ minutes for comprehensive reports
- User checks back later for results
- Need: Background with notification on completion

### 1.3 Design Goals

1. **Zero-loss durability**: No workflow progress lost on restart
2. **Transparent resumption**: Workflows resume automatically without user intervention
3. **Flexible scheduling**: Support one-time, interval, and cron-style triggers
4. **Minimal dependencies**: Use MongoDB (existing), avoid heavy frameworks
5. **Incremental adoption**: Existing workflows continue to work unchanged

---

## 2. Current State Analysis

### 2.1 Existing Components

| Component | Purpose | Durability |
|-----------|---------|------------|
| `WorkflowExecutor` | State machine runner | ❌ In-memory only |
| `WorkflowExecutionTrace` | Post-hoc logging | ✅ MongoDB (30-day TTL) |
| `BackgroundTaskRegistry` | Thread-pool for async | ❌ In-memory only |
| `rag_indexing_worker.py` | Polling-based worker | ⚠️ Uses DB status flags |

### 2.2 What Works Today

The **RAG indexing worker** pattern demonstrates a working async model:
- Documents marked `PENDING` in MongoDB
- Worker polls for pending items
- Status updated to `INDEXED`, `FAILED`, or `SKIPPED`
- Resume on restart: re-polls pending items

**Key insight**: MongoDB status flags provide implicit checkpointing.

### 2.3 What's Missing

1. **Generalised instance table**: Need a `workflow_instances` collection (not just traces)
2. **State serialisation**: Need to persist `WorkflowState` mid-execution
3. **Worker abstraction**: Decouple polling loop from specific workflow logic
4. **Scheduler**: Currently no way to trigger workflows by time

---

## 3. Design Principles

1. **Database is the source of truth**: All workflow state lives in MongoDB
2. **Idempotent operations**: Steps can be re-executed safely on resume
3. **Fail-safe defaults**: Incomplete workflows are detectable and resumable
4. **Backward compatible**: Existing synchronous workflows unchanged
5. **Observable progression**: Every state change is logged and queryable

---

## 4. Architecture Overview

### 4.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     Von Application Server                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  API Routes  │  │  Orchestrator │  │  WorkflowScheduler  │  │
│  │  /workflows  │  │  (sync calls) │  │  (timer-based)      │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘  │
│         │                 │                      │              │
│         │    ┌────────────┴──────────────────────┘              │
│         │    │                                                  │
│         ▼    ▼                                                  │
│  ┌─────────────────────────────────────┐                       │
│  │       WorkflowInstanceManager       │                       │
│  │  - create_instance()                │                       │
│  │  - submit_for_execution()           │                       │
│  │  - get_status()                     │                       │
│  │  - checkpoint()                     │                       │
│  │  - resume()                         │                       │
│  └─────────────────┬───────────────────┘                       │
│                    │                                            │
└────────────────────┼────────────────────────────────────────────┘
                     │
                     ▼
          ┌──────────────────────┐
          │       MongoDB        │
          │  ┌────────────────┐  │
          │  │workflow_instances│ │
          │  │workflow_schedules│ │
          │  │workflow_executions││
          │  └────────────────┘  │
          └──────────┬───────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│                  DurableWorkflowWorker(s)                        │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Poll Loop:                                               │  │
│  │  1. Claim pending/resumable instances (atomic lock)       │  │
│  │  2. Load workflow definition + state                      │  │
│  │  3. Execute steps (using WorkflowExecutor)                │  │
│  │  4. Checkpoint after each step                            │  │
│  │  5. Mark completed/failed                                 │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Data Flow

1. **Submission**: Client/scheduler creates `workflow_instance` with `status=pending`
2. **Claim**: Worker atomically sets `status=running`, `locked_by=worker_id`, `lock_expires_at=now+TTL`
3. **Execution**: Worker runs steps, checkpointing state after each
4. **Completion**: Worker sets `status=completed` or `status=failed`, releases lock
5. **Resume**: On restart, worker finds instances where `status=running` AND `lock_expires_at < now`

---

## 5. Workflow Instance Model

### 5.1 MongoDB Collection: `workflow_instances`

```typescript
interface WorkflowInstance {
  // Identity
  _id: ObjectId;
  instance_id: string;           // UUID, primary lookup key
  workflow_id: string;           // Definition ID (e.g., "#V#rag_sync_workflow")

  // Ownership
  user_id: string;               // Who initiated
  org_id: string;                // Organisation context
  namespace: string;             // Full namespace for data access

  // Status
  status: "pending" | "running" | "completed" | "failed" | "cancelled" | "paused";
  created_at: Date;
  started_at: Date | null;
  completed_at: Date | null;

  // Execution state (for resume)
  current_state: string;         // State machine state ID
  workflow_data: object;         // Serialised WorkflowState.data
  step_index: number;            // For sequential workflows: last completed step

    // Progress tracking
    progress_current: number | null;
    progress_total: number | null;
    progress_message: string | null;
    progress_updated_at: Date | null;

  // Locking (for distributed workers)
  locked_by: string | null;      // Worker ID holding lock
  lock_expires_at: Date | null;  // Auto-release stale locks

  // Inputs/outputs
  inputs: object;                // Initial workflow inputs
  outputs: object | null;        // Final outputs (on completion)

  // Error info
  error: string | null;
  error_step: string | null;
  retry_count: number;
  max_retries: number;

  // Scheduling (if scheduled)
  schedule_id: string | null;    // Reference to workflow_schedules

  // Tracing
  execution_trace_id: string | null;  // Link to workflow_executions
}
```

### 5.2 Indexes

```javascript
// Primary lookup
db.workflow_instances.createIndex({ instance_id: 1 }, { unique: true });

// Worker polling: find claimable instances
db.workflow_instances.createIndex(
  { status: 1, lock_expires_at: 1 },
  { partialFilterExpression: { status: { $in: ["pending", "running"] } } }
);

// User queries
db.workflow_instances.createIndex({ user_id: 1, created_at: -1 });
db.workflow_instances.createIndex({ org_id: 1, status: 1 });

// Schedule association
db.workflow_instances.createIndex({ schedule_id: 1, created_at: -1 });

// TTL: auto-delete old completed instances after 30 days
db.workflow_instances.createIndex(
  { completed_at: 1 },
  { expireAfterSeconds: 30 * 24 * 60 * 60, partialFilterExpression: { status: "completed" } }
);
```

### 5.3 Status Transitions

```
                    ┌─────────────────┐
         ┌─────────│     pending     │◄────────┐
         │         └────────┬────────┘         │
         │                  │                  │
         │                  │ claim            │ schedule
         │                  ▼                  │ triggers
         │         ┌────────────────┐          │
         │         │    running     │──────────┘
         │         └───┬────────┬───┘
         │             │        │
         │   success   │        │ failure (retriable)
         │             │        │
         │             ▼        ▼
         │    ┌────────────┐  ┌──────────────┐
         │    │ completed  │  │   running    │ (retry)
         │    └────────────┘  └──────────────┘
         │                          │
         │                          │ failure (max retries)
         │                          ▼
         │                    ┌──────────────┐
         │                    │    failed    │
         │                    └──────────────┘
         │
         │ cancel request
         ▼
  ┌────────────┐
  │ cancelled  │
  └────────────┘
```

---

## 6. Checkpoint & Resume

### 6.1 Checkpoint Strategy

**When to checkpoint**:
- After each workflow step completes successfully
- Before any operation that could fail (LLM call, external API)

**What to checkpoint**:
- Current state ID
- Accumulated workflow data (context dict)
- Step completion index (for sequential flows)
- Trace so far (optional, for debugging)

### 6.2 Checkpoint Implementation

```python
class DurableWorkflowExecutor:
    """Extended WorkflowExecutor with checkpoint support."""

    def __init__(
        self,
        *,
        registry: ActionRegistry,
        instance_manager: WorkflowInstanceManager,
        max_transitions: int = 50,  # Higher limit for durable workflows
    ):
        self._registry = registry
        self._instance_manager = instance_manager
        self._max_transitions = max_transitions

    async def run_durable(
        self,
        instance_id: str,
        *,
        resume_from_checkpoint: bool = True,
    ) -> WorkflowResult:
        """Execute workflow with checkpointing."""
        instance = await self._instance_manager.load(instance_id)

        # Load definition
        definition = self._load_definition(instance.workflow_id)

        # Restore state from checkpoint
        if resume_from_checkpoint and instance.workflow_data:
            context = dict(instance.workflow_data)
            current_state = instance.current_state
        else:
            context = dict(instance.inputs)
            current_state = definition.initial_state

        # Create environment
        environment = WorkflowEnvironment(
            user_id=instance.user_id,
            org_id=instance.org_id,
            namespace=instance.namespace,
        )

        # Execute with checkpointing
        transitions = 0
        while transitions < self._max_transitions:
            # Check for cancellation
            if await self._instance_manager.is_cancelled(instance_id):
                return WorkflowResult(
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error="cancelled",
                )

            state_spec = definition.states.get(current_state)
            if state_spec is None:
                return WorkflowResult(
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error=f"unknown_state:{current_state}",
                )

            # Execute actions
            for action in state_spec.actions:
                result = await self._execute_action(action, context, environment)
                if not result.ok:
                    # Checkpoint failure state
                    await self._instance_manager.checkpoint(
                        instance_id,
                        current_state=current_state,
                        workflow_data=context,
                        error=result.error,
                    )
                    return WorkflowResult(
                        data=context,
                        completed=False,
                        final_state=current_state,
                        error=result.error,
                    )
                context.update(result.outputs)

            # Check terminal
            if state_spec.terminal:
                return WorkflowResult(
                    data=context,
                    completed=True,
                    final_state=current_state,
                )

            # Evaluate transitions
            next_state = self._evaluate_transitions(state_spec.transitions, context)
            if next_state is None:
                return WorkflowResult(
                    data=context,
                    completed=False,
                    final_state=current_state,
                    error="no_transition",
                )

            # CHECKPOINT after successful step
            await self._instance_manager.checkpoint(
                instance_id,
                current_state=next_state,
                workflow_data=context,
            )

            current_state = next_state
            transitions += 1

        return WorkflowResult(
            data=context,
            completed=False,
            final_state=current_state,
            error="transition_limit",
        )
```

### 6.3 Resume Logic

```python
class WorkflowInstanceManager:
    """Manages workflow instance lifecycle."""

    async def find_resumable_instances(
        self,
        worker_id: str,
        limit: int = 10,
    ) -> list[WorkflowInstance]:
        """Find instances that can be resumed by this worker."""
        now = datetime.now(timezone.utc)

        # Atomic claim: find and update in one operation
        instances = []
        for _ in range(limit):
            instance = await self._collection.find_one_and_update(
                {
                    "$or": [
                        # New pending instances
                        {"status": "pending"},
                        # Stale running instances (lock expired)
                        {
                            "status": "running",
                            "lock_expires_at": {"$lt": now},
                        },
                    ],
                },
                {
                    "$set": {
                        "status": "running",
                        "locked_by": worker_id,
                        "lock_expires_at": now + timedelta(minutes=5),
                        "started_at": {"$ifNull": ["$started_at", now]},
                    },
                },
                return_document=ReturnDocument.AFTER,
            )
            if instance:
                instances.append(WorkflowInstance.from_doc(instance))
            else:
                break

        return instances
```

---

## 7. Scheduling System

### 7.1 MongoDB Collection: `workflow_schedules`

```typescript
interface WorkflowSchedule {
  _id: ObjectId;
  schedule_id: string;           // UUID
  workflow_id: string;           // Definition to run

  // Ownership
  user_id: string;
  org_id: string;
  namespace: string;

  // Schedule config
  schedule_type: "once" | "interval" | "cron";

  // For "once"
  run_at: Date | null;

  // For "interval"
  interval_seconds: number | null;

  // For "cron"
  cron_expression: string | null;  // Standard cron format

  // State
  enabled: boolean;
  last_run_at: Date | null;
  next_run_at: Date | null;

  // Inputs
  default_inputs: object;        // Passed to each workflow instance

  // Metadata
  created_at: Date;
  updated_at: Date;
  description: string | null;
}
```

### 7.2 Scheduler Service

```python
class WorkflowScheduler:
    """Periodically checks schedules and creates workflow instances."""

    def __init__(
        self,
        instance_manager: WorkflowInstanceManager,
        check_interval_seconds: int = 60,
    ):
        self._instance_manager = instance_manager
        self._check_interval = check_interval_seconds
        self._running = False

    async def start(self):
        """Start the scheduler loop."""
        self._running = True
        while self._running:
            try:
                await self._process_due_schedules()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(self._check_interval)

    async def _process_due_schedules(self):
        """Find and trigger due schedules."""
        now = datetime.now(timezone.utc)

        due_schedules = await self._get_schedules_collection().find({
            "enabled": True,
            "next_run_at": {"$lte": now},
        }).to_list(length=100)

        for schedule in due_schedules:
            # Create workflow instance
            instance_id = await self._instance_manager.create_instance(
                workflow_id=schedule["workflow_id"],
                inputs=schedule["default_inputs"],
                user_id=schedule["user_id"],
                org_id=schedule["org_id"],
                namespace=schedule["namespace"],
                schedule_id=schedule["schedule_id"],
            )

            # Update schedule
            next_run = self._calculate_next_run(schedule)
            await self._get_schedules_collection().update_one(
                {"_id": schedule["_id"]},
                {
                    "$set": {
                        "last_run_at": now,
                        "next_run_at": next_run,
                    },
                },
            )

            logger.info(
                f"Triggered schedule {schedule['schedule_id']}: "
                f"created instance {instance_id}"
            )
```

---

## 8. Worker Architecture

### 8.1 DurableWorkflowWorker

```python
class DurableWorkflowWorker:
    """Background worker that processes durable workflow instances."""

    def __init__(
        self,
        worker_id: str,
        instance_manager: WorkflowInstanceManager,
        executor: DurableWorkflowExecutor,
        *,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 5,
        heartbeat_interval_seconds: float = 60.0,
    ):
        self._worker_id = worker_id
        self._instance_manager = instance_manager
        self._executor = executor
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._heartbeat_interval = heartbeat_interval_seconds
        self._running = False
        self._current_instances: dict[str, asyncio.Task] = {}

    async def start(self):
        """Start the worker loop."""
        self._running = True
        logger.info(f"DurableWorkflowWorker {self._worker_id} starting")

        # Start heartbeat task
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            while self._running:
                # Claim instances
                instances = await self._instance_manager.find_resumable_instances(
                    self._worker_id,
                    limit=self._batch_size,
                )

                if instances:
                    for instance in instances:
                        if instance.instance_id not in self._current_instances:
                            task = asyncio.create_task(
                                self._process_instance(instance)
                            )
                            self._current_instances[instance.instance_id] = task
                else:
                    # No work, wait before polling again
                    await asyncio.sleep(self._poll_interval)

                # Clean up completed tasks
                await self._cleanup_completed_tasks()
        finally:
            heartbeat_task.cancel()
            await self._cancel_current_tasks()

    async def _process_instance(self, instance: WorkflowInstance):
        """Process a single workflow instance."""
        try:
            logger.info(
                f"Processing instance {instance.instance_id} "
                f"(workflow={instance.workflow_id})"
            )

            result = await self._executor.run_durable(
                instance.instance_id,
                resume_from_checkpoint=True,
            )

            if result.completed:
                await self._instance_manager.mark_completed(
                    instance.instance_id,
                    outputs=result.data,
                )
                logger.info(f"Instance {instance.instance_id} completed")
            else:
                await self._instance_manager.mark_failed(
                    instance.instance_id,
                    error=result.error or "unknown_error",
                )
                logger.warning(
                    f"Instance {instance.instance_id} failed: {result.error}"
                )
        except Exception as e:
            logger.exception(f"Error processing instance {instance.instance_id}")
            await self._instance_manager.mark_failed(
                instance.instance_id,
                error=str(e),
            )
        finally:
            # Release lock
            await self._instance_manager.release_lock(
                instance.instance_id,
                self._worker_id,
            )

    async def _heartbeat_loop(self):
        """Periodically extend locks on active instances."""
        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            for instance_id in list(self._current_instances.keys()):
                try:
                    await self._instance_manager.extend_lock(
                        instance_id,
                        self._worker_id,
                        extend_seconds=int(self._heartbeat_interval * 2),
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to extend lock for {instance_id}: {e}"
                    )
```

### 8.2 Worker Deployment

**Option A: In-process (development/single-server)**
```python
# In server startup
worker = DurableWorkflowWorker(
    worker_id=f"server_{socket.gethostname()}_{os.getpid()}",
    instance_manager=instance_manager,
    executor=executor,
)
asyncio.create_task(worker.start())
```

**Option B: Separate process (production)**
```python
# scripts/durable_workflow_worker.py
if __name__ == "__main__":
    worker = DurableWorkflowWorker(
        worker_id=f"worker_{uuid.uuid4().hex[:8]}",
        instance_manager=instance_manager,
        executor=executor,
    )
    asyncio.run(worker.start())
```

---

## 9. API Design

### 9.1 REST Endpoints

```yaml
# Create and submit workflow instance
POST /api/workflows/instances
Request:
  workflow_id: string
  inputs: object
  schedule?: { type: "once" | "interval" | "cron", ... }
Response:
  instance_id: string
  status: "pending"

# Get instance status
GET /api/workflows/instances/{instance_id}
Response:
  instance_id: string
  workflow_id: string
  status: string
  current_state: string
  progress: { step: number, total: number } | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  error: string | null

# List user's instances
GET /api/workflows/instances?status=running&limit=20
Response:
  instances: [...]
  total: number

# Cancel instance
POST /api/workflows/instances/{instance_id}/cancel
Response:
  success: boolean

# Get instance result (for completed)
GET /api/workflows/instances/{instance_id}/result
Response:
  outputs: object
  execution_trace_id: string

# Create schedule
POST /api/workflows/schedules
Request:
  workflow_id: string
  schedule_type: "once" | "interval" | "cron"
  run_at?: string  # ISO date for "once"
  interval_seconds?: number
  cron_expression?: string
  default_inputs: object
Response:
  schedule_id: string

# List schedules
GET /api/workflows/schedules

# Update schedule (enable/disable)
PATCH /api/workflows/schedules/{schedule_id}
Request:
  enabled?: boolean
  default_inputs?: object

# Delete schedule
DELETE /api/workflows/schedules/{schedule_id}
```

### 9.2 MCP Tool Integration

```python
# In catalogue.py
DURABLE_WORKFLOW_TOOLS = [
    MCPToolSpec(
        name="submit_durable_workflow",
        description="Submit a workflow for background execution",
        input_schema=Schema(
            required={"workflow_id": str},
            optional={
                "inputs": dict,
                "schedule_type": str,
                "schedule_config": dict,
            },
        ),
        handler=_submit_durable_workflow,
    ),
    MCPToolSpec(
        name="get_workflow_status",
        description="Get the status of a running workflow instance",
        input_schema=Schema(required={"instance_id": str}),
        handler=_get_workflow_status,
    ),
    MCPToolSpec(
        name="list_my_workflows",
        description="List workflow instances for the current user",
        input_schema=Schema(
            optional={"status": str, "limit": int},
        ),
        handler=_list_my_workflows,
    ),
]
```

---

## 10. Vontology Integration

### 10.1 New Concept Types

```
#V#durable_workflow_instance (type)
  is_a_type_of: #V#workflow_execution
  description: "A persistently tracked workflow execution that can be checkpointed and resumed"

#V#workflow_schedule (type)
  is_a_type_of: #V#scheduled_task
  description: "A recurring or one-time trigger for workflow execution"
```

### 10.2 Workflow Definition Predicates

Add to existing `#V#llm_workflow`:

```
#V#supports_durable_execution (predicate)
  description: "Indicates this workflow can be run durably with checkpointing"
  domain: #V#llm_workflow
  range: boolean

#V#default_schedule (predicate)
  description: "Default schedule configuration for this workflow"
  domain: #V#llm_workflow
  range: #V#workflow_schedule
```

### 10.3 Example: RAG Sync Workflow Definition

```
#V#rag_text_relation_sync_workflow (individual)
  is_an_instance_of: #V#llm_workflow
  hasName: "RAG Text Relation Sync"
  hasDescription: "Synchronises Vontology text relations to RAG store for semantic search"
  supports_durable_execution: true
  hasInitialStep: #V#rag_sync_step_collect
  hasStep: [#V#rag_sync_step_collect, #V#rag_sync_step_batch, #V#rag_sync_step_upsert, #V#rag_sync_step_complete]

#V#rag_sync_step_collect (individual)
  is_an_instance_of: #V#workflow_step
  hasName: "Collect text relations"
  invokesAction: #V#collect_text_relations_action
  nextStep: #V#rag_sync_step_batch

#V#rag_sync_step_batch (individual)
  is_an_instance_of: #V#workflow_step
  hasName: "Batch for upsert"
  invokesAction: #V#batch_documents_action
  nextStep: #V#rag_sync_step_upsert

#V#rag_sync_step_upsert (individual)
  is_an_instance_of: #V#workflow_step
  hasName: "Upsert to RAG"
  invokesAction: #V#rag_upsert_batch_action
  hasPrecondition: #V#has_more_batches_condition
  onTrueNextStep: #V#rag_sync_step_batch  # Loop back
  onFalseNextStep: #V#rag_sync_step_complete

#V#rag_sync_step_complete (individual)
  is_an_instance_of: #V#workflow_step
  hasName: "Complete"
  terminal: true
```

---

## 11. Migration Path

### 11.1 Phase 1: Infrastructure (Week 1)

1. Create `workflow_instances` collection with indexes
2. Implement `WorkflowInstanceManager` (create, checkpoint, status)
3. Add basic REST endpoints for instance management
4. Write unit tests

### 11.2 Phase 2: Durable Execution (Week 2)

1. Implement `DurableWorkflowExecutor` with checkpointing
2. Create `DurableWorkflowWorker` with polling loop
3. Add heartbeat and lock management
4. Integration tests for resume scenarios

### 11.3 Phase 3: RAG Sync Migration (Week 2-3)

1. Define `#V#rag_text_relation_sync_workflow` in Vontology
2. Create action implementations for RAG sync steps
3. Migrate `rag_sync_text_relations` to submit durable workflow
4. Deprecate synchronous path (with feature flag)

### 11.4 Phase 4: Scheduling (Week 3)

1. Create `workflow_schedules` collection
2. Implement `WorkflowScheduler` service
3. Add schedule management REST endpoints
4. Test cron expressions and interval triggers

### 11.5 Phase 5: Observability (Week 4)

1. Add progress tracking to instance model
2. Implement SSE stream for live status updates
3. Create simple workflow status UI component
4. Documentation and user guide

---

## 12. Implementation Plan

### 12.1 New Files

```
src/backend/workflows/
├── durable/
│   ├── __init__.py
│   ├── instance_manager.py      # WorkflowInstanceManager
│   ├── durable_executor.py      # DurableWorkflowExecutor
│   ├── worker.py                # DurableWorkflowWorker
│   ├── scheduler.py             # WorkflowScheduler
│   └── models.py                # WorkflowInstance, WorkflowSchedule
├── definitions/
│   ├── rag_sync_workflow.py     # RAG sync workflow definition
│   └── ...
```

### 12.2 Modified Files

- `src/backend/server/routes/workflows_routes.py` - Add instance/schedule endpoints
- `src/backend/integrations/internal_mcp/catalogue.py` - Add MCP tools
- `src/backend/services/rag_text_relation_sync_service.py` - Use durable workflow

### 12.3 Jira Tasks

| Task | Summary | Estimate |
|------|---------|----------|
| JVNAUTOSCI-XXXX | Durable workflow infrastructure (collections, manager) | 2d |
| JVNAUTOSCI-XXXX | DurableWorkflowExecutor with checkpointing | 2d |
| JVNAUTOSCI-XXXX | DurableWorkflowWorker with lock management | 2d |
| JVNAUTOSCI-XXXX | Migrate RAG sync to durable workflow | 3d |
| JVNAUTOSCI-XXXX | Workflow scheduling system | 2d |
| JVNAUTOSCI-XXXX | REST API and MCP tools | 1d |
| JVNAUTOSCI-XXXX | Documentation and testing | 2d |

---

## 13. Testing Strategy

### 13.1 Unit Tests

```python
# tests/backend/workflows/durable/test_instance_manager.py
class TestWorkflowInstanceManager:
    async def test_create_instance_pending(self):
        """New instance starts in pending status."""

    async def test_checkpoint_updates_state(self):
        """Checkpoint persists current state and data."""

    async def test_find_resumable_claims_atomically(self):
        """Concurrent workers don't claim same instance."""

    async def test_stale_lock_allows_reclaim(self):
        """Instance with expired lock can be reclaimed."""
```

### 13.2 Integration Tests

```python
# tests/backend/workflows/durable/test_resume_scenarios.py
class TestResumeScenarios:
    async def test_resume_after_checkpoint(self):
        """Workflow resumes from checkpointed state."""

    async def test_resume_after_worker_crash(self):
        """New worker picks up orphaned instance."""

    async def test_cancel_running_workflow(self):
        """Cancellation stops workflow gracefully."""
```

### 13.3 Load Tests

- 100 concurrent workflow submissions
- Worker restart during execution
- Schedule with 1-second interval

---

## 14. Operational Concerns

### 14.1 Monitoring

**Metrics to expose**:
- `workflow_instances_pending` - Gauge: pending instance count
- `workflow_instances_running` - Gauge: running instance count
- `workflow_instance_duration_seconds` - Histogram: execution time
- `workflow_checkpoint_count` - Counter: checkpoints written
- `workflow_resume_count` - Counter: workflows resumed

**Live status stream**:
- `GET /api/workflows/instances/stream` (SSE)
- Filters: `user_id`, `org_id`, `namespace`, `workflow_id`, `instance_id`, `status` (comma-separated)
- Payload includes progress fields (`progress.current`, `progress.total`, `progress.message`)

### 14.2 Alerts

- Pending instances > 100 for > 5 minutes
- Failed instances > 10 in 1 hour
- Worker heartbeat missed for > 2 minutes

### 14.3 Troubleshooting

**Stuck workflows**:
```javascript
// Find instances that have been running too long
db.workflow_instances.find({
  status: "running",
  started_at: { $lt: new Date(Date.now() - 3600000) }  // 1 hour
})
```

**Orphaned locks**:
```javascript
// Release all expired locks
db.workflow_instances.updateMany(
  { status: "running", lock_expires_at: { $lt: new Date() } },
  { $set: { locked_by: null, lock_expires_at: null } }
)
```

---

## Revision History

| Version | Date       | Changes                          | Author           |
|---------|------------|----------------------------------|------------------|
| 1.0     | 2026-02-03 | Initial design document          | Von AI Assistant |

---

**End of Document**
