# Operational Engineering Guide for Coding Agents

**Status**: Living practical guidance  
**Date**: 2026-04-04

## 1. Purpose

This document is the operational companion to `AGENTS.md`.

Use it for practical engineering guidance that is too implementation-shaped,
environment-shaped, or tool-shaped to belong in the core constitutional rules.
It collects durable learnings from real Von work: shell behaviour, local
environment handling, access/auth friction, testing practice, MCP tool usage,
and debugging habits that repeatedly matter in day-to-day implementation.

Keep `AGENTS.md` short and constitutional. Put practical, repeat-encounter
engineering lessons here unless they rise to the level of repo-wide doctrine.

## 2. When to Read It

Read this guide when a task is likely to involve any of the following:

- local environment setup or service startup
- shell behaviour, command hangs, or host/tooling quirks
- Jira, GitHub, Vontology, workflow MCP, or auth/access friction
- pytest planning, timeout avoidance, or acceptance-path validation
- diagnosing why a real tool path behaves differently from a direct unit test

For background and authority questions, still defer to:

- `AGENTS.md`
- `docs/engineering/security_considerations.md`
- the situation-specific engineering documents named in `AGENTS.md`

If the current operational docs are still insufficient for an unusual
implementation or debugging problem, you may consult
`docs/engineering/historical_agent_guidance_notes.md` for historical context.
Treat it as non-authoritative and prefer current docs where they differ.

## 3. Operational Posture

- Prefer canonical control surfaces over ad-hoc workarounds.
- Treat failures in Von's own tooling paths as product bugs, not as permission
  to create shadow pathways.
- Prefer bounded, inspectable, PowerShell-first commands.
- If a problem appears to be transport, host, or tool-session related, fix or
  restart that layer rather than debugging the repository blindly.
- When a workflow/prompt/Vontology dependency fails, fix that dependency path
  where possible rather than patching symptoms with local heuristics.

## 4. Shell and Host Defaults

### 4.1 PowerShell first

Von's default shell is PowerShell. Prefer:

- `$env:VAR = 'value'`
- `$var = (Get-Content file.txt)`
- here-strings for larger inline snippets

Avoid Bash-only syntax unless the user explicitly asks for Bash.

### 4.2 Keep commands bounded

Prefer bounded reads and targeted searches:

- `Get-Content -TotalCount`
- `Select-Object -First`
- `rg` with a specific path or pattern

Do not default to broad recursive reads when a targeted command will do.

### 4.3 Treat unexplained hangs as operational signals

If a simple read-style command stalls for roughly 30 seconds with no useful
output, suspect tool transport or host-session trouble before assuming the repo
is at fault.

Good first responses:

1. Retry with a smaller bounded command.
2. Use an explicit timeout for the next run.
3. If the minimal command is still oddly slow in the agent session, reload the
   IDE window or restart the relevant MCP/tool host.

Quick sanity probes:

- `Get-Content <path> -TotalCount 5`
- `rg <pattern> <path>`
- `git status --short`

Do not launch multiple long file reads in parallel when diagnosing hangs. A
small, fast probe is more informative than repeating the same broad command.

### 4.4 Do not over-trust host labels

The visible UI label is not always the true execution host or shell. When host
differences matter for debugging, inspect them conservatively and record the
finding in diagnostics or task notes.

### 4.5 Keep ad-hoc PowerShell probes boring

- Do not serialise arbitrary PowerShell object graphs with `ConvertTo-Json`
  inside temporary debug probes. That path can recurse into provider-backed or
  otherwise surprising objects and create host-destabilising memory blow-ups.
- For launcher-path diagnostics, prefer repo-supported probes that emit plain
  text or bounded scalar/string-list fields. If you need the current workflow
  purity status, use `scripts/powershell/invoke_workflow_purity_status_probe.ps1`
  rather than inventing a new `%TEMP%\ps_stage_probe_*` script.
- If stale probe processes or temp dirs do accumulate, use
  `python scripts/cleanup_stale_powershell_probes.py --min-age-seconds 0`
  to terminate matching `probe.ps1` processes and remove their temp
  directories.

## 5. Environment and Credential Handling

- `.env` is the authoritative local source for credentials and service-critical
  configuration unless a stronger deployment mechanism is intentionally in use.
- Never print secrets or dump `.env`.
- When code depends on a credential or service-critical environment variable,
  register the key in `_apply_dotenv_overrides()` in
  `src/workflows/von/main.py`.
- Do not rely on inherited parent-shell values for well-known keys such as
  `GITHUB_TOKEN`; IDEs, CI, and host tooling often override them.
- Assume `.env` edits do not affect already-running Von processes.
- After changing credential values in `.env`, restart the relevant Von process
  so startup-time overrides are applied again.
