# JVNAUTOSCI-2391 Mongo Hot-Path Attribution Audit

Date: 2026-06-03

## Purpose

JVNAUTOSCI-2391 is the evidence-baseline slice of JVNAUTOSCI-2388. Its
purpose is to make MongoDB cost and latency attributable to Von routes and
service calls before later subtasks optimise storage, hydration, polling, and
guardrails.

This is support-surface work only. It must not reduce retained history,
telemetry, LLM interaction data, tool evidence, workflow evidence, or replay
inspectability.

## Staleness Review

The parent issue JVNAUTOSCI-2388 was created after several adjacent fixes
landed. Current code already includes:

- JVNAUTOSCI-2382: async local spillway and migrator for durable blob writes.
- JVNAUTOSCI-2383: request-thread starvation and hot-poller mitigation,
  including the `/api/settings/db/info` TTL cache and workflow definitions
  stale-while-refresh cache.
- JVNAUTOSCI-2389: local-first debug blob cache for turn history/debug blobs.
- JVNAUTOSCI-2314: bounded durable workflow checkpoint context.

So this task should not reimplement storage offload. The current useful gap is
attribution: when Atlas profiler or local logs show a costly operation, future
agents need to know which route/service operation caused it.

## Observed Evidence

The parent issue records these concrete symptoms:

- A history/diagnostic path for
  `request_id=dd7ff7b0-6749-43bb-a3f3-14625da10af1` returned the useful user
  answer, but later diagnostic/history loading spent tens of seconds hydrating
  debug blobs and touching object-store metadata.
- Logs showed `GET von.history took 47455.8ms`.
- JVNAUTOSCI-2383 recorded hundreds of `/api/settings/db/info` calls in a
  single log window, plus slow `history_sessions`, `rag_status`,
  `get_all_settings`, and capability-index/status paths.
- JVNAUTOSCI-2307 remains the dedicated Atlas query-targeting alert task; this
  audit should feed it route/service labels rather than replacing index work.

## Implemented Attribution Surface

`src/backend/services/mongo_observability_service.py` now provides:

- PyMongo/Atlas query comments with safe route, endpoint, service, collection,
  and operation labels.
- Lightweight in-process counters for operation count, failures, slow
  operation count, total/average/max elapsed time, and last error type.
- Environment flags:
  - `VON_MONGO_QUERY_ATTRIBUTION_ENABLED` defaults to `true`.
  - `VON_MONGO_OPERATION_AUDIT_ENABLED` defaults to `true`.
  - `VON_MONGO_OPERATION_AUDIT_SLOW_MS` defaults to `500`.

The comments deliberately avoid query bodies, document bodies, prompt text,
email bodies, raw debug payloads, Mongo URIs, credentials, and secrets. Flask
route metadata uses `request.path`, not the query string.

## Labelled Hot Paths

### Chat History

`src/backend/services/chat_history_service.py` now labels the existing bounded
read wrappers:

- `get_chat_history.find_session`
- `get_chat_history_segments.aggregate_tail`
- `get_chat_history_segments.tail_fallback`
- `get_chat_history_segments.find_session`
- `get_chat_history_debug_entry.slice_entry`
- `get_chat_history_length.aggregate`
- `get_chat_history_session_count.aggregate`
- `get_chat_history_session_summaries.metadata_find`
- `get_chat_history_session_summaries.full_find`
- `has_chat_history_session.exists`
- `get_chat_history_session_summary.metadata_find`
- `get_chat_history_session_summary.full_find`
- `get_chat_history_session_summary.metadata_fallback`

These cover the main ordinary-history, session-list, and explicit-debug read
surfaces implicated in the `von.history` latency incident.

### Turn Execution Records

`src/backend/services/turn_execution_record_service.py` now labels:

- `upsert_turn_execution_record_projection.update_one`
- `get_latest_turn_execution_record_projection.find_latest`
- `get_latest_turn_execution_record_projection.fallback_find_latest`

These cover the diagnostic projection writes and latest-record reads that
connect chat history to turn-execution evidence.

### Settings / DB Info

`src/backend/server/routes/settings_routes.py` labels the live Mongo ping in
`/api/settings/db/info`:

- `get_db_location_info.ping`

`src/backend/services/settings_service.py` labels application-settings reads
and writes:

- `get_setting.find_one`
- `get_settings_batch.find`
- `update_setting.precheck_find_one`
- `update_setting.update_one`

These cover the named `get_all_settings`/settings hot path and the repeated DB
info poller evidence from JVNAUTOSCI-2383.

## Remaining Audit Targets

The following areas are intentionally left for follow-on implementation tasks
because they require more targeted measurement or behaviour changes:

- JVNAUTOSCI-2392: ordinary history and Thinking-card reads should remain
  compact unless diagnostics are explicitly requested.
- JVNAUTOSCI-2393: poller coalescing/caching for RAG status, capability-index
  status, settings/status, main health, and frontend visibility-aware loops.
- JVNAUTOSCI-2394: durable workflow checkpoint and turn-record storage pressure.
- JVNAUTOSCI-2395: operator-visible guardrails and diagnostic views for runaway
  local workloads.
- JVNAUTOSCI-2307: index/query-targeting fixes using Atlas profiler evidence.

Workflow-use episode queries and capability-index status should be revisited in
JVNAUTOSCI-2393 after polling frequency is measured. They are plausible cost
surfaces, but the immediate 2391 patch focuses on the already-evidenced
history, settings, and turn-record paths.

## Recommended Next Order

1. JVNAUTOSCI-2392, because it addresses the direct `GET von.history took
   47455.8ms` class of latency and hydration amplification.
2. JVNAUTOSCI-2393, because repeated small reads can generate cost while a
   browser is idle.
3. JVNAUTOSCI-2395, because once the labels and poller changes exist, Von can
   surface early warnings for expensive local workloads.
4. JVNAUTOSCI-2394, unless new evidence from 2391/2392 shows workflow/TER
   storage pressure is the immediate largest contributor.
5. JVNAUTOSCI-2396, after the implementation subtasks can be measured together.

## Evidence Gap

This pass adds route/service attribution and local counters but does not access
Atlas profiler data directly. When Atlas profiler access is available, filter
for query comments containing `jira: JVNAUTOSCI-2391` and group by
`service`, `operation`, and `route.endpoint`.
