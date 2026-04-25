# Debug Payload Blob Offload

## Purpose

Von chat turns can produce large diagnostic payloads, especially when tool
results contain full external API responses.  `llm_debug_data` and projected
`turn_execution_records` are valuable forensic evidence, but storing every
large nested payload inline in MongoDB can exhaust small Atlas tiers.

The support surface in `src/backend/services/debug_payload_store.py` keeps
compact summaries and counters in MongoDB while moving oversized debug leaves to
the configured Von blob store (`local`, Catalyst Swift, or S3-compatible
storage).  Diagnostic readers hydrate blob references on demand, so the normal
debug-entry tools still return the original payload when the blob is available.

## Runtime Behaviour

- `chat_history_service.add_message_to_history()` compacts oversized
  `llm_debug_data` before storing assistant messages.
- `/von/generate` compacts oversized stored tool-message content before adding
  tool messages to chat history.
- `turn_execution_record_service.upsert_turn_execution_record_projection()`
  compacts oversized projected turn-execution records.
- `get_chat_history_debug_entry()` and turn-execution diagnostics loading
  recursively hydrate debug blob references before returning diagnostic data.

Thresholds are environment-controlled:

- `VON_DEBUG_PAYLOAD_BLOB_THRESHOLD_BYTES` defaults to `32768`.
- `VON_DEBUG_TOOL_MESSAGE_BLOB_THRESHOLD_BYTES` defaults to `4096`.

These keys are registered in startup `.env` override handling.

## Migration

Use `scripts/offload_debug_payloads_to_blob.py`.  Dry-run is the default.

Recommended sequence:

```powershell
pdm run python scripts/offload_debug_payloads_to_blob.py --limit 25
```

If the candidate count and configured blob backend look right, narrow the first
apply run to a known session or request:

```powershell
pdm run python scripts/offload_debug_payloads_to_blob.py --apply --session-id <session-id>
```

Then run the broader apply:

```powershell
pdm run python scripts/offload_debug_payloads_to_blob.py --apply
```

Safety properties:

- the script writes blobs before touching MongoDB;
- every new blob reference is read back and SHA-256 checked against the original
  canonical JSON payload;
- MongoDB updates use exact dotted `$set` paths instead of replacing full
  history arrays;
- historical payloads are never replaced with degraded summaries during
  migration;
- the script is idempotent and skips already-offloaded fields on later runs.

If an error is reported, the affected Mongo document has not been migrated for
that field.  Fix the blob-store configuration or network issue, then rerun the
same command.
