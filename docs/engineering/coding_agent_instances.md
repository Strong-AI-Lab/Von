# Prepared coding-agent instances

This capability packages the existing Codex task/inbox worker for several
independently attributed instances on a prepared Linux host. It does not create
Unix accounts, enrol authentication, grant arbitrary host access, or certify
isolation between mutually untrusted workloads. Same-account workers share that
account's authority.

The callable surface is `coding_agent_instances`: `list`, `status`,
`reconcile`, `pause`, and `resume`. A normal Von agent calls it under its
authenticated user's organisation context. The user must still have live
`MANAGE_MEMBERS` permission in that exact organisation. Payload actor IDs are
not accepted. Separate coding-controller identities do not automatically acquire
that user's provisioning authority.

## Specification and host enrolment

An operator enrols concrete slots in the private JSON registry selected by
`VON_CODING_AGENT_REGISTRY`. It must be root/service-owned and not group/world
writable. Keep it and all credential references outside Git and model context.
One registry covers all approved host consumers; enrol existing identities as
`reserved: true` so another slot cannot duplicate them. Identity duplication
within the registry is rejected. Independent registries or manually launched
unenrolled workers are outside this topology guarantee.

Each `instances` entry has an exact stable `instance_id`, `agent_id`,
`agent_name`, `role`, `delegator_id`, `organisation_id`, `endpoint`,
`host`, `unix_account`, `allow_identity_creation`, and
`provisioner_command`. The command is an operator-approved argv, for example:

```json
{
  "instances": {
    "research-helper": {
      "instance_id": "research-helper",
      "agent_id": "#V#research_helper",
      "agent_name": "Research Helper",
      "role": "Coding tasks assigned by the enrolled researcher",
      "delegator_id": "#V#researcher",
      "organisation_id": "#V#research_lab",
      "endpoint": "https://von.example.invalid",
      "host": "prepared-host",
      "unix_account": "research-agent",
      "allow_identity_creation": true,
      "provisioner_command": ["/operator/fixed-research-helper"]
    }
  }
}
```

The fixed executable invokes the reviewed `scripts/codex_von_instance.py`
with its operator-chosen `--binding` path under the prepared account. Its stdin
contains only an action and optional model/reasoning settings. Bind remote SSH
or cross-account execution to that one executable/configuration; do not grant a
general root shell or let a caller supply argv/account/path flags. Access to the
helper itself must be restricted to the trusted service/operator. The host
independently checks its hostname, account and selector configuration, and its
receipt must match the server's binding.

The private host binding repeats the six identity/host/account fields and adds:

- `instance_root`: a dedicated private directory, never an existing worker root;
- `release_commit`: full reviewed SHA, available from the approved repository;
- `worker_config`: the existing worker's config, including the matching
  `agent_id`, `agent_name`, `delegator_id`, `organisation_id`,
  `source_repo`, `codex_command`, `python_environment`, and
  `state_root=<instance_root>/state`;
- existing task-source image delegation, repository authentication references,
  sandbox/profile and permitted deployment route, only where authorised;
- `systemd`: `unit_directory` for this account's user units,
  `memory_max_bytes`, optional `poll_interval_seconds` (default 300), and
  `environment_files` containing approved **paths**, never secret values;
- `capacity_enrolment_verified` and `resource_limits_verified`: operator
  attestations after checking enrolment and resource controls. These are
  prerequisites, not independent evidence of live aggregate safety.

Keep the canonical Von endpoint, actual database/environment binding, Unix
identity and Codex login separate. The operator must verify that the enrolled
environment reaches that endpoint's intended database before enabling work.
The existing worker uses canonical Python services, not an HTTP endpoint
parameter. Changing an endpoint string does not redirect that database.

The normal installer writes deterministic `von-coding-INSTANCE.service/timer`
units and refuses to overwrite a unit without its own instance marker. It
installs `MemoryMax`, disables swap for the service and polls on the configured
cadence. Alternatively an existing scheduler can be retained with fixed
`schedule_commands.enable/disable/status` argv bindings; status must return
JSON with boolean `enabled`. That adapter owns its verified resource ceiling.
User lingering, prepared Python dependencies, a functioning sandbox, per-account
Codex subscription authentication and repository credential enrolment remain
operator prerequisites for a new account.

