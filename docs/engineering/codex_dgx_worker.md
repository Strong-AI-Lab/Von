# Subscription-backed Codex worker

- **Kind:** Operator runbook and bounded capability description
- **Lifecycle:** Pilot
- **Owner:** Michael Witbrock / SAIL
- **Last reviewed:** 11 September 2026
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

The worker checks hourly, starts at most one coding run per check, and sends a
pickup message followed by a result or question through Von direct messages.
An empty check makes no model request. Each task has its own Git worktree. Each
execution is a fresh subscription-backed Codex session, using the retained
worktree, task and previous result for continuity.

Answer a question in its task message thread or add a comment to the task.
The current Messages UI groups messages by participants: once it contains
several task threads it may omit a thread identifier on a general reply. In
that case use a task comment to keep the reply unambiguous. General inbox
messages do not create new coding assignments in this pilot.

Publishing uses an operator-configured GitHub account and follows each task's
publication authority. Missing authentication leaves changes retained and the
task blocked; it must not claim that local code is published. Deployment of
the public web server is outside this worker's execution profile. The chosen
GitHub account and current authentication status belong in the Jira decision
surface, not in the task's text.

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

Install the script and adjacent `codex_von_worker_prompt.md` from the same
reviewed revision. Run them with the project's PDM-managed Python environment.
A JSON config has these fields (paths are operator-selected):

```json
{
  "agent_id": "#V#codex_dgx",
  "delegator_id": "#V#michael_witbrock",
  "organisation_id": "#V#university_of_auckland_strong_ai_lab",
  "state_root": "/home/mjw/.codex-von-worker/worker-state",
  "source_repo": "/home/mjw/von-codex-runtime",
  "codex_command": "/home/mjw/.local/bin/von-codex",
  "model": "gpt-5.6-terra",
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

The DGX operator wrapper reads the existing web runtime's database credential
in-process, checks that the configured database target still matches the
public website, binds the worker config, and suppresses event-driven workflow
launches in that controller process. It does not alter the web process's
configuration. Store credentials, configuration and operational state outside
the repository with owner-only permissions. Never log the database URI.

On a host with an active cron service, install an hourly entry invoking the
wrapper and redirecting stdout/stderr to an owner-only log. An example schedule
is `17 * * * *`; preserve any other existing crontab entries. Run the exact
entry manually before installation. No Codex app session or SSH connection
needs to remain open. The controller uses a nonblocking file lock and passes
it to its child so concurrent invocations do not start overlapping work.

## Operation and evidence

On the pilot DGX, the wrapper is
`/home/mjw/.codex-von-worker/poll-von`. `worker-state/tasks/` records checkpoints;
`worker-state/runs/` contains input context, Codex JSONL events, the structured
result and exit receipt; `worker-state/worktrees/` retains coding results.
Keep these private: they can contain authorised conversation excerpts.

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
