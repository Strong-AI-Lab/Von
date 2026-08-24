# Operational Engineering Guide for Coding Agents

- **Kind:** Routed operational runbook
- **Lifecycle:** Active
- **Authority:** Canonical for recurring local engineering procedures; subordinate
  to `AGENTS.md` and the scoped design guides
- **Created:** 2026-04-04
- **Last substantive content update:** 2026-08-01
- **Last reviewed:** 2026-08-01
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

### 3.2 Deploy and start Von through the repository launcher

For the normal local deployed instance, run this from the clean primary
checkout on `main`:

```sh
./run.sh deploy-main
```

This is the canonical local deployment operation. It fetches `origin/main`,
fast-forwards a clean primary `main`, checks out the exact commit detached in
the dedicated sibling `Von-runtime-main` worktree, runs that release's explicit
startup-seed reconciliation from the runtime checkout, restarts the runtime,
and verifies the running commit, clean-build marker, startup-seed receipts,
durable workflow services, and both background indexing workers. The
reconciliation is a trusted local operator action that may write canonical
Vontology state; ordinary Flask startup only checks its derived receipts. The
deployment refuses a dirty, divergent, wrong-branch, unrelated runtime
checkout, failed reconciliation, or unavailable startup materialisation. Set
`VON_RUNTIME_WORKTREE` or pass `-RuntimeWorktree <path>` only when the runtime
worktree intentionally lives elsewhere.

The primary checkout owns the local `main` branch. The runtime worktree is
deliberately detached at the deployed commit; it must not maintain a second
long-lived local branch. A detached runtime is therefore expected, whereas a
detached primary checkout is not.

Use a direct restart only when the runtime checkout is already intentionally
selected and no Git deployment is required:

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

To reconcile the same release inputs explicitly without deploying or starting
a server, run this from the checkout whose derived receipt path the eventual
runtime will use:

```sh
.venv/bin/python scripts/reconcile_startup_seed_materialisations.py
```

Use `--family concept-summary-fields` or
`--family publication-scope-profiles` for a bounded repair. A zero exit status
means the command applied any missing canonical state, performed a subsequent
unchanged verification when needed, and published a current dependency
receipt. This command is not a startup workaround and must not be scheduled on
every restart.

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

### 4.3 Do not fabricate unavailable authority

If a required Vontology, workflow, prompt, or credential surface is unavailable,
do not invent authority, expose private data, or perform an effect beyond the
delegated capability. Deny that affected effect with a typed explanation.

Do not turn the absence of a preferred represented path into a universal
failure. Preserve safe reads, direct-tool or manual alternatives, partial
progress, and bounded recoverable action when those paths retain the same
authority and user outcome. A stale prompt body or guessed ontology term must
not silently impersonate live authority, but neither should a broken wrapper
erase an otherwise authorised simpler path.

## 5. Canonical Tool and Access Paths

### 5.1 Vontology, workflows, and prompts

Use Vontology APIs, MCP tools, or canonical services for data deliberately
governed there. Resolve candidate concepts and predicates before creating new
ones, and read back a write through the selected canonical surface.

When a workflow or represented prompt has actually been selected as authority:

1. inspect the current represented artefact;
2. inspect the context and telemetry the relevant model call actually saw;
3. change represented policy there when the surface can express it;
4. keep generic execution and task policy on the smallest suitable surfaces.

This does not require every behaviour change to acquire a represented artefact.
A direct tool, function, model call, or small composition may be the correct
canonical path.

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

- re-read the live issue and follow its actual decision authority;
- distinguish minimum ship criteria from stop-ship conditions, non-blocking
  measurements or limitations, and explicit non-goals as required by
  `AGENTS.md`;
- use a task branch when the work is substantial;
- keep comments, assignee, links, and status aligned with reality where doing
  so helps coordination;
- publish, merge, release, comment, or transition only as the current task
  authorises; and
- stop when evidence is ready for a stated human gate or when the current
  decision authority ends. Implementation completion is not an instruction to
  close the issue.

Do not require Jira ceremony for an unrelated local inspection or small
non-ticketed documentation change.

When creating an issue, assign it to the authenticated user by default unless
directed otherwise or Jira rejects the assignment. Do not open speculative
refactor issues based on line count alone; record a concrete capability,
coherence, authority, reliability, or testability problem.

For a substantial task, write acceptance criteria as the minimum evidence that
would support the intended delivery decision. Put desirable optimisation,
research questions, additional model arms, broader task families, and
non-critical telemetry in a visibly non-blocking section unless the task names
the material consequence that makes one of them a release gate. Do not turn a
high-quality task description into a conjunctive programme of every useful
measurement.

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
- Tier 1: bounded behaviour; targeted tests and the exact or nearest faithful
  path when user-visible, with enough telemetry to identify the affected path.
- Tier 2: materially consequential or cross-boundary capability; Tier 1 plus
  canonical effect read-back and the highest material residual risk.
- Tier 3: security, authority release, certification, or an explicit
  comparative research claim.

Escalate only when evidence reveals a candidate-caused material risk that could
change the delivery decision. Do not start with a browser, full-server replay,
prompt family, or release campaign unless the claim requires it.

### 6.2 Make new evidence change a decision

Before the first implementation change on substantial work, record the current
merge, release, or handoff decision and the minimum evidence that would make it
ready. Then, before adding a patch, model arm, prompt family, telemetry surface,
or delay because of a material new observation, write one plain-language
sentence in the existing working record stating whether:

- `Merge decision changes — ...`; or
- `Merge decision does not change — ...`.

These are human-readable examples, not machine fields. Do not introduce a
separate form, schema, CI check, or runtime gate for them.

Use the equivalent delivery term when there is no merge. A changed decision
must identify a candidate-caused material regression, an invalidated claim, or
evidence that the current result cannot be trusted. A decision that does not
change may justify a note or a later prioritised task, but not expansion of the
current release boundary. A small cleanup already inside the stated scope may
still be completed without becoming another ship criterion.

Gather more evidence only while a plausible result could change delivery,
rollback, or the scope of the claim. Stop when the selected tier supports the
claim and another check would merely search for unrelated defects or improve
confidence without changing the decision.

### 6.3 Start narrow, then test the real boundary

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

### 6.4 Frontend validation

For changed JavaScript, run the repository's static/lint gate and the targeted
frontend tests. Use browser validation when DOM state, rendering, interaction,
authentication, live progress, or the authenticated user view is part of the
claim. A screenshot alone is not evidence that an interaction worked.

### 6.5 Timeouts and optional live services

Bound calls to providers, browsers, MCP servers, and live external services.
Separate deterministic tests from opt-in live acceptance. A timeout or
unavailable external dependency should produce a clear limitation, not an
indefinite wait or a false pass.

### 6.6 Acceptance record

Record only the evidence needed to support the claim:

- exact command or path;
- pass/fail/typed-blocker result;
- relevant identifiers or artefact digest;
- material limitations;
- the current merge or equivalent delivery decision, including only material
  observations that changed it; and
- the user-visible answer, canonical effect read-back, or path evidence only
  when the acceptance claim depends on it.

Do not claim broader coverage than was run.

## 7. Recurring Diagnosis Patterns

### 7.1 Represented behaviour looks wrong

Inspect, in order:

1. the actual selected authority or execution surface, whether direct
   tool/function, prompt, workflow, profile, or predicate;
2. the evidence and capabilities available at the decision point;
3. the model's actual context and selected action where a model was involved;
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