## Normal use and recovery

1. Call `coding_agent_instances(action="list")` to discover only slots for the
   current authenticated delegator and organisation.
2. Call `reconcile` for an enrolled slot, optionally passing
   `settings={"model":"gpt-6-astra","reasoning_effort":"high"}`. Canonical concept
   and membership APIs create/reuse the preapproved identity; an unrelated
   existing concept cannot be repurposed. Task preferences still override these
   defaults. Tokens are preserved, not assumed to prove provider availability.
3. A new instance is installed **paused**. Reconcile retains an existing pause.
   Call `resume` explicitly to enable that instance's timer. Call `pause` to
   stop future polling without terminating an active run.
4. Inspect `status`: selected release/settings, schedule state and retained
   runtime evidence are separate. A runtime receipt can be historical. Actual
   readiness additionally requires canonical task pickup, outcome and intended
   recipient read-back; provisioning never asserts those.

Provisioning records its step before effects, serialises per-instance changes,
uses the existing coherent release installer, and holds the worker's existing
lock during reconfiguration. An active run returns `waiting_for_active_run`.
After an interrupted installation, reconcile the same slot. After uncertain
scheduling, inspect status then repeat the same pause/resume operation. It
targets the same unit, identity and state, not a new consumer. Do not remove
loaded releases or state to retry.

For upgrade/rollback, the operator selects a reviewed release SHA in the binding,
pauses the timer, waits for the active worker lock, and reconciles the same
instance. Its task/inbox state survives. Verify canonical runtime and recipient
receipts before claiming the new version is serving work.

## Aggregate capacity

Every participating worker config must contain the same `host_capacity`:

```json
{
  "lock_path": "/operator/host-capacity.lock",
  "reserve_bytes": 17179869184,
  "launch_headroom_bytes": 25769803776
}
```

Those example memory values are not a universal recommendation. Calibrate them
against the host and concurrent non-coding services. The operator creates one
stable regular inode, not replaceable by consumers, readable across prepared
accounts and not group/world writable. All consumers, including the existing
worker, must be enrolled before enabling another one. The admission module
serialises coding work host-wide and checks Linux `MemAvailable` while holding
the lock. It reports `waiting_for_capacity` without selecting a new task.
Coding children inherit the descriptor so controller death does not release
admission while the child still runs. Per-service memory ceilings remain
necessary; this lock alone does not constrain non-coding processes.

## Acceptance and limitations

On 17 September 2026, the normal actor-scoped tool provisioned a temporary,
independently attributed instance on the prepared DGX account. Repeated
reconciliation retained one identity and paused unit. Killing its host
provisioner after release preparation and before activation retained an honest
partial state; the same request recovered without another identity or unit.

The resource-bounded canary used the existing DGX worker's inherited lock as
its host admission inode. Its first scheduled poll reported
`waiting_for_capacity` without selecting a task while that worker was active.
After admission became available, one actual Astra/high execution consumed a
checksum-verified canonical attachment, produced and read back its artefact,
completed the canonical task, and sent its independently attributed result to
the intended recipient. Canonical start/end, partial token usage and unknown
cost were recorded; timing and message replay did not duplicate the result.
An unrelated private file remained unavailable, wrong-assignee task selection
was excluded, and another organisation's provisioning request was denied.
The temporary timer and service were removed afterwards; the identity, task,
artefact and receipts remain audit evidence.

This acceptance found and repaired two defects missed by initial mocks:
canonical missing-identity lookup raises `ConceptNotFoundError`, and
`EnvironmentFile` requires systemd path escaping rather than command quoting.
The installed unit's environment paths and 8 GiB memory ceiling were read back.
Targeted lifecycle, worker/inbox, capacity, release and timing checks also pass.

The test used the existing prepared Unix account, not a separate account.
Separate-account authentication and hostile-account isolation remain unverified;
shared-account execution is not a security sandbox between users. No permanent
additional consumer, production registry, or public deployment was activated.
Native task `#V#task_agent_002b293c463ed8e1e52df23cb68f9612` retains the
substantive acceptance receipt and its environment/revision provenance.
