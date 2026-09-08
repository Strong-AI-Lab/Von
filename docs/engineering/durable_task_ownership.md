# Durable task ownership

Tracking: JVNAUTOSCI-2729; federation lifecycle planning: JVNAUTOSCI-2593.

## Contract

Background durable workers share the existing workflow-instance lease and
`claim_token`. The partial unique index `active_task_owner_unique` allows one
active owner for each `task_ownership_key`. Acquiring the lease and ownership
uses the same atomic document update. An expired lease is reclaimed on that
instance, retaining its checkpoint. A duplicate instance cannot bypass it.
An ordinary pause retains ownership. Successful completion releases ownership;
failed/cancelled owners without uncheckpointed effects can be released by the
next claimant with a conditional update. Conflicting candidates back off while
polling continues to unrelated work.

Keys include persisted user, organisation, and namespace. Identity preference
is input `task_concept_id`/`task_id`, then schedule ID, event idempotency key,
then instance ID. The key is persisted before input compaction. Producers must
use the same canonical task ID to coordinate different launches. Related work
without a shared identity is not automatically inferred to be the same task.
Child tasks that need independent background execution need distinct task IDs.

This serialises work. The existing event idempotency key still deduplicates a
logical occurrence at submission; sequential launches without such an identity
are not magically exactly-once. Supervised conversation mirror instances and
the separate chat prompt queue retain their existing coordination mechanisms.

## Actions and uncertain effects

The worker binds a trusted claim context alongside its authenticated actor.
ActionRegistry checks it before explicit/fallback actions. The MCP gateway
checks again inside the actual transport handler thread, after queueing. Tool
payloads cannot supply or override the claim. Ownership does not grant actor
authority.

Covered MCP mutations reserve an `uncheckpointed_effects` entry atomically
against the current, unexpired claim before invoking the handler. Entries carry
an effect ID, method, payload digest, time and bounded outcome metadata, not
email bodies or credentials. A returned result is observed; checkpointing
acknowledges observed effects atomically with workflow state. A handler that
raises, remains in flight after a timeout, or loses its process leaves an
uncertain entry. Recovery cannot silently replay that instance or release its
task to a duplicate. Reads do not create effect entries.

No lease can retract an external request already sent. Existing provider
idempotency and delivery receipts remain authoritative. A direct custom action
that bypasses MCP gets the entry ownership check but must retain its own
effect/idempotency contract; this change does not assert that arbitrary Python
side effects are transactionally fenced.

For uncertain effects, first drain the old execution (lease expiry alone does
not prove its handler stopped), inspect provider/canonical receipts, and decide
the correct continuation checkpoint. A trusted operator can call
`WorkflowInstanceManager.reconcile_claim_effects` with the exact old token,
effect IDs, evidence and verified checkpoint. It rejects an active lease and
changed effect inventory, records a reconciliation receipt and leaves the
instance manually paused for the existing authority-aware resume preflight.
Do not clear the marker merely to make a retry run. Ownership/effect metadata
is exposed with instance diagnostics.

## Legacy workers and activation

SC448086's observed July build replaces `claimed_by_build` on every claim and
does not advertise `durable_task_ownership_v1`. Database document validation
can reject those writes even though that code ignores new application checks.
Use `claim_admission_validator` with strict/error validation. The operator
script `scripts/durable_claim_admission.py` previews by default, preserves
existing validation rules, and records prior options for rollback. `--apply`
requires a database principal with `collMod`; Atlas Data Explorer's Validation
tab is also an administrator activation surface.

For a staged rollout, `--worker-pattern '^worker_SC448086_'` constrains only
that legacy host. Deploy capable workers before expanding to `^worker_`.
Verify the exact existing validator, then test a synthetic legacy
`findOneAndUpdate` and a compatible claimant. A database rejection (code 121),
not a heartbeat disappearing, is the acceptance evidence. The old process may
remain alive and continue heartbeat/scheduler writes. Check for already-running
claims separately; this rule cannot retract an action already started.

This is compatibility enforcement against the inspected implementation, not an
authentication boundary against a malicious database writer that forges build
metadata or has bypassDocumentValidation privilege. Broader admission-service,
per-node credentials and fleet upgrades remain separate federation work.
Keep completed one-off schedules disabled; do not send real email as a canary.

## Validation

`tests/backend/test_durable_task_ownership.py` exercises competing claims,
duplicate-instance fairness, scope separation, checkpoint takeover, stale
dispatch, uncertain effects and legacy-validator semantics. Use a real MongoDB
canary as well: mongomock does not implement server document validation.
Run affected durable-worker/executor and MCP gateway/transport regressions.