- In documentation and examples, use explicit placeholders such as
  `<YOUR-CLIENT-SECRET-HERE>` rather than token-like sample strings that may
  trigger scanners.

For minimum local and hosted environment sets, see
`docs/engineering/environment_minimums.md`.

### 5.1 PATH changes are scope-sensitive

- Treat PATH behaviour as scope-sensitive, not magical. A PowerShell script can
  change `$env:Path` for its own process, but if it is invoked with
  `& .\script.ps1` those session-local changes do not flow back into the
  already-open caller shell.
- `[Environment]::SetEnvironmentVariable(..., "User")` and
  `[Environment]::SetEnvironmentVariable(..., "Machine")` affect future shells,
  not the already-running agent session or the current integrated terminal.
- For repo-scoped developer tooling that should be available in fresh workspace
  terminals, prefer a repo-controlled terminal environment surface such as
  `.vscode/settings.json` `terminal.integrated.env.windows`, or an explicit
  activation script that the caller intentionally dot-sources into the current
  session.
- Bootstrap/install scripts may still update user or machine PATH, but they
  should also either:
  - update the repo-scoped terminal config for future workspace terminals, or
  - state clearly that a new shell must be opened before `Get-Command` will
    succeed.
- Do not claim that a tool is now "on PATH" for the current session unless you
  verified it in that same session with a direct probe such as
  `Get-Command latexmk`.
- When wrappers can reliably discover tools by absolute install location, keep
  that fallback for resilience; however, treat "wrapper works but PATH is still
  stale" as an operational defect worth fixing, not as proof that the PATH
  problem is solved.

## 6. Preferred Tool and Access Pathways

### 6.1 Vontology and workflow behaviour

- Use Vontology API, MCP tools, or canonical service pathways for
  Vontology-governed data.
- Do not introduce direct DB access for Vontology-governed state.
- Prefer workflow MCP tools as the default control surface for workflow
  behaviour when the behaviour can be represented there.

### 6.2 Jira

- Prefer Von's internal Jira path when Atlassian MCP OAuth is unreliable.
- Use the Atlassian recovery runbook rather than handwritten REST workarounds.
- If the Jira pathway is broken, improve the canonical path or document the gap
  instead of normalising ad-hoc bypasses.
- When updating Jira descriptions through MCP, do not rely on wiki-style
  pseudo-markup such as `h2.`, `* item`, or ad-hoc plain-text headings unless
  the tool explicitly documents that format. The safe default is Atlassian
  document structure (`type: "doc"` with real `heading`, `paragraph`,
  `bulletList`, and `orderedList` nodes).
- After substantial Jira description rewrites, do a quick read-back check to
  confirm the stored payload contains structured heading/list nodes rather than
  flattened paragraph text. Treat bad Jira rendering as a tooling-path defect to
  correct, not as cosmetic noise to ignore.
- When creating Jira issues on the user's behalf, assign them to the
  authenticated Jira user by default unless the user explicitly asks for a
  different assignee or Jira refuses the assignment.
- Once Jira work is clearly in scope for the current request, do not impose
  extra human-attention cost for low-risk Jira hygiene. By default, go ahead
  and perform routine housekeeping such as:
  - transitioning the current issue to the appropriate in-progress or done
    state
  - adding concise progress or closure comments that reflect actual work
  - setting the obvious parent epic when the fit is clear from current context
  - adding or updating straightforward issue links between clearly related
    tasks created or discussed in the same thread
- Ask before Jira mutations that materially change planning intent or ownership,
  such as rewording issue scope, changing assignee away from the authenticated
  user, reprioritising, bulk-editing many issues, or creating uncertain links.

Jira MCP failure checkpoint:

1. Stop further Jira writes after bounded retries.
2. Record what issue work succeeded, what remains, and what state is now
   uncertain.
3. Use the recovery runbook rather than inventing a substitute write path.
4. Resume only after a minimal health check confirms the canonical path is
   working again.

See:

- `docs/engineering/atlassian_mcp_recovery_runbook.md`
- `docs/engineering/jira_components_taxonomy.md`

### 6.3 Conversation-turn context authority

- Treat the accumulated turn context as a shared runtime object, not as a thin
  prompt fragment rebuilt independently for each stage.
- Default to generous shared context for selector, planner, tool-use, and
  response stages. If a stage needs extra instructions or evidence, add them.
  If a stage truly needs a reduced context, justify and validate that
  reduction explicitly.
- Do not confuse debug metadata such as concept references, prompt skeletons,
  or stored descriptors with the actual messages sent to the model.
