# Operational Engineering Guide for Coding Agents

- **Kind:** Routed operational runbook
- **Lifecycle:** Active
- **Authority:** Canonical for recurring local engineering procedures; subordinate
  to `AGENTS.md` and the scoped design guides
- **Created:** 2026-04-04
- **Last substantive content update:** 2026-07-18
- **Last reviewed:** 2026-07-18
- **Freshness boundary:** Revalidate host-, credential-, launcher-, and
  connector-specific facts before relying on them

## 1. Purpose and Use

Use this guide when work involves local service startup, shell or host
behaviour, credentials, Jira/GitHub/Vontology access, test selection, replay
tooling, or process cleanup. It is not required reading for an unrelated
documentation or local code task.

This guide contains recurring procedures, not incident transcripts. A one-off
failure belongs in a Jira issue, replay record, dated evidence note, or Git
history unless it establishes a durable invariant.

Authority order:

1. `AGENTS.md` for repo-wide constraints.
2. `docs/design_index.md` for document status and routing.
3. The scoped design or validation guide for the affected surface.
4. This guide for practical execution.
5. Historical notes and old Jira evidence for diagnosis only.

## 2. Operational Posture

- Prefer the simplest canonical path that can establish the required claim.
- Treat broken Von control surfaces as product evidence; do not create a shadow
  authority path merely to bypass them.
- Distinguish a product defect from a host, transport, credential, or stale
  process problem before changing code.
- Keep probes bounded and inspectable.
- Do not print secrets, clobber `.env`, or mutate Vontology through direct
  database access.
- Preserve unrelated worktree changes. Do not use `git stash` as routine
  workspace management.
- Match evidence cost to the change and claim using the validation tiers in
  `AGENTS.md`.

## 3. Shell, Host, and Process Hygiene

### 3.1 Use the host-native shell

On macOS and Linux, use the native POSIX shell for normal probes and the
repository's `run.sh`. On Windows, use PowerShell and `run.ps1`. Use
PowerShell on macOS/Linux only for a `.ps1` script or an intentional
cross-platform check.

Prefer:

- `rg` for searches and file discovery;
- bounded `sed`, `Get-Content`, or `Select-Object` reads;
- explicit working directories and timeouts for automation;
- repo launchers over hand-assembled server commands.

If a simple read stalls, retry a smaller probe. If that also stalls, inspect the
tool host or transport before blaming repository code.

### 3.2 Start Von through the repository launcher

Normal local restart:

```sh
./run.sh restart -NoBrowser -HealthTimeoutSec 180
```

```powershell
.\run.ps1 restart -NoBrowser -HealthTimeoutSec 180
```

Isolated agent-test restart:

```sh
./run.sh restart -AgentTest -NoBrowser -HealthTimeoutSec 180
```

```powershell
.\run.ps1 restart -AgentTest -NoBrowser -HealthTimeoutSec 180
```

Agent-test replay tools normally target `http://127.0.0.1:5010` and should
verify that `/health` reports `agent_test_instance=true`.

### 3.3 Clean up repeated local helpers

Before starting another server, browser replay, or MCP-heavy batch after
several retries, inspect for:

- duplicate Von listeners;
- stale Playwright/browser daemons and temporary profiles;
- detached MCP stdio helpers;
- abandoned debug probes.

Stop only processes clearly spawned by the work and no longer in use. Do not
solve stale-process pressure by continually choosing new ports. Recheck
listeners and memory after cleanup.

## 4. Environment and Credentials

### 4.1 Protect configuration and secrets

- Never overwrite `.env` wholesale.
- Do not echo tokens, signing keys, cookies, connection strings, or credential
  payloads into logs.
- Prefer environment variables, OS credential stores, and existing repo
  helpers.
- Treat shell-, IDE-, service-, and automation-process environments as distinct.
  A credential available in one is not proof it reaches another.
- When certificates matter, inspect the effective process environment rather
  than assuming global configuration was inherited.

### 4.2 PATH and dependency changes are scope-sensitive

Determine whether a missing executable affects the current shell, the launcher,
the IDE, an automation checkout, or a service process. Make the narrowest
durable correction. Do not add global PATH entries to repair one isolated
process without confirming that global scope is intended.

For an isolated checkout or automation, install dependencies through the
repository's declared environment and record any bootstrap requirement in the
automation definition rather than relying on a previously warmed machine.

### 4.3 Fail closed at authority boundaries

If a required Vontology, workflow, prompt, or credential surface is unavailable,
return a typed and diagnosable failure. Do not silently use stale prompt bodies,
repo snapshots, guessed ontology terms, or heuristic policy as a substitute.

## 5. Canonical Tool and Access Paths

### 5.1 Vontology, workflows, and prompts

Use Vontology APIs, MCP tools, or canonical services for governed data. Resolve
candidate concepts and predicates before creating new ones, and read back a
write through the canonical surface.

For workflow or prompt behaviour:

1. inspect the current represented artefact;
2. inspect the context and telemetry the relevant model stage actually saw;
3. change represented policy there when the surface can express it;
4. add Python only for a named reusable execution, validation, telemetry,
   persistence, or integration primitive.

Repo seeds and snapshots are bootstrap or test material, not production
authority.

### 5.2 Durable manuals and documents inside Von

When a user asks to store a manual, guide, or runbook *inside Von*, use
Vontology document/blob authoring rather than treating a repo Markdown file as
completion:

1. resolve the appropriate document/manual type and canonical link predicate;
2. store the content through the Von file-copy/blob path;
3. create or reuse the described concept;
4. link the concept and blob using the canonical predicate;
5. index and read back the stored file-copy;
6. create the requested review/follow-up task.

Do not claim completion from a local file alone.

### 5.3 Jira

For substantial Jira-backed implementation:

- create or switch to a task branch;
- move the issue to `In Progress`;
- name the intended authority surface and validation claim;
- keep comments, assignee, links, and status aligned with reality;
- after implementation, continue through targeted validation, commit,
  merge/push, remote read-back, closure comment, transition, and Jira read-back
  unless the user explicitly asks to pause.

Do not require Jira ceremony for an unrelated local inspection or small
non-ticketed documentation change.

When creating an issue, assign it to the authenticated user by default unless
directed otherwise or Jira rejects the assignment. Do not open speculative
refactor issues based on line count alone; record a concrete capability,
coherence, authority, reliability, or testability problem.

### 5.4 Git and GitHub

- Inspect `git status --short --branch` before editing.
- Branch for substantial work.
- Compare with `origin/main` when another worktree or agent may have landed
  overlapping changes.
- Preserve user changes and avoid destructive reset/checkout operations.
- Commit a coherent scope with an intentional message.
- Push and verify the exact remote ref before reporting publication.
- After merge, remove only branches or worktrees known to be disposable.

Authentication, account identity, network policy, and sandbox permissions are
environment facts. Revalidate them at the time of publication. Do not copy a
past incident's credential recipe into a new environment and treat it as
authority.

### 5.5 Conversation and execution evidence

Use the canonical conversation, execution, and telemetry tools rather than
direct database reads. Preserve identifiers such as request, session,
conversation, call, and execution ids when they are needed to join evidence.
Treat telemetry as evidence about a run, not as the user-facing product.

## 6. Testing and Acceptance

### 6.1 Select the tier before selecting tools

Use the validation tiers in `AGENTS.md`:

- Tier 0: static/doc/format validation.
- Tier 1: targeted automated tests and nearest faithful path.
- Tier 2: exact user-visible replay with relevant telemetry.
- Tier 3: broader evaluation, release, migration, or safety evidence.

Escalate when evidence reveals greater risk. Do not start with a browser,
full-server replay, prompt family, or release campaign unless the claim requires
it.

### 6.2 Start narrow, then test the real boundary

Run the smallest relevant test first, then expand to the nearest integration
boundary. A unit test can establish a local invariant; it cannot establish a
route, workflow, browser, authentication, or provider claim it did not execute.

Useful selection aids include:

```sh
pdm run python scripts/testing/recommend_pytest_lane.py <changed-paths>
pdm run pytest <targeted-test-path-or-node-id> -q
```

For isolated captured LLM exchanges:

```sh
pdm run python scripts/replay_llm_exchange.py --help
```

That replay can compare prompts, providers, and timing, but it does not execute
tools or workflows and cannot prove a user-visible turn is fixed.

For a live behaviour claim, follow
`docs/engineering/real_path_server_replay_and_telemetry_loop.md`. For a
browser-specific claim, also follow
`docs/engineering/frontend_browser_user_view_validation.md`.

### 6.3 Frontend validation

For changed JavaScript, run the repository's static/lint gate and the targeted
frontend tests. Use browser validation when DOM state, rendering, interaction,
authentication, live progress, or the authenticated user view is part of the
claim. A screenshot alone is not evidence that an interaction worked.

### 6.4 Timeouts and optional live services

Bound calls to providers, browsers, MCP servers, and live external services.
Separate deterministic tests from opt-in live acceptance. A timeout or
unavailable external dependency should produce a clear limitation, not an
indefinite wait or a false pass.

### 6.5 Acceptance record

Record only the evidence needed to support the claim:

- exact command or path;
- pass/fail/typed-blocker result;
- relevant identifiers or artefact digest;
- material limitations;
- user-visible answer and telemetry reconciliation for Tier 2/3 behaviour work.

Do not claim broader coverage than was run.

## 7. Recurring Diagnosis Patterns

### 7.1 Represented behaviour looks wrong

Inspect, in order:

1. the workflow/prompt/profile/predicate that should own the behaviour;
2. discovery and candidate-generation evidence;
3. the model's actual stage context and selected action;
4. execution/tool results and persisted evidence;
5. final answer composition.

Symptom location is not fix location. A bad fallback string in Python does not
prove that user-facing recovery policy belongs there.

### 7.2 Direct helper passes but the product path fails

Compare the two paths for inputs, namespace, auth, context construction,
materialised artefacts, routing, model/provider selection, persistence, and
timeouts. Promote the narrow test to the actual failing boundary rather than
adding a special-case bypass.

### 7.3 Auth works in one process only

Compare effective environment, account, credential store, certificate settings,
working directory, and launcher inheritance. Repair the intended process path;
do not expose credentials for convenience.

### 7.4 A workaround is becoming policy

Stop when a temporary script, branch table, registry case, lexical score, or
fallback begins deciding user-facing semantics. Identify whether the real
missing piece is a represented artefact or a reusable primitive, implement it
at that layer, and delete or explicitly time-bound the workaround.

## 8. Maintaining This Guide

Add material here only when it is:

- recurring across tasks or environments;
- operational rather than product policy;
- concise enough to apply without replaying an old incident;
- paired with a freshness boundary where facts can drift.

Prefer an invariant, decision rule, and minimal current command over a long
transcript. Delete superseded instructions. Preserve dated incident evidence in
Jira, certification artefacts, or Git history rather than letting this runbook
grow monotonically.
