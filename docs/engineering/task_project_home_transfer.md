# Task-project home transfer

This bounded operational slice of the federation programme (JVNAUTOSCI-2615)
keeps MongoDB and transfers selected native-writer projects. It does not replace
the broader federation plan or implement general database replication.

## Contract and authority

`writer=von` chooses the tracking application. `task_project_homes` separately
records the physical write/execution home, epoch, route and state. Private peer
configuration admits exact projects and audiences; verified identity mappings
are independent of subscriptions. Task IDs, original authors, Jira aliases,
comments/history/relationships, project membership and source file bytes survive.
Accounts, memberships, login bindings, dispatch receipts and execution state do
not travel with a snapshot. Current canonical assignment creates local dispatch
authority; a copied assignment cannot start a coding agent.

Canonical task, concept and text-relation mutations obey the project barrier.
The durable worker checks the home before claiming and again before actions.
The claim-admission validator can require `task_project_home_v1` to exclude older
workers. This does not constrain arbitrary administrator database writes.
Before first registration, identify and drain all previous writers and in-flight
claims; update active consumers and fence legacy worker claims. An in-flight
external action is not cancelled merely by changing a home record.

## Transfer order

Use `scripts/transfer_task_project.py` under the explicitly selected environment
with a private federation config, `VON_TASK_HOME_NODE_ID`, actor and original
organisation scope. The script is an operator CLI, not a public authority API.
Each output is owner-only and should be retained with the task's audit evidence.

1. Inventory the exact projects, audiences, identities, collision histories,
   active execution and source/destination recovery evidence. Verify old records
   by Jira source identity and history retention, not concept slug or date alone.
2. Deploy home-aware canonical services and consumers. Require the worker
   capability at the shared claim boundary after capable workers are available.
3. `register` the source home with the evidence file. `freeze` the exact epoch
   only after writers/claims have drained and every retained operation has been
   reconciled. Failed writes retain operation tokens; elapsed time is not proof
   their effects stopped.
4. `capture` a fresh signed snapshot while frozen. An active/preview snapshot
   cannot be published. Transfer the snapshot and private hash-named file cache
   through an authenticated operator channel. Keep credentials on their hosts.
5. At the destination, `preflight` verifies identities/history and records exact
   current concept and text digests. `stage-files` verifies all file hashes and
   sizes before and after destination storage. A missing/corrupt file stops the
   transfer without publishing concepts.
6. `publish` rechecks the preflight inside one MongoDB transaction, retaining the
   prior complete view if interrupted. Transactions require a replica set; a
   standalone server is rejected. The complete snapshot and receipt become
   visible together with a **prepared**, non-writable, non-executing home.
7. Canonically read back project/task text, relationships, membership, aliases,
   audiences and file bytes. Obtain the signed destination `receipt`.
8. At the source, `release` compares a fresh frozen capture with that receipt,
   moves the route to a read-only replica and returns a signed release.
9. At the destination, `activate` requires the exact signed source release,
   publication receipt, digest and epoch. Only now can canonical writes and
   deliberate assignment proceed. Verify public search, native create/edit and
   an explicitly assigned harmless coding-agent canary through actual result.
10. Repeat for the remaining admitted projects in their own scopes. Report
    per-project completion and outstanding exceptions, not only pilot success.

## Interruption and recovery

Before publication, retry capture/staging after inspecting the retained state.
An interrupted transaction leaves the previous destination view intact. A
committed publication can be replayed idempotently; its receipt is returned
without overwriting later edits. Preflight rejects concurrent destination text
or concept changes and requires a new reconciliation.

Between publication and release, both homes remain non-writable. Retry the
exact receipt after diagnosing the failure. Between release and activation,
the source remains a read-only replica; retry the exact signed release. There
is no timeout-based ownership takeover. Stale epochs cannot reactivate a prior
home. After activation, recover the current destination in place, retaining
all subsequent writes. Returning ownership is a new explicitly reconciled
transfer of the latest state; restoring the old source snapshot is prohibited.
The current CLI does not automate a reverse transfer into an existing home.

Mongo recovery must be tested in a separate authenticated database. A live dump
is not a whole-database point-in-time snapshot. Preserve the existing data
volume and take a cold copy before changing a standalone server's topology.
A topology rollback must retain the latest data volume, not replace it with a
pre-transfer backup after new work has been accepted. Keep blob bytes with the
paired database recovery evidence.

## Validation boundary

Targeted tests cover audience/signature denial, retained uncertain operations,
write/claim fencing, actor-bound dispatch, signed handoff, stale-epoch rejection,
and real-Mongo transaction interruption/retry with later-write preservation.
Real project rehearsal and public/canonical read-back are required for a live
transfer claim. Fixture home certificates are labelled fixture-only and never
establish that a production source was released.
