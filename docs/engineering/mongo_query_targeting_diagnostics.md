# Mongo Query Targeting Diagnostics

**Status:** Operational guide  
**Related Jira:** `JVNAUTOSCI-2429`, `JVNAUTOSCI-2428`, `JVNAUTOSCI-2427`, `JVNAUTOSCI-2426`

Von now has seven complementary Mongo query-targeting and cost diagnostics
surfaces:

1. In-process slow command telemetry from the global PyMongo command listener.
2. A bounded profiler report command over MongoDB `system.profile`.
3. A bounded, read-only `$queryStats` report from the configured MongoDB
   deployment.
4. A read-only Atlas Admin API report for Query Shape Insights.
5. A read-only Atlas report review loop that compares reports and drafts or
   upserts Jira review tasks.
6. A Von-internal MCP tool, `mongo_query_diagnostics_report`, for trusted
   operator conversation and VWL maintenance workflows.
7. A compact cost guardrail report at `/api/settings/db/guardrails` and through
   the Von-internal MCP tool `mongo_cost_guardrails_report`.

These surfaces are designed as support plumbing only. They record query shape,
execution counters, and operational attribution; they must not become a hidden
workflow, prompt, routing, or index-creation policy surface.

## Mongo Cost Guardrail Report

`JVNAUTOSCI-2395` adds a compact redacted guardrail report for local/dev
runtimes connected to paid or remote MongoDB. It is intended for early warning:
it does not disable research/debugging work, mutate indexes, or decide workflow
policy.

HTTP surface:

```sh
curl http://127.0.0.1:5000/api/settings/db/guardrails
```

Von-internal MCP surface:

```json
{
  "tool": "mongo_cost_guardrails_report",
  "arguments": {
    "local_development": true
  }
}
```

The report includes:

- recent Mongo operation volume and per-minute rates
- slow operation counts using `VON_MONGO_OPERATION_AUDIT_SLOW_MS`
- background and poller route activity
- ranked redacted query-shape telemetry
- the last successful bounded `$queryStats` snapshot, its collection status,
  and freshness metadata
- size-only large write attempts observed by the PyMongo command listener
- blob/debug/workflow hydration counters grouped by family and cache status
- runtime posture: local, remote, or Atlas, with credentials removed
- warning codes such as `remote_local_high_operation_rate`,
  `background_operation_rate_high`, `slow_operation_rate_high`,
  `large_mongo_write_attempt`, and `query_targeting_waste_high`

The report intentionally omits Mongo credentials, URI query parameters, query
values, update values, document bodies, returned rows, prompts, email bodies,
raw debug payloads, `.env` contents, and secret values.

Tuning knobs:

- `VON_MONGO_GUARDRAIL_WINDOW_SECONDS` default `60`
- `VON_MONGO_GUARDRAIL_REMOTE_OPS_PER_MIN_WARN` default `300`
- `VON_MONGO_GUARDRAIL_BACKGROUND_OPS_PER_MIN_WARN` default `120`
- `VON_MONGO_GUARDRAIL_SLOW_OPS_PER_MIN_WARN` default `10`
- `VON_MONGO_OPERATION_AUDIT_SLOW_MS` default `500`
- `VON_MONGO_LARGE_WRITE_WARN_BYTES` default `8388608`
- `VON_MONGO_LARGE_WRITE_CRITICAL_BYTES` default `14680064`

Use `reset=1` on the HTTP endpoint or `{"reset": true}` on the MCP tool only
when deliberately starting a new observation window. Normal status views should
read the report without resetting it.

## What Is Captured

Slow command listener rows can retain:

- command name, database, collection, and namespace
- filter, sort, projection, update, and aggregate pipeline shapes
- limit or batch-size shape
- duration and request id
- returned count when available from the command reply
- safe profiler comment attribution such as Von service, operation, and route

`$queryStats`, profiler, explain-backed, and Atlas rows can additionally
include:

- `docsExamined`
- `keysExamined`
- `nReturned`
- scanned-per-returned ratios
- plan summary or winning index
- in-memory sort evidence
- query hash or plan cache key, when present
- Atlas total/average execution time, execution count, and P90/P99 latency

The diagnostics intentionally do not retain query values, update values,
returned documents, `.env` contents, Mongo URIs, credentials, prompts, email
bodies, or private document bodies.

## Running The Local Report

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
Advisor for the same time window, or use the `query_stats` source described
below. `$queryStats` counters are cumulative for the current MongoDB member and
are not a substitute for an exact Atlas time window.

## Running The Atlas Report

Use Atlas Admin API credentials with read-only project access. The command
supports service-account bearer tokens or digest public/private API keys.
Credential values may be supplied directly or through `_FILE` env vars:

- `ATLAS_ACCESS_TOKEN` / `ATLAS_ACCESS_TOKEN_FILE`
- `ATLAS_SERVICE_ACCOUNT_CLIENT_ID` /
  `ATLAS_SERVICE_ACCOUNT_CLIENT_ID_FILE`
