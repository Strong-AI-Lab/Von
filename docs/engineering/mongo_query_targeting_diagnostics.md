# Mongo Query Targeting Diagnostics

**Status:** Operational guide  
**Related Jira:** `JVNAUTOSCI-2427`, `JVNAUTOSCI-2426`

Von now has two complementary Mongo query-targeting diagnostics surfaces:

1. In-process slow command telemetry from the global PyMongo command listener.
2. A bounded profiler report command over MongoDB `system.profile`.

Both surfaces are designed as support plumbing only. They record query shape,
execution counters, and operational attribution; they must not become a hidden
workflow, prompt, routing, or index-creation policy surface.

## What Is Captured

Slow command listener rows can retain:

- command name, database, collection, and namespace
- filter, sort, projection, update, and aggregate pipeline shapes
- limit or batch-size shape
- duration and request id
- returned count when available from the command reply
- safe profiler comment attribution such as Von service, operation, and route

Profiler and explain-backed rows can additionally include:

- `docsExamined`
- `keysExamined`
- `nReturned`
- scanned-per-returned ratios
- plan summary or winning index
- in-memory sort evidence
- query hash or plan cache key, when present

The diagnostics intentionally do not retain query values, update values,
returned documents, `.env` contents, Mongo URIs, credentials, prompts, email
bodies, or private document bodies.

## Running The Report

Use the repository virtualenv if the shell does not expose `pdm`:

```sh
.venv/bin/python scripts/mongo_query_targeting_report.py --json
```

Useful bounded variants:

```sh
.venv/bin/python scripts/mongo_query_targeting_report.py \
  --namespace von_db.workflow_instances \
  --min-millis 0 \
  --sample-limit 200 \
  --report-limit 20 \
  --json
```

```sh
.venv/bin/python scripts/mongo_query_targeting_report.py \
  --namespace von_db.workflow_instances \
  --sample-limit 50 \
  --explain-samples 3 \
  --explain-max-time-ms 2000 \
  --json
```

`--explain-samples` only attempts sampled read-only `find` explains from
profiler entries. The report prints execution stats and shapes, not returned
documents. Keep the sample count low on a busy database.

If `system.profile` is unavailable, use Atlas Query Insights or Performance
Advisor for the same time window.

## Reading The Output

The report ranks rows by:

1. estimated waste score
2. maximum docs-scanned-per-returned ratio
3. maximum keys-scanned-per-returned ratio
4. total duration
5. recurring count

A merely slow query with a low scanned-per-returned ratio is different from a
query-targeting problem. `JVNAUTOSCI-2426` exposed this distinction: local logs
showed slow operations, but Atlas highlighted a `workflow_instances` worker
claim shape with approximately `129789` documents scanned for `1` returned row
at high query volume. The new report is designed to rank that kind of shape
above a high-duration but well-targeted lookup.

Fields to inspect:

- `namespace`: database and collection.
- `filter_shape`, `sort_shape`, `projection_shape`: structural query shape.
- `max_docs_examined_per_returned`: worst observed object scan ratio.
- `max_keys_examined_per_returned`: worst observed key scan ratio.
- `last_plan_summary` and `last_winning_index`: planner evidence.
- `sample_request_ids`: request ids, query hashes, or plan cache keys suitable
  for cross-reference.
- `recommended_next_step`: operator guidance for the next diagnostic action.

## Reconciling With Atlas

Atlas Query Insights and Performance Advisor use cluster-side telemetry. Von's
local command listener sees only operations from the current process, and the
profiler report sees only what Mongo profiling captured for the selected
database and time window. Differences are expected.

Recommended reconciliation loop:

1. Start with the Atlas alert or Query Insights time window.
2. Run the Von profiler report for the matching namespace and window where
   profiler data is available.
3. Compare Atlas query shape text/hash with Von `filter_shape`, `sort_shape`,
   `projection_shape`, and `sample_request_ids`.
4. If Atlas recommends an index, compare it with repo-owned index setup before
   making changes.
5. Materialise approved index changes in code or migration/setup surfaces, not
   as hidden live mutations from a diagnostics command.
6. Re-run the report after deployment and verify scanned-per-returned ratios
   fall for the same shape.

Useful Atlas Admin API surfaces, when service-account credentials are available:

- Query Shape Insights summaries:
  `GET /api/atlas/v2/groups/{groupId}/clusters/{clusterName}/queryShapeInsights/summaries`
- Query shapes:
  `GET /api/atlas/v2/groups/{groupId}/clusters/{clusterName}/queryShapes`
- Performance Advisor suggested indexes:
  `GET /api/atlas/v2/groups/{groupId}/processes/{processId}/performanceAdvisor/suggestedIndexes`
- Performance Advisor slow query logs:
  `GET /api/atlas/v2/groups/{groupId}/processes/{processId}/performanceAdvisor/slowQueryLogs`

Atlas Admin API credentials are separate from Mongo URI credentials. Do not
print either credential class in reports, Jira comments, logs, or screenshots.

## Environment Controls

- `VON_MONGO_QUERY_SHAPE_TELEMETRY_ENABLED`: enable/disable in-process
  query-shape telemetry. Defaults to enabled.
- `VON_MONGO_QUERY_SHAPE_MAX_ROWS`: maximum distinct in-process shapes retained.
  Defaults to `512`.
- `VON_MONGO_OPERATION_AUDIT_SLOW_MS`: existing slow-operation threshold.
  Defaults to `500`.

The profiler report command is read-only unless `--explain-samples` is used.
`explain` remains read-only, but it can still consume database resources, so keep
sample counts and max time bounded.
