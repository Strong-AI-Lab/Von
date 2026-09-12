# Subscription-backed Codex worker

- **Kind:** Operator runbook and bounded capability description
- **Lifecycle:** Pilot
- **Owner:** Michael Witbrock / SAIL
- **Last reviewed:** 12 September 2026
- **Decision surface:** [JVNAUTOSCI-2740](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2740)

## User workflow

Select SAIL in the public Von interface. Ask Von in the relevant conversation
to create a native task assigned to **Codex DGX** (`#V#codex_dgx`), with Michael
as its creator and report recipient, and link the source conversation. Invite
Codex DGX to that conversation when its context is needed. Assignment grants
access to the task; the conversation invitation grants access to the source
transcript separately. The current Tasks form's assignee picker offers only
Unassigned and Von; it does not yet offer this worker. The pilot acceptance
used labelled fixtures through canonical services, not that picker.

Native coding tasks can specify `requested_model` (an exact Codex model ID,
for example `gpt-6-astra`) and `requested_reasoning_effort` (for example `high`).
Ask Von to include those fields when creating the task or set them through
`task_update_fields`. Each omitted or cleared field independently inherits the
worker configuration. The worker's built-in defaults are Astra/medium; an
operator can configure a different permitted default. These are execution
preferences on the canonical task, not settings inferred from historical
conversation text. Changes apply at the next launch, not midway through a run.

The launch passes both resolved values explicitly to Codex and retains their
values and sources in `execution_settings` in the run context/checkpoint and
result message. Unsupported selections fail visibly through Codex; the worker
does not substitute another model or reasoning level. The standing Sol block
also applies to task overrides. Reply-only inbox runs use operator defaults
because task selection happens within that reply.

The worker checks at the operator-configured interval, starts at most one coding run per check, and sends a
pickup message followed by a result or question through Von direct messages.
An empty check makes no model request. Each task has its own Git worktree. Each
execution is a fresh subscription-backed Codex session, using the retained
worktree, task and previous result for continuity.

When the operator enables the inbox, the worker also answers direct messages
from its configured delegator in its organisation, including questions after
task completion. It uses the new message, recent participant conversation and
accessible canonical task records to identify the task, even when the Messages
UI omits a thread identifier. Status questions preserve the completed state and
record the question and answer as a task comment. A coding follow-up can reopen
the identified task; an ambiguous reference prompts a clarification. General
inbox messages do not create new coding assignments in this pilot.

Publishing uses an operator-configured GitHub account and follows each task's
publication authority. Missing authentication leaves changes retained and the
task blocked; it must not claim that local code is published. Deployment of
the public web server requires an explicit instruction in the initial task or
the current coding follow-up, and
the operator-enabled deployment command described below. The chosen
GitHub account and current authentication status belong in the Jira decision
surface, not in the task's text.

## Interactive coding-agent messages