- `ATLAS_SERVICE_ACCOUNT_CLIENT_SECRET` /
  `ATLAS_SERVICE_ACCOUNT_CLIENT_SECRET_FILE`
- `ATLAS_PUBLIC_KEY` / `ATLAS_PUBLIC_KEY_FILE`
- `ATLAS_PRIVATE_KEY` / `ATLAS_PRIVATE_KEY_FILE`

The command also accepts MongoDB-prefixed aliases such as
`MONGODB_ATLAS_GROUP_ID`, `MONGODB_ATLAS_CLUSTER_NAME`,
`MONGODB_ATLAS_PUBLIC_KEY`, and `MONGODB_ATLAS_PRIVATE_KEY`.

Basic JSON report:

```sh
.venv/bin/python scripts/atlas_query_insights_report.py \
  --group-id "$ATLAS_GROUP_ID" \
  --cluster-name "$ATLAS_CLUSTER_NAME" \
  --since 2026-06-05T00:00:00Z \
  --until 2026-06-05T01:00:00Z \
  --namespace von_db.workflow_use_episodes \
  --json
```

Include authorised query-shape metadata with literal values redacted:

```sh
.venv/bin/python scripts/atlas_query_insights_report.py \
  --namespace von_db.workflow_use_episodes \
  --command aggregate \
  --include-query-shapes \
  --include-shape-text \
  --json
```

Include Performance Advisor suggested-index summaries for known Atlas process
ids:

```sh
.venv/bin/python scripts/atlas_query_insights_report.py \
  --include-suggested-indexes \
  --process-id "$ATLAS_PROCESS_ID" \
  --json
```

The report emits typed blockers for missing project id, missing cluster name,
missing credentials, insufficient Atlas role, unavailable Query Shape Insights,
and parse/request failures. Missing credentials should produce a blocked report;
the script must not silently fall back to local Mongo telemetry.

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
- `command_name`: Atlas command or operation.
- `query_shape_hash`: Atlas query shape hash for cross-reference.
- `total_execution_time_ms`: Atlas total execution time for the window.
- `average_execution_time_ms`: Atlas average execution time.
- `execution_count`: executions observed by Atlas in the window.
- `p90_execution_time_ms` and `p99_execution_time_ms`: latency percentiles when
  Atlas returns them.
- `filter_shape`, `sort_shape`, `projection_shape`: structural query shape.
- `max_docs_examined_per_returned`: worst observed object scan ratio.
- `max_keys_examined_per_returned`: worst observed key scan ratio.
- `last_plan_summary` and `last_winning_index`: planner evidence.
- `sample_request_ids`: request ids, query hashes, or plan cache keys suitable
  for cross-reference.
- `recommended_next_step`: operator guidance for the next diagnostic action.
- `repo_index_comparison`: evidence-only signal that the collection appears in
  repo-owned index setup. This is not proof of full index coverage.

## Reconciling With Atlas

Atlas Query Insights and Performance Advisor use cluster-side telemetry. Von's
local command listener sees only operations from the current process, the
profiler report sees only what Mongo profiling captured for the selected
database and time window, and `$queryStats` exposes cumulative statistics for
the current MongoDB member. Member restarts, Atlas sampling, topology, and the
selected Atlas window can all produce different counts. Differences are
expected.

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

`scripts/atlas_query_insights_report.py` uses these endpoints only as read-only
diagnostics. It never creates or drops indexes, and it does not include slow-log
bodies in the redacted report.

## Running The Atlas Review Loop

`scripts/atlas_query_insights_review.py` compares two redacted Atlas reports and
groups query shapes as new, persistent, resolved, regressed, or improved. It can
also generate Jira-ready task payloads for high-impact candidates. The default
mode is dry-run; Jira writes require both `--apply-jira` and `--approved`.

Compare two saved reports:

```sh
.venv/bin/python scripts/atlas_query_insights_review.py \
  --previous-report .von/atlas_query_reviews/previous.json \
  --current-report .von/atlas_query_reviews/current.json \
  --json
```

Fetch a fresh Atlas report, compare it with the previous saved report, and
persist the current report for later review:

```sh
.venv/bin/python scripts/atlas_query_insights_review.py \
  --previous-report .von/atlas_query_reviews/current.json \
  --fetch-current \
  --group-id "$ATLAS_GROUP_ID" \
  --cluster-name "$ATLAS_CLUSTER_NAME" \
  --since 2026-06-05T00:00:00Z \
  --until 2026-06-05T01:00:00Z \
  --namespace von_db.workflow_use_episodes \
  --include-suggested-indexes \
  --process-id "$ATLAS_PROCESS_ID" \
  --store-current-report .von/atlas_query_reviews/current.json \
  --json
```

Use an external scheduler, cron, launchd, GitHub Actions runner, or Von
operator task to invoke the same command only when the schedule gate is enabled:

```sh
VON_ATLAS_QUERY_REVIEW_SCHEDULE_ENABLED=1 \
  .venv/bin/python scripts/atlas_query_insights_review.py \
  --schedule-enabled-only \
  --use-default-store \
  --fetch-current \
  --json
```

Dry-run output includes candidate Jira payloads. To create or update deduplicated
Jira tasks, use:

```sh
.venv/bin/python scripts/atlas_query_insights_review.py \
  --previous-report .von/atlas_query_reviews/previous.json \
  --current-report .von/atlas_query_reviews/current.json \
  --apply-jira \
  --approved \
  --json
```

Generated task descriptions include the Atlas time window, cluster, namespace,
command, query shape hash, redacted shape text when available, execution
counters, examined/returned ratios, Atlas suggested-index evidence, repo-owned
index evidence, and a recommended next diagnostic step. Generated tasks link
back to `JVNAUTOSCI-2427` through the payload and text. Duplicate suppression is
based on a stable query-shape fingerprint.

The review loop groups candidates into evidence categories such as:

- `missing_index`
- `likely_query_shape_index_mismatch`
- `expensive_regex_text_scan`
- `high_frequency_low_latency_hot_path`
- `aggregate_recomputation`
- `possible_data_model_issue`

These categories are review labels, not live mutation policy. They are intended
to help an operator choose the next investigation step.

After an index materialisation or query rewrite, such as the `JVNAUTOSCI-2426`
class of fix, run the review over a comparable post-deployment Atlas time window.
Treat shapes that move to `resolved` or `improved` as verification evidence, and
open or update tasks only for shapes that remain persistent or regress.

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

## Running Diagnostics Through Von

`JVNAUTOSCI-2450` exposes Mongo query-targeting diagnostics as the internal MCP
tool `mongo_query_diagnostics_report`. This is the conversation and
workflow-facing form of the in-process, profiler, and `$queryStats` reports; it
does not shell out to `scripts/mongo_query_targeting_report.py`.

The tool is read-only and requires an explicit operator gate:

```json
{
  "allow_operator_diagnostics": true,
  "source": "combined",
  "mongo_namespace": "von_db.workflow_instances",
  "min_millis": 0,
  "sample_limit": 200,
  "report_limit": 20,
  "explain_samples": 0,
  "explain_max_time_ms": 2000
}
```

Supported `source` values:

- `in_process`: current-process command-listener telemetry only.
- `profiler`: bounded `system.profile` samples, optionally with read-only
  `explain("executionStats")`.
- `query_stats`: bounded, server-ranked `$queryStats` counters for the
  configured database. The aggregation runs on `admin`, has a 5-second
  `maxTimeMS`, groups by query-shape hash, and returns at most the bounded sample
  limit plus one truncation sentinel before redaction.
- `combined`: all three surfaces, with a merged ranked summary.

`$queryStats` requires a compatible MongoDB deployment and the
`queryStatsRead` privilege (included in `clusterMonitor`). When it is
unsupported or unauthorised, the source returns a typed
`query_stats_unavailable` report and `combined` continues with the other
available sources. Von does not enable profiling or change MongoDB roles.

The `$queryStats` adapter retains only database/collection names,
field/operator shape strings, the query-shape hash, aggregate counters,
scanned/returned ratios, timestamps, and sort/disk flags. Raw query-shape
documents and literal values do not enter the returned report. A successful
collection replaces an in-memory redacted snapshot used by
`/api/settings/db/guardrails`; ordinary guardrail reads never issue a fresh
MongoDB diagnostic query. Run the maintenance workflow or operator diagnostic
again to refresh it, and inspect `snapshot_refresh` and
`latest_seen_at_utc` before treating the data as current. The first/latest
timestamps describe the selected server-ranked shapes, not every shape in the
MongoDB query-statistics store.

Use `mongo_namespace` for Mongo namespaces such as
`von_db.workflow_instances`. The normal Von `namespace` argument is accepted for
workflow/user-scope propagation, but it is not interpreted as a Mongo namespace
filter. This avoids mixing user/org tenancy scope with database collection
scope.

The tool returns:

- a merged ranked `summary`;
- per-source reports under `reports`;
- parameter bounds showing requested versus effective limits;
- `direct_index_mutation: false`;
- privacy metadata listing the omitted secret/private value classes.

Conversation-level operator prompts can ask Von to run the tool when diagnosing
Mongo/Vontology slowness. Recurring rumination or maintenance behaviour should
invoke the same tool through VWL, for example via `workflow_mcp.invoke_tool`,
and keep scheduling/escalation policy in the authored workflow rather than in
the Python tool. Generated recommendations are evidence and next diagnostic
steps only; index creation/drop remains explicit reviewed code or operator
work.

The Atlas review loop still requires Atlas Admin API configuration
(`ATLAS_GROUP_ID`/`MONGODB_ATLAS_GROUP_ID`, cluster name, and read-only Atlas
credentials). If those are absent, the Atlas CLI/tooling should fail closed with
a typed blocker such as `missing_project_id` rather than falling back to local
Mongo data.
