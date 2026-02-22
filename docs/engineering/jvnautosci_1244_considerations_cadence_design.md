# JVNAUTOSCI-1244 Considerations-for-Use Cadence Design

Date: 2026-02-22

## Scope

This task is planning-only. It defines the global cadence design for Considerations-for-Use generation and does not introduce production code changes in this ticket.

## Objective

Adopt a workflow-first global cadence policy so that:

1. `concept.created` keeps immediate Considerations-for-Use generation.
2. `concept.updated` stops launching immediate enrichment and instead feeds a global ~5-minute cadence run.
3. Repeated updates inside a cadence window dedupe cleanly and do not create duplicate work.

## Current State (Observed)

Observed from workflow surfaces and event-launch code:

1. `#V#enrichment_workflow` currently has `background_launch_policy` with:
   - `enabled=true`
   - `min_interval_seconds=300`
   - `scope=global_per_server`
   - `applies_to_sources=["event"]`
2. Both `concept.created` and `concept.updated` are bound directly to `#V#enrichment_workflow` for `#V#has_considerations_for_use`.
3. Cadence gating is evaluated before instance creation and is workflow-global, so one recent event-triggered instance can block another event type for the same workflow ID.
4. There is no explicit update-candidate queue for later scheduled processing.
5. A daily enrichment schedule exists but is disabled and is not the required 5-minute update cadence.

Implication: current gating is a skip gate, not a queue-backed cadence system.

## Selected Trigger Policy

1. `concept.created`:
   - Immediate launch remains enabled.
   - Must not be blocked by update cadence throttling.
2. `concept.updated`:
   - No direct enrichment launch.
   - Event path records/refreshes a pending candidate marker.
   - Processing happens only through a global 5-minute scheduler.

## Workflow-First Architecture

### A. Event intake (updated concepts)

`concept.updated` binding targets an intake workflow (or intake-capable step) that only records candidate state.

Required behaviour:

1. Idempotent candidate upsert keyed by `concept_id`.
2. Update marker timestamp on every update event.
3. Do not spawn enrichment instances from this path.

### B. Immediate path (new concepts)

`concept.created` binding continues to launch immediate enrichment for `#V#has_considerations_for_use` with `force_concept_id=event.concept_id` and `limit=1`.

### C. Global cadence dispatcher

One server-wide schedule runs every 300 seconds and dispatches bounded enrichment batches from pending candidates.

Required behaviour:

1. Deterministic candidate ordering (oldest pending first).
2. Bounded batch size per run (operator-configurable).
3. Candidate state updated atomically after success/failure.
4. Retries use bounded attempts with next-attempt timestamp.

## Candidate Data Model (Vontology-first)

Persist candidate state as Vontology-governed data, not ephemeral in-memory state.

Minimum logical fields:

1. `concept_id`
2. `pending_since`
3. `last_updated_event_at`
4. `last_attempt_at`
5. `attempt_count`
6. `last_result` (`success|failed|skipped`)
7. `last_error` (optional diagnostic text)

Representation can be a dedicated candidate concept type or canonical text relations, but persistence must remain Vontology-managed and queryable via Vontology services/tools.

## Dedupe and Back-Pressure Rules

1. Multiple updates for the same concept inside one cadence window collapse to one pending candidate.
2. Dispatcher enforces a per-run batch cap and leaves residual candidates pending.
3. Repeated failures move candidates to retry-later state (bounded retry policy).
4. Scheduler must not launch overlapping dispatcher runs for the same workflow (instance-level idempotency/locking).

## Observability Requirements

Per cadence run, emit telemetry including:

1. `window_started_at`, `window_ended_at`, `duration_ms`
2. `pending_at_start`, `processed_count`, `success_count`, `failed_count`, `skipped_count`, `remaining_count`
3. `batch_size_limit`, `retry_count`, `oldest_pending_age_seconds`
4. `reason` when no work is processed (for example `no_pending_candidates`, `dispatcher_locked`)

Per event intake, emit:

1. `event_type`, `concept_id`
2. `candidate_state` (`created|refreshed|unchanged`)

## Migration and Rollout Plan

1. Introduce candidate intake for `concept.updated` while keeping current immediate path temporarily behind a feature flag.
2. Enable 5-minute dispatcher schedule and validate telemetry + throughput.
3. Disable direct `concept.updated` enrichment launch after verification.
4. Keep `concept.created` immediate path enabled throughout.

Rollback:

1. Re-enable direct `concept.updated` launch binding.
2. Disable dispatcher schedule.
3. Preserve candidate records for forensic analysis; do not delete on rollback.

## Edge Cases (Explicit)

1. Burst updates on one concept: dedupe by `concept_id`, retain freshest update timestamp.
2. Duplicate event deliveries: intake remains idempotent.
3. Backlog growth: bounded batch processing with residual carry-over and age telemetry.
4. Service restart/recovery: pending candidates persist and are resumed by next schedule tick.
5. Concurrent scheduler ticks: guarded by workflow instance lock/idempotency.
6. Created-then-updated quickly: create path runs immediately; update path queues potential follow-up refresh.

## Test Plan (for implementation task)

### Unit tests

1. Intake dedupe behaviour for repeated `concept.updated` events.
2. Candidate ordering and batch slicing.
3. Retry/backoff transitions and terminal failure handling.
4. Created-vs-updated policy split (created immediate, updated queued).

### Integration tests

1. Event binding `concept.updated` writes candidate state without launching enrichment.
2. 5-minute dispatcher launches enrichment for queued candidates and updates state.
3. Cadence runs remain deterministic under burst updates.
4. Restart scenario resumes pending candidates.

### End-to-end tests

1. Real call path: update concept -> candidate marked -> scheduled run -> `#V#has_considerations_for_use` updated.
2. Real call path: create concept -> immediate enrichment occurs without waiting for cadence.
3. Operator control checks: disable/enable schedule toggles behaviour as expected.

## Completion Criteria for JVNAUTOSCI-1244

1. Design note exists (this document).
2. Decision for created-vs-updated handling is explicit.
3. Dedupe/back-pressure/observability/migration are specified.
4. Test plan is specified.
5. No production code changes are included in this ticket.