The reporting requirement lives in [AGENTS.md](../../AGENTS.md#9-completion-and-maintenance).
Interactive Codex VS Code uses `#V#codex_vscode` (Codex VS Code); the DGX worker
uses `#V#codex_dgx` (Codex DGX). Keep these identities distinct. Michael's
recipient identity is `#V#michael_witbrock`; the existing SAIL organisation is
`#V#university_of_auckland_strong_ai_lab`.

Use an available canonical Von message tool or the existing trusted operator
helper for the intended Von instance. Absence of an exposed MCP message tool
does not establish that messaging is unavailable. A local development or
migration database can lack an identity that exists in the user's running Von;
check the configured delivery route before declaring the identity missing or
asking for account setup. Do not recreate identities or grant memberships to
repair a connection to the wrong instance.

The verified interactive helper accepts an operator-owned JSON file containing
`key`, `subject`, `content` and an optional native `task_id`. It binds the sender,
recipient and organisation itself, checks membership, calls
`message_service.create_message_idempotently`, and reads the result back as the
recipient. Use a unique key for each milestone or report; preserve both key and
exact payload when retrying the same delivery. A changed message needs a new
key. Include a task link when available; omit `task_id` if that native task has
not been verified in the destination instance.

Keep host aliases, installed helper/environment paths, credentials and delivery
receipts in operator-owned configuration outside Git. The operator's personal
`AGENTS.md` records the concrete route and recovery instructions. Invoke the
helper with its PDM-managed Python environment. Reporting alone must not start
the coding worker, a model request or unrelated event workflows. Keep normal
coding-interface updates as well as Von messages; the delivery receipt proves
message persistence, not completion of the work being reported.

For an explicitly requested recurring report, record the actual scheduler,
cadence, completion trigger, retry/idempotency state and host-availability
dependency. Verify an immediate delivery and the installed schedule separately.
Do not treat an imported task, a passive assignment or an enabled schedule as
proof of execution, or stop reports at an intermediate migration milestone.

## Controller and authority

[`scripts/codex_von_worker.py`](../../scripts/codex_von_worker.py) uses the
existing canonical task, membership, conversation and direct-message services.
An operator-owned config binds the actor, organisation, delegator, source
repository, state directory and Codex launcher before processing any task.
The worker selects tasks assigned to that actor, created by that delegator,
in that organisation, and reporting to that delegator or with no explicit
report recipient. Retrieved task or model text cannot change these selectors.
For project-backed tasks, the live project writer must be `von`. Standalone
Jira imports are also excluded. An assignment with another tracking authority
receives a message explaining the unsupported route; its imported state is
left unchanged. This pilot does not become a second Jira writer.

Conversation concept identities remain owner-private. When access-control
redaction hides a task's source relation, the adapter matches only actual
invitations addressed to its actor against an access-controlled existence query
on that task. It accepts pending invitations only from the configured delegator
in the configured organisation. The canonical transcript handler still checks
membership and accepted sharing. Missing context is reported explicitly; the
model decides whether the task text alone is adequate.

The controller writes task progress/evidence and sends idempotent direct
messages, reading effects back before recording delivery. It freezes the result
message intent before sending, so an interrupted report can be reconciled.
An unsuccessful process, absent exit receipt or invalid structured result is
blocked rather than completed. Reassignment/cancellation discovered after a run
prevents the worker from changing the task state. Its result still goes to the
original configured delegator. This is a single-host worker, not a distributed
claim/lease protocol.

When the initial task explicitly requests deployment, the coding run returns
the full merged `deploy_commit` after its code, tests and publication finish.
The controller rechecks the live task authority and instructions, then calls
only its operator-configured `deployment_command`. The run cannot supply a
host, checkout, shell command or credentials. An empty deployment request has
no deployment effect. Interpretation of the initial task belongs to the model;
there is no keyword matcher or second per-deployment confirmation.

The Codex process defaults to `workspace-write` sandboxing and an explicit non-Sol
model. Publication installations use the scoped profile below. Its environment
omits the controller's Mongo credentials and unrelated
API keys. Its MCP connection is disabled during assigned executions; selected
task/conversation context is supplied in a local file. The operational prompt
is a versioned input for this external Codex worker, not a duplicate of a
Vontology-governed production prompt. This pilot uses the trusted SAIL local
operator profile; it is not a hardened boundary against a hostile host user.

## Installation

Use a dedicated Codex home with ChatGPT subscription login, an explicit
non-Sol model, and a working native Linux sandbox. Keep the API-key Codex home
separate. The launcher should unset `OPENAI_API_KEY` and `CODEX_API_KEY`, set
the dedicated `CODEX_HOME`, and execute the installed official Codex CLI.
Validate an actual agent-issued shell command before enabling polling.

Install the worker, `codex_von_inbox.py`, and both adjacent `_prompt.md` files
from the same reviewed revision. Run them with the project's PDM-managed Python environment.
When these scripts are copied into a release bundle outside a complete checkout,
pass `--backend-root /path/to/reviewed/Von` to the worker. This selects the
canonical backend before any backend imports; a missing backend or previously
loaded imports from another checkout fail with a restart instruction. A checkout
installation defaults to the checkout containing the worker script.

The selected backend must include task execution-preference hydration on both
point and list reads. The adapter requires `requested_model` and
`requested_reasoning_effort` keys even when their values are null. An older reader
that omits them fails visibly before pickup instead of silently choosing defaults.
Installing only a newer worker script cannot repair an older canonical reader.
Do not change global model defaults to compensate for a mismatched installation.

For an authorised upgrade, use the coherent installation below. Preserve the
existing worker lock, state directory, identities, memberships, task selectors
and five-minute schedule. A selected release is not evidence of actual pickup.
Local installation and public deployment remain distinct effects; both need
applicable task or standing authority.

### Coherent worker/backend releases

`scripts/codex_von_release.py` prepares a complete detached checkout of one
reviewed revision with independent Git objects. It copies only tracked source,
including backend, worker, inbox, prompts and deployment helpers. It does not
copy credentials, environment files or private configuration. Installations
reside in an operator-owned `worker_release_root/<full SHA>`; `current` is an
atomically replaced symlink. Never modify or remove a release used by an active
process. The existing PDM environment remains operator-owned; a changed
dependency lock still requires recoverable environment preparation.

One-time operator bootstrap (outside a coding sandbox):

1. Retain the existing poll/deployment wrappers and their backend/bundle
   bindings for rollback. Let the active controller finish. The installer
   refuses to overlap its existing `state_root/worker.lock`.
2. Add `worker_release_root` to the existing private deployment configuration;
   keep its other bindings unchanged. Using the existing PDM Python, run the
   reviewed `scripts/codex_von_release.py --config <deployment-config> --commit
   <full-origin-main-SHA> --receipt <private-installation-receipt>`. This selects
   source only; it does not start a worker, restart the web server or touch Von
   records. The receipt retains the previous pointer if one exists. At the
   first bootstrap, rollback also requires the retained pre-bootstrap wrappers.
3. Retarget the existing `poll-von` wrapper to resolve `current` **once per
   invocation, before importing any backend modules**. Use that resolved
   directory for both `scripts/codex_von_worker.py` and `--backend-root`.
   Retain the wrapper's database-target binding, event suppression, configured
   identity, subscription launcher, model defaults and state. Retarget the
   existing fixed deployment launcher to the same selected release's
   `scripts/codex_von_deploy.py`. It must forward the controller's
   `--worker-lock-fd`, `--commit`, `--receipt` and optional `--recover-only`;
   preserve that descriptor through `exec` or explicit `pass_fds` if the wrapper
   starts a subprocess. The configuration path remains operator-bound. Install
   this pair together.
4. Through the same prepared poll wrapper, run the worker with
   `--check-task <assigned-native-task-id>` under its normal actor context.
   Permit that diagnostic option explicitly in the wrapper. This checks
   canonical point/list reads, `inputs` and exact resolved task preferences;
   it prints only provenance/settings, does not accept conversation invitations,
   send messages, change task state or invoke a model. A completed task is
   reported as ineligible, not picked up. Check an isolated native assignment
   with task-sourced Astra/high and no project/collection requirement.
5. Retain the diagnostic output and inspect the next existing scheduled
   invocation's `state_root/runtime.json`: observed time, PID, backend revision,
   worker path and actual task-service module path must match the selection.
   The runtime receipt proves controller startup, not task pickup or completion.

With this bootstrap and `worker_release_root` enabled, each authorised public
deployment prepares the complete worker release before stopping the server,
verifies local/public health, then selects the worker release for the next
invocation. The controller passes its existing lock descriptor to deployment;
standalone operator deployment acquires that same lock. Loaded files remain
intact. The deployment receipt retains both predecessor code revisions and
worker selection. Interrupted activation reconciles the pointer; failed
deployment attempts restore the prior worker selection and web service,
reporting failures separately. Pointer selection is recorded as
`selected_for_next_invocation`; verify actual startup and task settings as
above before claiming local capability activation. No new poller is installed.

Ordinary conversation `task_create` permits explicit assignment to a represented
same-organisation coding agent using the continuation route's live membership
checks. It verifies creator, organisation, assignee, report recipient, execution
preferences and source link on canonical read-back. A standalone native task does
not need a project or collection for worker eligibility. Eligibility and creation
do not prove actual pickup. Unsupported `parent_task_concept_id` on creation is
rejected before writing; the separately authorised `task_create_subtask` and
`task_set_parent` routes remain the hierarchy surfaces. When an update recovers
only some requested fields, the outcome report retains the task link and verified
fields alongside the unresolved ones.

A JSON config has these fields (paths are operator-selected):

```json
{
  "agent_id": "#V#codex_dgx",
  "delegator_id": "#V#michael_witbrock",
  "organisation_id": "#V#university_of_auckland_strong_ai_lab",
  "state_root": "/home/mjw/.codex-von-worker/worker-state",
  "source_repo": "/home/mjw/von-codex-runtime",
  "codex_command": "/home/mjw/.local/bin/von-codex",
  "model": "gpt-6-astra",
  "model_reasoning_effort": "medium",
  "permission_profile": "von-coding",
  "github_command": "/home/mjw/.local/bin/gh",
  "git_author_name": "Your configured commit author",
  "git_author_email": "your-verified-email@example.org",
  "python_environment": "/home/mjw/Von/.venv"
}
```

The optional `python_environment` provides the existing PDM environment via a
worktree symlink. Dependency changes needing writes outside the sandbox may
require operator preparation. Do not replace `pdm.lock` or synchronise a Von
project with another dependency manager.

Inbox pickup is opt-in with `inbox_enabled: true` and an explicit ISO timestamp
in `inbox_since`. Select the cutoff after inspecting outstanding messages; do
not replay old setup messages unintentionally. Reply runs use a fresh read-only
Codex process under the same controller lock. They return an answer and a
semantic choice to reply or resume an identified task; the controller rechecks
authority, records the Q/A note, and delivers an idempotent reply. The process
does not execute coding or deployment work. Resumed coding starts on a later
poll and uses the current follow-up as its instructions. An old deployment
instruction does not authorise deployment of newly requested work.
Several follow-ups queued before a coding run retain all their instructions
and source-message identities, rather than replacing one another.

For publication, authenticate GitHub CLI in a dedicated `GH_CONFIG_DIR` outside
the repository, and set that directory in both the controller and Codex launcher.
Keep other host accounts separate. The adapter configures each new clone's Git
credential helper to use `github_command`; credentials remain in the dedicated
CLI store. New task checkouts have independent Git metadata and borrow source
objects read-only. Keep the source repository available; to remove that
dependency before retiring it, repack the retained clones first.

Ordinary workspace sandboxing protects `.git`, preventing commits. A publishing
installation may permit the isolated checkout's own metadata with this profile:

```toml
default_permissions = "von-coding"

[permissions.von-coding]
extends = ":workspace"
[permissions.von-coding.filesystem.":workspace_roots"]
".git" = "write"
[permissions.von-coding.network]
enabled = true
```

Remove legacy `sandbox_mode` and `[sandbox_workspace_write]` settings from that
dedicated Codex home before selecting `permission_profile` in worker config;
otherwise those older settings take precedence. Other workspace protections
remain inherited. Validate both Git writes inside the isolated checkout and
write denial outside it. Legacy linked worktrees retain their original Git
restrictions; preserve their work and migrate them explicitly if publication
is required. Never grant writes to the live web checkout's shared `.git` as a
shortcut. See [Codex permissions](https://learn.chatgpt.com/docs/permissions).

## Explicit deployment

Install `codex_von_deploy.py`, `codex_von_release.py` and `deploy_local_main.py`
from the same revision, preferably through the coherent release selection above.
A fixed launcher binds a separate JSON configuration and accepts `--commit`,
`--receipt`, `--worker-lock-fd` and optional `--recover-only` from the controller. Set its path as
`deployment_command` in worker config. The deployment configuration contains:

```json
{
  "primary_root": "/home/mjw/Von",
  "runtime_root": "/home/mjw/Von-runtime-main",
  "state_root": "/home/mjw/.codex-von-worker/worker-state",
  "worker_release_root": "/home/mjw/.codex-von-worker/releases",
  "health_url": "http://127.0.0.1:5000/health",
  "public_health_url": "https://von.curiouscat.cc/health",
  "public_headers_file": "/home/mjw/.codex-von-worker/cloudflare-health.json",
  "health_timeout_seconds": 960
}
```

Prepare the dedicated detached runtime worktree with its environment before
activation. The command uses the canonical launcher, requires the requested
SHA to remain `origin/main`, and verifies the exact clean revision locally and
through the public endpoint. Persistent receipts make retries reconcile an
interrupted deployment. Failed startup restores the previous code revision
and verifies the recovered service; a recovery failure remains an explicit
blocked outcome. This is code recovery, not a database rollback. A changed
`pdm.lock` requires preparation of a recoverable environment before automatic
deployment. Migrations and infrastructure changes need their own task scope
and recovery plan.

For a Cloudflare Access-protected site, keep the two service-token headers in
the private `public_headers_file`. The deployment command injects only the
service Client ID into `VON_CLOUDFLARE_HEALTH_SERVICE_IDS` for the server.
Cloudflare must issue the token for the configured application's Service Auth
policy. Von validates its signature, issuer, audience, expiry and exact
configured service identity for `GET /health` only; it never creates a Von user
session. Human login and private routes retain their existing checks. Health
requests do not follow redirects, so credentials cannot be forwarded to a
login provider. Token expiry belongs in the operator's installation record.
See [Cloudflare service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/).

The DGX operator wrapper reads the existing web runtime's database credential
in-process, checks that the configured database target still matches the
public website, binds the worker config, and suppresses event-driven workflow
launches in that controller process. It does not alter the web process's
configuration. Store credentials, configuration and operational state outside
the repository with owner-only permissions. Never log the database URI.

On a host with an active cron service, install a five-minute entry invoking the
wrapper and redirecting stdout/stderr to an owner-only log. An example schedule
is `*/5 * * * *`; preserve any other existing crontab entries. Run the exact
entry manually before installation. No Codex app session or SSH connection
needs to remain open. The controller uses a nonblocking file lock and passes
it to its child so concurrent invocations do not start overlapping work.

## Operation and evidence

On the pilot DGX, the wrapper is
`/home/mjw/.codex-von-worker/poll-von`. `worker-state/tasks/` records checkpoints;
`worker-state/runs/` contains input context, Codex JSONL events, the structured
result and exit receipt; `worker-state/worktrees/` retains coding results.
Keep these private: they can contain authorised conversation excerpts.

`worker-state/inbox/` retains reply context, process receipts and reporting
checkpoints. `worker-state/followups/` binds resumed task instructions to their
source message. Interrupted reporting reconciles the existing task comment and
message rather than repeating delivery. Empty inbox and task checks make no
model request. The five-minute poll skips while either kind of run holds the
controller lock.

To pause pickup, remove only the tagged worker crontab entry. To retry a failed
run, inspect its retained work and set the Von task back to pending. Do not
delete worktrees to force a retry. A full host crash preserves files and makes
the next poll report an incomplete run; it does not automatically repeat
potentially consequential code-side effects. If the controller cannot reach
Von, its error remains in the operator log and reconciliation waits for a
later check. There is no separate out-of-band outage alert in this pilot.

Acceptance is a task-to-code-to-message result on the actual DGX, including
question/reply continuation and source context that is unavailable before an
invitation. The targeted tests cover empty polls, wrong task scope, failed
processes/results, report replay, reassignment, invitation lookup, and Git
preparation failure. Use the Jira decision surface for current installation,
runtime receipts and remaining publication work; a repository merge alone does
not prove that a scheduler or public runtime was activated.

## Task-linked coding activity and archives (JVNAUTOSCI-2751)

The reviewed controller can register a separate run identity before execution,
then publish complete JSONL records in bounded batches during its existing wait
loop. The task inspector's **Coding run activity** button opens private run
history, paged records, capture freshness/error state and the final download.
Refresh is explicit. Historical content is inert text, never executable HTML or
instructions to another model. Changing the browser actor or organisation closes
the view.

`task_run_archive_service` owns operational run records in `task_run_archives`
and immutable content-addressed objects through the existing canonical blob
store. This is a run primitive, not another task writer, workflow, scheduler or
telemetry platform. It does not replace the current work product or existing
task evidence. Final ZIP attachments link through the private run download;
the ordinary organisation-visible attachment upload is deliberately not used
for transcript bytes. A reassigned, cancelled or unavailable task receives no
new attachment, but the original private run record retains its task backlink.

The controller's trusted actor and organisation determine the producer. The
original worker/requester audience is intersected with source-conversation
access, then rechecked on reads, including after invitation revocation. Task
visibility alone does not grant archive access. The HTTP routes are read-only:

- `GET /api/tasks/<task>/runs?offset=0` lists up to 50 runs;
- `GET /api/tasks/<task>/runs/<attempt>?cursor=0` reads one activity batch;
- `GET /api/tasks/<task>/runs/<attempt>/download` returns the verified ZIP.

The source cursor counts original bytes, independently of redacted/exported
bytes. Retry identities include the run, start/end cursor and source/export
hashes. A partial final line waits until complete while the process is running;
an interrupted terminal tail is retained as an explicitly malformed record.
Finalisation reconciles the complete available event stream, source segment
hashes, exported file sizes/hashes, event count, thread identity, outcome,
missing files, redactions and exposed-summary availability. A digest-checked
archive reference is required before the controller reports normal completion.
Coding results remain intact when capture fails: `archive_pending` is retried
on the existing controller's next opportunity, with a separate pending report.
No coding effects are repeated to repair archive storage.

Only `events.jsonl`, `context.json`, `schema.json`, `result.json`, `exit.json`,
`stderr.log`, `launch-error.json`, `deployment.json` and `deployment.log` from
the selected run are eligible. Local originals remain untouched. Credential
patterns and known controller environment credentials are redacted with
counts; opaque provider state is omitted. These are **available execution
records**, not a comprehensive private reasoning transcript. No additional
model call or summaries setting is introduced. Provider-exposed reasoning items
are retained when emitted; an empty exec stream does not prove that no private
reasoning occurred. Source rollouts are not collected because this route has no
supported bounded rollout export configured; it never searches other sessions
or authentication files. Legacy backfill containing a transcript without a
stable source session locator fails explicitly rather than guessing its scope.

The capture interval is 15 seconds while the child runs, with at most four
roughly 512 KiB batches per live checkpoint. Limits are 16 MiB per record/batch,
128 MiB per companion file, and 256 MiB per run archive. Exceeding the bounded
route leaves finalisation pending and preserves local originals; it does not
silently truncate them. The UI displays at most 16,000 characters per record;
the downloadable redacted record is not shortened by that display limit.
`source_complete` is separate from a successfully stored archive: a failed or
interrupted run can have a durable archive whose manifest identifies missing
source files. Controller and web runtime must resolve the same private canonical
blob storage; a local receipt alone does not prove cross-process availability.

For an explicitly selected historical task/run pair, the existing operator-bound
controller accepts `--backfill-run 'TASK_CONCEPT_ID=ATTEMPT'` (repeat for at most
20 pairs). It checks the exact context task/worker, shares the existing worker
lock, does not launch Codex, and keeps a retry receipt in `archive-backfills/`.
The ordinary next tick retries only those selected pending receipts. Do not
scan the estate, remove originals, or run backfill against an active worker.

Candidate acceptance is covered by `tests/backend/test_task_run_archives.py`:
a deterministic executable traverses the actual `launch()` loop, exposes
activity through the canonical Flask route before exit, then verifies the final
ZIP through that route as the requester. Other cases cover partial lines,
large outputs, corruption, redaction, source revocation, reassignment and
interrupted finalisation. The 1,000-event fixture performs three run-record
writes (registration, one batch, final reference), independently of event count.
`tests/browser/taskRunActivity.cjs` checks desktop/mobile rendering, inert source
text, refresh, scoped download and clearing on scope change using an isolated
browser fixture. These tests do not prove live scheduler activation, public
OAuth, actual provider summaries, or production historical backfill.