- When a stage adds or removes context, emit machine-readable lineage telemetry
  that captures the base context summary, the stage-local delta, and the final
  effective context.
- If behaviour differs between a direct-response path and a supervised
  workflow-turn path, inspect whether they are building different effective
  contexts before patching routing or answer logic.

### 6.4 Conversation-scoped evidence tools

- For conversation-scoped MCP read tools, do not expose raw `session_id`
  fields in model-facing `mcp_access` descriptors when an authoritative
  server-bound reference can be emitted instead.
- Prefer signed or otherwise server-bound `conversation_ref` and
  `history_location_ref` descriptors emitted by authoritative services over
  model-authored raw conversation identifiers.
- Keep the authority split clean:
  - workflow, prompt, and Vontology decide whether evidence is needed
  - code provides only integrity surfaces such as binding, verification,
    scope checking, and fail-closed resolution
- In turn-execution payloads, keep `request_id` and `chat_session_id`
  distinct. Do not alias `request_id` into `session_id` for convenience.
- When changing model-facing MCP descriptors, update the canonical surface
  registry and regenerate `src/backend/mcp_server/vontology_mcp.json` so the
  stdio surface, manifest, and handler contracts stay in sync.

### 6.5 Scheduled monitoring workflows

- When adding recurring monitoring or regression detection for workflow or turn
  surfaces, prefer the full authority pattern:
  - a Vontology-defined workflow published from a repo seed bundle
  - a managed schedule bootstrap service that only ensures schedule presence and
    configuration
  - startup integration in `utils_flask._start_durable_workflow_system()`
  - targeted tests for publication, startup bootstrap, and real workflow
    execution via gateway-backed fallback actions
- Keep Python responsible only for schedule bootstrap, runtime plumbing, and
  fail-closed enforcement. Do not encode the monitoring policy itself in Python
  cron-like logic if a durable workflow can express it.

### 6.6 GitHub

- Prefer Von's internal GitHub MCP proxy and its guardrails for GitHub access.
- Keep write behaviour fail-closed and allow-list aware.

See:

- `docs/engineering/github_internal_mcp_runbook.md`

### 6.7 Vontology and MCP field notes

- Prefer `upsert_singleton_text_relation` for canonical singleton text
  predicates rather than repeatedly appending parallel values.
- After creating or updating workflow concepts, run a quick integrity check with
  `concept_exists` and `get_text_relations_summary`.
- If an internal MCP tool fails in a way that suggests a product defect, treat
  that as a Von bug to document and fix, not merely as session-local friction.

## 7. Testing and Acceptance Practice

### 7.1 Start narrow, but real

- Start with targeted impacted validation.
- Use `docs/engineering/pytest_lane_strategy.md` as the canonical reference for
  lane planning and broader aggregate coverage.
- For targeted planning, prefer:
  `pdm run python scripts/pytest_lanes.py recommend --git-diff origin/main`
- Use aggregate lanes only when broader coverage is actually needed.
- Do not claim a full pytest run unless the relevant aggregate lanes were run.
- Default to small sequential pytest batches for slower or integration-heavy
  areas.
- For slow integration-heavy areas, start with one file per invocation and split
  further by class or test selection as soon as a run stalls or times out.

### 7.2 Test the real call path

When changing MCP tools or handlers, do not stop at direct handler tests.
Exercise the real gateway path, especially through
`InternalMCPGateway.invoke()`, so schema enforcement, error shaping, and
early-return behaviour are tested on the actual surface that callers use.

### 7.3 Acceptance claims need direct evidence

- Do not claim broader coverage than you actually ran.
- Do not claim end-to-end acceptance from nearby unit tests alone.
- If a task's intended outcome includes Vontology/workflow/KB state changes,
  verify those authoritative changes were actually materialised, not merely that
  repo-side support code was merged.

### 7.4 Frontend static JS gate

For the browser-side modules under:

- `src/frontend/web/von_interface/static/js/**/*.js`
- `tests/frontend/**/*.js`

do not use `pyright` as a pre-commit or changed-files gate. `pyright` is a
Python type checker and produces parser/configuration noise rather than
actionable diagnostics on these files.

Use this repo's JS gate instead:

- changed-file check:
  `npm run lint:frontend:static -- <changed static-js files>`

The wrapper filters mixed changed-file lists down to the supported browser-side
JS paths and avoids falling back to a noisy repository-wide sweep. Do not treat
bare `npm run lint:frontend:static` as a clean whole-tree gate while the wider
static JS surface still carries unrelated lint debt.

For user-visible behaviour changes, pair that lint pass with a focused Jest
run, for example:

- `npx jest src/frontend/web/von_interface/static/js/test/chatTab.test.js --runInBand`

