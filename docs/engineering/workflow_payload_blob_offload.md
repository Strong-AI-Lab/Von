# Workflow Payload Blob Offload

## Purpose

Durable workflow records can grow quickly because `workflow_instances` carries
checkpoint state and terminal outputs, while `workflow_executions` carries trace
actions, steps, and transitions.  The generic Von blob store now supports moving
oversized workflow payload leaves out of MongoDB while leaving compact,
verifiable blob references inline.

This is separate from debug payload offload.  Debug offload covers
`chat_history` and `turn_execution_records`; workflow payload offload covers the
durable workflow persistence collections.

## Runtime Behaviour

- `WorkflowInstanceManager.create_instance()` compacts oversized `inputs`.
- `WorkflowInstanceManager.checkpoint()` compacts oversized `workflow_data`.
- `WorkflowInstanceManager.mark_completed()` and `mark_failed()` compact
  oversized terminal `outputs`.
- `insert_workflow_execution_trace()` compacts oversized trace fields before
  inserting into `workflow_executions`.
- `get_instance()` and workflow trace readers hydrate workflow blob references
  before returning full records.

The compacted inline reference uses schema version
`workflow_payload_blob_ref.v1`.  Each blob is gzipped JSON and stores raw and
compressed SHA-256 metadata for verification.

## Configuration

The runtime uses the existing Von blob-store configuration, for example Catalyst
Swift via `VON_BLOB_STORE_BACKEND=swift`, `VON_SWIFT_CONTAINER`, and OpenStack
`OS_*` credentials.

If `VON_SWIFT_S3_FAILOVER_ENABLE` is enabled and S3-compatible Catalyst
credentials are present, writes may return blob references with `backend: s3`
even when the configured primary backend is `swift`.  Workflow hydration uses
the backend recorded in the blob reference for readback, so verified migrations
continue to work when Swift authentication fails over to S3-compatible access.
For one-off backfills on Catalyst, it is also acceptable to set
`VON_BLOB_STORE_BACKEND=s3` for the migration command when Swift authentication
is intermittently failing but S3-compatible read/write probes succeed.

Workflow-specific threshold:

- `VON_WORKFLOW_PAYLOAD_BLOB_THRESHOLD_BYTES` defaults to `32768`.

S3-compatible Catalyst access also supports bounded network controls:

- `VON_S3_CONNECT_TIMEOUT_SECONDS` defaults to `10`.
- `VON_S3_READ_TIMEOUT_SECONDS` defaults to `30`.
- `VON_S3_MAX_ATTEMPTS` defaults to `3`.

These keys are registered in startup `.env` override handling.

## Migration

Use `scripts/offload_workflow_payloads_to_blob.py`.  Dry-run is the default.

Recommended first pass:

```powershell
pdm run python scripts/offload_workflow_payloads_to_blob.py --limit 25
```

Apply a narrow first batch after confirming the blob backend is correct:

```powershell
pdm run python scripts/offload_workflow_payloads_to_blob.py --apply --collection workflow_instances --status completed --limit 25
```

For large live collections, prefer an unmigrated-only backfill window so each
successful pass removes already-inspected records from the next scan:

```powershell
pdm run python scripts/offload_workflow_payloads_to_blob.py --collection workflow_instances --status completed --limit 100 --batch-size 1 --unmigrated-only
pdm run python scripts/offload_workflow_payloads_to_blob.py --apply --collection workflow_instances --status completed --limit 100 --batch-size 1 --unmigrated-only --mark-inspected-skips --progress-every 10
```

`--batch-size 1` keeps MongoDB `getMore` responses small for bloated documents.
`--mark-inspected-skips` marks below-threshold documents during apply mode so
`--unmigrated-only` scans keep advancing.  If a window stalls on network or blob
backend behaviour, rerun a smaller `--limit`; blob-verified Mongo updates are
idempotent and each document is patched only after round-trip verification.

Then widen gradually only after repeated clean windows:

```powershell
pdm run python scripts/offload_workflow_payloads_to_blob.py --apply --collection workflow_instances --limit 500
pdm run python scripts/offload_workflow_payloads_to_blob.py --apply --collection workflow_executions --limit 500
```

Safety properties:

- dry-run does not initialise or write to the blob store;
- apply mode writes blobs before touching MongoDB;
- apply mode prints bounded progress every 25 updated documents by default;
- apply mode can mark inspected below-threshold records with
  `workflow_payload_offload_migration.status = skipped_no_inline_candidates`;
- every new workflow blob reference is hydrated and SHA-256 checked against the
  original canonical JSON payload before MongoDB is patched;
- MongoDB updates use exact `$set` field patches;
- already-offloaded fields are skipped, so reruns are idempotent;
- if a blob write or verification fails, that Mongo document is left unchanged.

The migration does not delete workflow records or change retention.  Retention
for failed, cancelled, stuck running, and stale pending instances remains a
separate policy decision.