This keeps frontend validation file-local and avoids repository-wide parser
failures unrelated to the changed JS module.

### 7.5 Browser user-view validation

For frontend tasks whose real acceptance depends on rendered UI state, do not
stop at static tests alone. Use the browser validation guidance in:

- `docs/engineering/frontend_browser_user_view_validation.md`

In particular, prefer authenticated user-view validation over anonymous-mode
checks when the important surface is Messages, saved conversations, invites, or
other user-scoped UI.

For local browser acceptance work, the implemented pseudouser path from
`JVNAUTOSCI-1747` is now the preferred entry point:

- enable `VON_BROWSER_TEST_AUTH_ENABLED=1` locally;
- restart Von;
- use the Settings-tab `Browser Test Login` control on a `localhost` /
  `127.0.0.1` session to establish the representative user-view fixture.
- before trying to log in, check the Settings authentication area for the
  browser-test mode status line. It now reports whether the feature is
  available, disabled, or blocked by non-localhost conditions, and it shows the
  configured pseudouser identity that will be used.

### 7.6 Env-gated live arXiv acceptance

- The paper-representation workflow now has an env-gated live acceptance lane in
  `tests/backend/test_paper_representation_workflow_vontology_service.py`.
- Use `VON_RUN_LIVE_ARXIV_WORKFLOW_ACCEPTANCE=1` for the single-paper smoke path.
- Use `VON_LIVE_ARXIV_ACCEPTANCE_PAPER` to override the single-paper case.
  The default is `2505.14396`, matching the current Jira-guided acceptance
  paper for `JVNAUTOSCI-1799`.
- Use `VON_RUN_LIVE_ARXIV_WORKFLOW_ACCEPTANCE_BATCH=1` for the representative
  batch path, and `VON_LIVE_ARXIV_ACCEPTANCE_SAMPLE` to override the default
  sample list.
- Keep the live lane nearest-real-path and fail-closed:
  - force `VON_BLOB_STORE_BACKEND=local` inside the test lane
  - disable event-workflow integration for the lane
  - use the workflow-driven fixture, verification, and cleanup helpers rather
    than ad-hoc setup/teardown code

### 7.7 Real-path server replay and telemetry loop

For user-visible route, selector, workflow, or answer-path defects, use the
repeatable replay-and-diagnosis loop described in:

- `docs/engineering/real_path_server_replay_and_telemetry_loop.md`

That note is the preferred operational runbook when you need to keep replaying
the real server path, inspecting exact turn telemetry, and iterating until the
behaviour is both user-correct and telemetry-consistent.

## 8. Practical Refactoring and Consistency Habits

- Search first before adding helpers or parallel pathways.
- If similar logic is appearing for the third time, stop and centralise it.
- Prefer one authoritative write pathway for high-risk state changes such as
  auth, destructive mutations, or durable Vontology updates.
- Preserve user-authored text exactly unless the task explicitly authorises
  normalisation or renaming.
- Add brief comments that explain why a tricky path exists when future
  modification would otherwise be error-prone.

Compact DRY checklist:

1. Search for existing helpers and all parallel instances before fixing.
2. If the same pattern is appearing for the third time, stop and centralise it.
3. Create the helper first.
4. Replace all relevant usages in one pass rather than patching one-by-one.

## 9. Recurring Troubleshooting Patterns

### 9.1 Behaviour looks wrong on a workflow- or prompt-governed path

Inspect the authoritative prompt, workflow definition, routing metadata, and
rendered inputs before starting code-first diagnosis. In Von, the explanation
for behaviour often lives in those artefacts rather than in Python.

Also check:

- the actual context sent to the model at the stage that behaved badly, not
  only the visible prompt fragment
- any context-lineage telemetry showing what was inherited vs added vs reduced
- whether the user-facing answer was built from result content or from workflow
  bookkeeping, completion narration, or renderer diagnostics

### 9.2 A direct test passes but the real tool fails

Suspect gateway-path differences, schema validation, auth/context injection, or
response-shape mismatches before assuming the business logic is correct.

### 9.3 Auth works in one process but not another

Check `.env` override registration, process restarts, and which key was
actually resolved at runtime.

### 9.4 The temptation to "just script around it"

Pause before doing this. If the canonical Jira, GitHub, workflow, or Vontology
path is unreliable, the durable fix is usually to repair that path, document
the failure mode, and keep the system's intended control surface intact.

## 10. Maintaining This Guide

- Put durable practical lessons here when they are too detailed for
  `AGENTS.md` but likely to help future implementation work.
- Move mature domain-specific material into its own engineering document when it
  grows beyond a compact operational note.
- If a lesson is truly constitutional rather than practical, promote a shorter
  version into `AGENTS.md` instead.
