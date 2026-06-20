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
- Prefer bounded, inspectable commands in the native shell for the current OS.
- If a problem appears to be transport, host, or tool-session related, fix or
  restart that layer rather than debugging the repository blindly.
- When a workflow/prompt/Vontology dependency fails, fix that dependency path
  where possible rather than patching symptoms with local heuristics.

## 4. Shell and Host Defaults

### 4.1 Host-appropriate shell first

Choose the shell for the host and the thing being tested:

- On Windows, default to PowerShell. Prefer:
  - `$env:VAR = 'value'`
  - `$var = (Get-Content file.txt)`
  - here-strings for larger inline snippets
- On macOS and Linux, default to the native POSIX shell (`zsh`/`sh`), especially
  for PATH-sensitive probes and standard package-manager commands. Prefer:
  - `VAR=value command`
  - `export VAR=value`
  - `rg`, `sed`, `awk`, and normal POSIX path syntax where appropriate

Use PowerShell on macOS/Linux when invoking `.ps1` scripts or deliberately
testing PowerShell behaviour. Use `zsh`/Bash syntax on Windows only when the user
explicitly asks for it or the command is running inside a known POSIX layer.

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

### 4.6 Long-session process hygiene

Long coding sessions that repeatedly start local Von servers, browser replays,
Playwright tooling, or MCP helper processes can quietly accumulate stale
processes and host-level memory pressure.

Do not treat this as harmless background noise. It changes test results,
consumes ports, and can destabilise the host badly enough to distort later
debugging.

Run a bounded hygiene check:

- before starting yet another local Von server because "the previous one might
  be stuck";
- after multiple browser replay attempts or Playwright-driven acceptance runs;
- after repeated MCP/tool-host restarts;
- whenever RAM pressure or unexplained port conflicts start to appear;
- at least once in any long session that has already spawned several local
  services.

Good first probes:

- current RAM pressure:
  `Get-CimInstance Win32_OperatingSystem | Select-Object @{Name='TotalGB';Expression={[math]::Round($_.TotalVisibleMemorySize/1MB,2)}}, @{Name='FreeGB';Expression={[math]::Round($_.FreePhysicalMemory/1MB,2)}}`
- duplicate Von listeners:
  `Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -ge 5000 -and $_.LocalPort -le 5008 }`
- likely stale Von / MCP / Playwright processes:
  `Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe','node.exe','chrome.exe' -and $_.CommandLine -match 'src\\.workflows\\.von\\.main|mcp_server|playwright|@playwright/mcp|mcp-chrome' }`
- top memory consumers:
  `Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 15 ProcessName, Id, @{Name='WS_GB';Expression={[math]::Round($_.WorkingSet64/1GB,2)}}`

Common stale-process families in Von work:

- extra `src.workflows.von.main` servers on adjacent ports from prior retries
- Playwright daemon `node` processes and their temporary Chrome profiles
- MCP stdio Python helpers no longer attached to a real client
- temporary probe scripts or one-off debug helpers left behind after diagnosis

Cleanup expectations:

- prefer stopping clearly stale duplicate helpers before launching new ones;
- keep the currently used local server or browser session alive when practical;
- do not work around stale listeners by blindly hopping to a new port unless
  the retained old process is genuinely needed;
- after cleanup, re-check RAM and listeners so the session notes say whether
  the pressure actually improved.

Canonical local Von restart:

- Restart the local backend through the repository launcher:
  - on macOS/Linux: `./run.sh restart -NoBrowser -HealthTimeoutSec 180`
  - on Windows/PowerShell: `.\run.ps1 restart -NoBrowser -HealthTimeoutSec 180`
- Use the host-native launcher by default: `run.sh` on macOS/Linux and
  `run.ps1` on Windows. Use PowerShell on macOS/Linux only when deliberately
  testing the PowerShell launcher.
- Do not manually restart the backend with `Start-Process`,
  `python src/workflows/von/main.py`, or another direct process spawn. Those
  paths can bypass launcher setup such as `.env` loading, import-path setup,
  admin/shutdown handling, logs, and health/version checks.
- After restart, verify the process with the matching launcher
  (`./run.sh status -NoBrowser` or `.\run.ps1 status -NoBrowser`) and `/health`.
  Confirm the reported branch and commit match the checkout you are testing
  before running replay or acceptance evidence.
- If there is already a listener on port 5000, inspect it first. If it is the
  wrong branch or a stale process, stop/restart through the host-native
  repository launcher rather than leaving the old process alive and moving to
  another port.

Isolated coding-agent replay backend:

- For automated replay, live prompt sampling, or acceptance evidence that
  should not disturb the user-facing local server, use:
  - on macOS/Linux: `./run.sh restart -AgentTest -HealthTimeoutSec 180`
  - on Windows/PowerShell: `.\run.ps1 restart -AgentTest -HealthTimeoutSec 180`
- `-AgentTest` defaults to port `5010`, implies `-NoBrowser`, preserves other
  Von server processes, and skips shared background workers/startup maintenance.
  Maintained live replay/testing tools default to `http://127.0.0.1:5010` and
  check `/health` for `agent_test_instance=true` so accidental use of the
  interactive server fails clearly.
- If port `5010` is already deliberately in use, choose an explicit isolated
  port such as:
  - on macOS/Linux: `./run.sh restart -AgentTest -Port 5011 -HealthTimeoutSec 180`
  - on Windows/PowerShell: `.\run.ps1 restart -AgentTest -Port 5011 -HealthTimeoutSec 180`
  and set `VON_AGENT_TEST_BASE_URL=http://127.0.0.1:5011` or pass the matching
  `--base-url` to the replay tool.
- Use replay-tool `--allow-non-agent-test-server` only when the test's purpose
  is specifically to exercise the interactive/user-facing server.
- Do not treat `-Port` alone as isolation. The isolated mode changes launcher
  ownership semantics so an automated run does not globally clean up or adopt
  unrelated local Von processes.
- For lightweight single-LLM-exchange debugging, use
  `scripts/replay_llm_exchange.py` instead of starting a full server when the
  question is only model timing, provider availability, or a proposed prompt
  revision. It can inspect/replay a logged exchange by `request_id`, replay an
  `llm_exchange_blob.v1` JSON file, send an explicit prompt, or replace a
  logged prompt with `--override-prompt-file` while preserving the logged
  context. This is diagnostic support only: it does not execute tools, run
  workflows, mutate Vontology, or count as acceptance evidence for a live
  turn.
  Examples:
  - inspect the captured exchange:
    `./.venv/bin/python scripts/replay_llm_exchange.py --request-id <request_id> --entry 1 --inspect-only`
  - replay the captured exchange against a selected local model:
    `./.venv/bin/python scripts/replay_llm_exchange.py --request-id <request_id> --provider ollama --model qwen3:8b --timeout 120`
  - test a revised prompt body against the captured context:
    `./.venv/bin/python scripts/replay_llm_exchange.py --request-id <request_id> --override-prompt-file /path/to/revised_prompt.txt --provider ollama --model qwen3:8b --timeout 120`
  - send a standalone explicit prompt for prompt-fix planning:
    `./.venv/bin/python scripts/replay_llm_exchange.py --prompt-file /path/to/prompt.txt --provider mock --model diagnostic`
- In VS Code / VS Code Insiders, the Codex Browser and Chrome plugin bundles
  can be present while the live browser backends are not exposed to the coding
  session. If the Browser/Chrome client lists no browsers or reports
  `Browser is not available: iab` / `Browser is not available: extension`
  after one retry, do not keep pursuing plugin recovery as the acceptance path.
  Use AgentTest plus Playwright for Von browser evidence, and reserve Codex App
  plugin recovery for sessions actually running in the Codex App UI.
- For Codex-driven Playwright checks on macOS, the normal sandbox may block
  Chromium launch or localhost browser access even when Playwright is installed.
  A characteristic launch failure is
  `MachPortRendezvousServer... Permission denied`. In that case, rerun the
  Playwright command with escalated sandbox permissions, validate against the
  AgentTest URL such as `http://127.0.0.1:5010`, and stop the AgentTest server
  when the browser evidence has been captured.

If cleanup removes the obvious duplicates but memory pressure remains extreme,
or kernel/pool counters stay abnormally high relative to process working sets,
treat that as a broader host issue rather than endlessly restarting repo
processes. Record that fact in diagnostics and avoid pretending the repository
itself is the only source of the problem.

### 4.7 Jira implementation close-out barrier

For Jira implementation tasks, local validation is not a stopping point. A
Jira comment that says the implementation is done, validated, ready to merge,
or ready to close creates a close-out obligation: finish the Git/Jira lifecycle
immediately unless the user explicitly asked to pause before commit, merge, or
transition.

Before telling the user the task is complete, run a bounded close-out checkpoint:

- `git status --branch --short`
- `git diff --name-only`
- verify the intended commit is on the branch or on `origin/main`, depending
  on the current lifecycle step
- read back the Jira issue status after the last comment or transition
- confirm there are no unrecorded authoritative Vontology/workflow/KB changes
  required for the task

Expected close-out for a Jira implementation task:

1. Finish targeted validation and any required real-path acceptance.
2. Add the closure/progress Jira comment with the evidence actually gathered.
3. Commit the complete in-scope change set, including regressions and required
   docs.
4. Merge or fast-forward to `main` and push, unless the task is intentionally
   stopping at a branch review state.
5. Verify `origin/main` contains the intended commit.
6. Transition the Jira issue to the intended done state and read it back.
7. Clean up branch/worktree state as described below.

If any step is blocked, do not describe the task as complete. Report the exact
remaining step, the blocker, and whether the local worktree or Jira status is
now inconsistent with the intended outcome.

### 4.8 Post-merge branch and worktree hygiene

After a Jira implementation task is merged to `main`, do not leave the Git
state half-finished.

Common failure shape:

- `origin/main` contains the fix;
- Jira is already closed;
- but the current worktree is still sitting on the completed task branch;
- the retained local `main` worktree is several commits behind;
- and the merged local/remote task branch is left behind indefinitely.

That is not merely cosmetic. It causes later confusion about what is really
done, makes branch lists noisy, and increases the chance that a later agent
reopens work on the wrong base.

Expected close-out after merge:

- verify the intended commit is on `origin/main`;
- if you keep a separate local `main` worktree, fast-forward it when it is
  clean and intended to track `origin/main`;
- move the current worktree off the completed task branch before trying to
  delete that branch;
- delete the merged local task branch;
- delete the merged remote task branch unless there is a clear reason to keep
  it;
- if you intentionally keep a merged branch, record why.

Useful probes:

- current branch and worktree state:
  `git branch --all --verbose --no-abbrev`
- merged local branches:
  `git branch --merged origin/main`
- worktree layout:
  `git worktree list`
- fast-forward a retained main worktree:
  `git -C C:\\path\\to\\main-worktree merge --ff-only origin/main`
- move the current worktree off a completed task branch:
  `git checkout --detach origin/main`
- delete a merged local branch:
  `git branch -d <task-branch>`
- delete a merged remote branch:
  `git push origin --delete <task-branch>`

If branch deletion fails, diagnose the real blocker rather than silently
abandoning cleanup. The common reasons are:

- the branch is still checked out in the current worktree;
- the branch is checked out in another worktree;
- the supposed `main` worktree is not actually clean enough to fast-forward;
- or the branch is not really merged yet.

## 5. Environment and Credential Handling

- `.env` is the authoritative local source for credentials and service-critical
  configuration unless a stronger deployment mechanism is intentionally in use.
- Never print secrets or dump `.env`.
- When code depends on a credential or service-critical environment variable,
  register the key in `_apply_dotenv_overrides()` in
  `src/workflows/von/main.py`.
- For repo-local shell diagnostics of internal Von MCP tools, prefer a canonical
  helper that runs through `InternalMCPGateway` from the repo root rather than
  assuming the parent shell inherited `.env`. Use
  `python utilities/invoke_internal_mcp_tool.py <tool_name>` or
  `pdm run python utilities/invoke_internal_mcp_tool.py <tool_name>`. The helper
  delegates through PDM when launched by a system Python, so it avoids missing
  repo dependencies and centralises the gateway initialisation boilerplate.
- For multi-step internal Von MCP writes, use the same helper with a JSON payload
  file rather than hand-writing inline Python. This keeps `InternalMCPGateway`,
  `InternalMCPTransport`, catalogue construction, output-schema validation, and
  write guardrails on the canonical path.
- For Codex-side Jira issue lifecycle actions, prefer the installed Atlassian
  Rovo/Jira connector when available and healthy. Use Von's internal Jira MCP
  helper when the connector is unavailable, auth is broken, or the task
  specifically needs to validate Von's own Jira integration path.
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

### 5.2 Codex automation dependency bootstrap

Codex app automations may run in a local sandbox or a dedicated worktree whose
dependency state is not the same as the main interactive checkout. Do not assume
the main checkout's `.venv`, `.pdm-python`, global `pdm`, or host shell PATH are
usable inside an unattended automation run.

For Von automation/worktree setup, use the repo-local bootstrap:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_codex_automation_environment.ps1
```

The script creates or repairs the checkout-local `.venv`, installs PDM inside
that venv, records the matching `.pdm-python`, and verifies core Python imports.
For an existing usable `.venv`, it skips dependency installation to avoid
rewriting locked packages under active local MCP/server processes. For a fresh
or incomplete worktree environment, it runs PDM dependency installation from the
repo root. It also avoids upgrading `pip` in an existing usable `.venv` unless
`-UpgradePip` is passed. Fresh checkout-local venvs still upgrade `pip`, but
fresh automation fallback venvs skip that nonessential network step unless
`-UpgradePip` is explicitly passed. Pip package-index calls use bounded timeout
and retry settings, and the PDM dependency installation step has a wall-clock
timeout so unattended automation fails closed before the outer automation run
times out. The script intentionally does not read `.env` or perform
machine-global setup such as MongoDB, Tesseract, uv tools, or VS Code
configuration.

If the checkout-local `.venv\Scripts\python.exe` exists but cannot be executed
inside a Codex automation sandbox, the bootstrap retries the probe and then uses
a non-repo fallback venv under Codex automation storage rather than deleting the
checkout `.venv`. This avoids breaking long-lived local Von services that may
be using the checkout venv while still giving unattended automation a usable
Python/PDM environment. If Codex automation storage is not writable in the
sandbox, the bootstrap probes candidate roots and falls back to a temp-directory
automation venv.

Use the lighter smoke form when you only need to verify the already-prepared
environment without reinstalling dependencies:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\setup_codex_automation_environment.ps1 -SkipDependencyInstall
```

Use `-ForceDependencyInstall` only when a run deliberately needs to refresh the
checkout-local venv.

## 6. Preferred Tool and Access Pathways

### 6.1 Vontology and workflow behaviour

- Use Vontology API, MCP tools, or canonical service pathways for
  Vontology-governed data.
- Do not introduce direct DB access for Vontology-governed state.
- Prefer workflow MCP tools as the default control surface for workflow
  behaviour when the behaviour can be represented there.

### 6.1a Von manual and durable document authoring

When the user says something like "add a Von manual", "write a Von runbook",
"store this as a Von guide", or "put this manual in Von blob", treat the request
as Vontology document/blob authoring unless they explicitly ask for repo-only
documentation.

This is a Vontology/MCP-mediated workflow:

1. Resolve the relevant type and predicates first. Prefer an existing specific
   manual/runbook/document type when one exists; otherwise use `#V#document` as
   the parent type. Prefer canonical predicates, especially
   `#V#propositional_information_thing_has_computer_file` for the
   document-to-file-copy edge when available.
2. Draft any source file needed for blob import in an ignored workspace path
   such as `.run/manuals/`. This file is only an import source; do not commit it
   unless the user also asked for repo documentation.
3. Register the file through the Von file-copy/blob path, normally
   `import_local_file_copy`, with explicit `namespace`, user/org context where
   supported, source metadata, and no secret-bearing content. Do not use direct
   blob, Mongo, or filesystem shortcuts for the persisted Von artefact.
4. Create or reuse the corresponding Vontology concept through
   `create_concepts` or the canonical Vontology concept API. Attach useful
   text relations such as `hasName`, `hasDescription`, and a concise
   `hasContent` summary so the concept is intelligible without opening the
   blob. Preserve user-authored wording exactly when they supplied the manual
   text.
5. Link the document concept to the file-copy concept with the canonical
   document/file-copy predicate. Add provenance context such as related Jira
   issue, source system, namespace, and file-copy concept ID.
6. Read the file-copy back with `read_file_copy` and, when useful, index it with
   `index_file_copy`. Do not claim "stored in Von blob" if only the local
   staging file or a text relation exists.
7. If the user asks for review, follow-up, or "send me a task", create a Von
   native task with the manual concept ID, file-copy concept ID, review focus,
   and acceptance criteria. Assign it to the authenticated/known user by
   default unless told otherwise.
8. Report the durable IDs at the end: manual/document concept, file-copy/blob
   concept, indexed document ID when available, and task concept ID when one was
   created. State any read-back or indexing blocker explicitly.

If a first-class Vontology workflow exists later for manual/document authoring,
prefer invoking that workflow over hand-sequencing the same MCP calls. Until
then, this recipe is the expected operational workflow.

### 6.1b Reusable workflow authoring during replay-driven fixes

When a real-path replay or `JVNAUTOSCI-1894`-style test uncovers a missing
capability that is really a broader research/lab task class, prefer authoring a
new reusable workflow or subworkflow in Vontology rather than patching Python to
make the sampled prompt pass.

This is allowed even when the workflow design relies partly on background
knowledge of the task class, so long as the result is a reusable authored
workflow rather than a one-off hack for a particular prompt combination.

Typical examples:

- paper-author disambiguation;
- daily diary drafting from represented context;
- weekly lab activity/progress report composition;
- stale-or-unnecessary task review.

Operational rule:

- if the behaviour can be represented cleanly in VWL/Vontology, do that;
- if a reusable runtime or authoring primitive is missing, add only that
  primitive in Python and then author the workflow through the canonical
  workflow-authoring path;
- do not encode the durable decision policy as a selector tweak, special route
  override, or test-specific orchestration patch just because that is faster
  for the current replay.

Use the current workflow-authoring guidance in
`docs/engineering/von_workflow_language_manual.md`, especially:

- `### 4.1a Generic Workflow Authoring Primitives`
- `#V#workflow_repair_or_create_workflow`
- `#V#workflow_authoring_repair_workflow`
- `#V#von_workflow_creation_workflow`

If a proposed workflow would only make sense for the exact wording or exact
tool mix of the current failing test, treat that as evidence you have not yet
found the right reusable workflow boundary.

### 6.2 Jira

- For Codex-side Jira task lifecycle work, prefer the installed Atlassian
  Rovo/Jira connector when available and healthy. Use it for issue reads and
  searches, comments, transitions, assignee/status checks, linked issue
  read-back, and final close-out verification.
- Use Von's internal Jira MCP path when the task specifically validates Von's
  Jira integration, when Jira access must flow through Von's own authority/tool
  surfaces, or when the Atlassian connector is unavailable or unhealthy.
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
- The `JVNAUTOSCI` workflow distinguishes `Backlog` from `To Do`. They are
  different statuses with different planning meaning, not synonyms. Treat the
  motion between them as explicit triage:
  - `Backlog`: known work that is not committed for the current focus window.
    Newly-created subtasks that are intentionally deferred (e.g. spun off as
    follow-ups while implementing the in-scope sibling) belong here, not in
    `To Do`.
  - `To Do`: work that is committed and ready to be picked up next. Use this
    only when the task is genuinely on deck.
  - When a task is created via the standard issue-creation path it usually
    lands in `To Do` by default. If the intent is "track for later, not now",
    transition it to `Backlog` (`transition_id=2` for `JVNAUTOSCI`) in the
    same step rather than leaving the queue cluttered.
  - When promoting a backlog item to active work, transition it explicitly to
    `To Do` (or directly to `In Progress` when starting immediately). Do not
    pick work directly out of `Backlog` and silently bypass `To Do`; the
    state change is the signal that the queue has been re-triaged.

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

#### Codex automation publish preflight

Recurring Codex automations that must publish from the sandbox should use the
PowerShell-native REST API preflight script instead of hand-writing `gh api`
request bodies inline:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\powershell\test_codex_automation_publish_preflight.ps1
```

The script loads only `VON_CODEX_AUTOMATION_TOKEN` from repo-root `.env` into
`GH_TOKEN` for its process, avoids `gh auth status`, verifies `gh api user`,
`gh repo view`, and `main` ref access, then creates, reads, deletes, and
rechecks `refs/heads/codex/api-preflight-token-test`. It uses a temporary JSON
file for `gh api --input`, because Bash-style `<<<` redirection is invalid in
PowerShell and older Windows PowerShell `Set-Content -Encoding UTF8` can emit a
BOM that GitHub rejects as malformed JSON.

### 6.6a Non-sandbox Git/GitHub publication handoff: JVNAUTOSCI-2208

This note preserves the exact operational pattern that allowed
`JVNAUTOSCI-2208` to be published from a normal Codex desktop agent after a
previous Codex automation sandbox implemented and validated the patch but could
not push it.

Use this as a runbook when an automation handoff says a patch exists locally
but Git/GitHub operations failed in the sandbox. The successful fix was not a
code change to Git tooling. It was to run the final Git operations from the
normal non-sandbox desktop agent environment, where the real worktree `.git`
metadata and host credential store were available.

#### Identity and account context

The successful non-sandbox agent session ran in:

```powershell
$env:USERNAME; whoami
```

Observed output:

```text
mwit860
uoa\mwit860
```

The Git author/committer identity was:

```powershell
git config --get user.name
git config --get user.email
git log -1 --pretty=fuller
```

Observed output after the successful commit:

```text
Michael Witbrock
witbrock@gmail.com

commit e7c9a9dc18f992f27b4bc0ec559c0aaa9494c4a6
Author:     Michael Witbrock <witbrock@gmail.com>
AuthorDate: Thu Apr 30 20:15:59 2026 +1200
Commit:     Michael Witbrock <witbrock@gmail.com>
CommitDate: Thu Apr 30 20:15:59 2026 +1200

    JVNAUTOSCI-2208 Surface selected child workflow traces
```

The Jira/Atlassian connector identity used for issue comments and transition
was `Michael Witbrock <m.witbrock@auckland.ac.nz>`.

The GitHub CLI identity available in the non-sandbox environment was:

```powershell
gh auth status
```

Observed output, with the token redacted:

```text
github.com
  ✓ Logged in to github.com account witbrock (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'
```

The actual push path used by `git push` was HTTPS through Git Credential
Manager, not the GitHub connector and not SSH:

```powershell
git remote -v
git config --show-origin --get-all credential.helper
```

Observed output:

```text
origin  https://github.com/Strong-AI-Lab/Von.git (fetch)
origin  https://github.com/Strong-AI-Lab/Von.git (push)
file:C:/Program Files/Git/etc/gitconfig manager
```

#### What was broken in the automation sandbox

The previous automation run recorded the blocker in Jira comments `34956`,
`34957`, and `34958`.

The important failure facts were:

- the primary repo `.git` directory was ACL-locked for the sandbox user, so
  normal branch and commit operations against the main worktree Git metadata
  failed with lock/ref permission errors;
- local HTTPS `git push` could not complete under the sandbox's available
  credential helper;
- with prompts disabled, the sandbox recorded this exact Git error:

```text
fatal: could not read Username for 'https://github.com': terminal prompts disabled
```

- `gh` authentication in that sandbox was invalid, recorded as:

```text
HTTP 401
token invalid
```

- the GitHub connector could create branches and small blobs, but the sandbox
  transcript/tooling path truncated the larger normalised file-content payloads
  needed to upload the four-file patch safely through connector blob/tree APIs.

The automation run also recorded an alternate local Git object store:

```text
C:\Users\mwit860\.codex\automations\jira-task-review-and-initiation\git-JVNAUTOSCI-2208.git
```

and corrected the alternate local commit SHA to:

```text
5d546eede4f8777f441f4bd070de907ef100a895
```

The full lock/ref error text from the sandbox ACL failure was not preserved in
the Jira comments. Future agents should not invent that output. Treat the
preserved fact as: the automation sandbox could not write the primary `.git`
metadata, while the normal non-sandbox desktop agent could.

#### What permissions or metadata had to be fixed

No repository metadata or filesystem ACLs were changed in the successful
non-sandbox session.

The fix was environmental:

- run from the normal worktree at
  `C:\Users\mwit860\Programming\Strong-AI-Lab\Von`;
- use the primary `.git` directory that the desktop user can write;
- use the host Git Credential Manager and keyring-backed GitHub credentials;
- avoid trying to push through the locked automation sandbox or through a
  transcript-sized connector blob upload.

The non-sandbox session proved the `.git` metadata was writable by creating a
branch, staging four files, creating commit
`e7c9a9dc18f992f27b4bc0ec559c0aaa9494c4a6`, fast-forwarding local `main`, and
pushing `main` to GitHub.

#### Successful command sequence

All commands were run from:

```powershell
Set-Location C:\Users\mwit860\Programming\Strong-AI-Lab\Von
```

Check the initial state and preserve the handoff patch as untracked:

```powershell
git status --short --branch
git fetch origin main
git rev-list --left-right --count main...origin/main
git apply --reverse --check JVNAUTOSCI-2208-selected-child-workflow-traces.patch
```

Relevant observed output:

```text
## main...origin/main
 M src/backend/services/turn_execution_diagnostics_service.py
 M src/backend/workflows/durable/turn_execution_runtime_support.py
 M tests/backend/test_turn_execution_diagnostics_workflow_trace_refs.py
 M tests/backend/test_turn_execution_selected_workflow_output_propagation.py
?? JVNAUTOSCI-2208-selected-child-workflow-traces.patch

0       0
```

`git apply --reverse --check` returned exit code `0` with no stdout, meaning
the patch was already applied in the worktree.

Create the task branch:

```powershell
git switch -c codex/JVNAUTOSCI-2208-selected-child-workflow-traces
```

Observed output:

```text
Switched to a new branch 'codex/JVNAUTOSCI-2208-selected-child-workflow-traces'
```

Validate before committing:

```powershell
python -m pytest tests/backend/test_turn_execution_selected_workflow_output_propagation.py tests/backend/test_turn_execution_diagnostics_workflow_trace_refs.py tests/backend/test_turn_execution_record_service_execution_summary.py::test_custom_workflow_summary_uses_selected_workflow_trace_when_dispatch_events_missing tests/backend/test_turn_execution_record_service_execution_summary.py::test_custom_workflow_summary_uses_trace_execution_summary_when_aux_entry_missing tests/backend/test_rag_turn_execution_records_mcp_read_tools.py::test_turn_execution_get_diagnostics_reconstructs_from_projection -q --tb=short

python -m py_compile src/backend/workflows/durable/turn_execution_runtime_support.py src/backend/services/turn_execution_diagnostics_service.py

pdm run pyright src/backend/workflows/durable/turn_execution_runtime_support.py src/backend/services/turn_execution_diagnostics_service.py tests/backend/test_turn_execution_selected_workflow_output_propagation.py tests/backend/test_turn_execution_diagnostics_workflow_trace_refs.py

pdm run python scripts/pytest_lanes.py recommend --git-diff origin/main --risk normal

pdm run pytest tests/backend/test_mcp_stdio_server_exposes_turn_execution_diagnostics_tool.py tests/backend/test_turn_execution_diagnostics_workflow_trace_refs.py tests/backend/test_turn_execution_selected_workflow_output_propagation.py -q
```

Observed validation outcomes:

```text
21 passed, 1 warning in 7.09s

0 errors, 0 warnings, 0 informations

20 passed in 73.20s (0:01:13)
workflow_capability_index_background_build_failed: inherits_chat_default_but_no_chat_default_is_available
```

The `workflow_capability_index_background_build_failed` line was emitted after
the direct-target test run but did not fail the tests.

Stage only the real implementation/test files, not the patch handoff:

```powershell
git add -- src/backend/workflows/durable/turn_execution_runtime_support.py src/backend/services/turn_execution_diagnostics_service.py tests/backend/test_turn_execution_selected_workflow_output_propagation.py tests/backend/test_turn_execution_diagnostics_workflow_trace_refs.py
git status --short
git diff --cached --name-only
git diff --cached --stat
```

Observed staged files:

```text
src/backend/services/turn_execution_diagnostics_service.py
src/backend/workflows/durable/turn_execution_runtime_support.py
tests/backend/test_turn_execution_diagnostics_workflow_trace_refs.py
tests/backend/test_turn_execution_selected_workflow_output_propagation.py
```

Commit:

```powershell
git commit -m "JVNAUTOSCI-2208 Surface selected child workflow traces"
```

Observed output:

```text
[codex/JVNAUTOSCI-2208-selected-child-workflow-traces e7c9a9dc] JVNAUTOSCI-2208 Surface selected child workflow traces
 4 files changed, 427 insertions(+), 3 deletions(-)
```

Push the task branch:

```powershell
git push -u origin codex/JVNAUTOSCI-2208-selected-child-workflow-traces
```

Observed output:

```text
branch 'codex/JVNAUTOSCI-2208-selected-child-workflow-traces' set up to track 'origin/codex/JVNAUTOSCI-2208-selected-child-workflow-traces'.
remote:
remote: Create a pull request for 'codex/JVNAUTOSCI-2208-selected-child-workflow-traces' on GitHub by visiting:
remote:      https://github.com/Strong-AI-Lab/Von/pull/new/codex/JVNAUTOSCI-2208-selected-child-workflow-traces
remote:
To https://github.com/Strong-AI-Lab/Von.git
 * [new branch]        codex/JVNAUTOSCI-2208-selected-child-workflow-traces -> codex/JVNAUTOSCI-2208-selected-child-workflow-traces
```

Fast-forward merge to local `main` and push `main`:

```powershell
git fetch origin main
git switch main
git merge --ff-only codex/JVNAUTOSCI-2208-selected-child-workflow-traces
git push origin main
```

Observed output:

```text
From https://github.com/Strong-AI-Lab/Von
 * branch              main       -> FETCH_HEAD

Your branch is up to date with 'origin/main'.
Switched to branch 'main'

Updating 69add526..e7c9a9dc
Fast-forward
 .../services/turn_execution_diagnostics_service.py |  30 +++
 .../durable/turn_execution_runtime_support.py      | 263 +++++++++++++++++++++
 ...rn_execution_diagnostics_workflow_trace_refs.py |  47 +++-
 ...ecution_selected_workflow_output_propagation.py |  90 +++++++
 4 files changed, 427 insertions(+), 3 deletions(-)

To https://github.com/Strong-AI-Lab/Von.git
   69add526..e7c9a9dc  main -> main
```

Verify `origin/main` contains the commit:

```powershell
git fetch origin main
git log origin/main -1 --oneline
git branch -r --contains e7c9a9dc
git ls-remote origin HEAD
```

Observed output:

```text
e7c9a9dc JVNAUTOSCI-2208 Surface selected child workflow traces

  origin/HEAD -> origin/main
  origin/main

e7c9a9dc18f992f27b4bc0ec559c0aaa9494c4a6        HEAD
```

Clean up the merged task branch:

```powershell
git branch -d codex/JVNAUTOSCI-2208-selected-child-workflow-traces
git push origin --delete codex/JVNAUTOSCI-2208-selected-child-workflow-traces
git ls-remote origin refs/heads/codex/JVNAUTOSCI-2208-selected-child-workflow-traces
```

Observed output:

```text
Deleted branch codex/JVNAUTOSCI-2208-selected-child-workflow-traces (was e7c9a9dc).

To https://github.com/Strong-AI-Lab/Von.git
 - [deleted]           codex/JVNAUTOSCI-2208-selected-child-workflow-traces
```

`git ls-remote` returned no output for the deleted task branch.

The final local state after the task remained:

```powershell
git status --short --branch
```

```text
## main...origin/main
?? JVNAUTOSCI-2208-selected-child-workflow-traces.patch
```

The patch file was intentionally left untracked as a handoff artefact and was
not committed.

#### Verify credentials before a real push

Before attempting a real push from a future non-sandbox agent, verify both Git
remote access and push authentication without changing the remote:

```powershell
git status --short --branch
git remote -v
git config --show-origin --get-all credential.helper
gh auth status
git ls-remote origin HEAD
git push --dry-run origin main
```

Known-good output from this non-sandbox session included:

```text
origin  https://github.com/Strong-AI-Lab/Von.git (fetch)
origin  https://github.com/Strong-AI-Lab/Von.git (push)
file:C:/Program Files/Git/etc/gitconfig manager

github.com
  ✓ Logged in to github.com account witbrock (keyring)
  - Active account: true
  - Git operations protocol: https
  - Token: gho_************************************
  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'

e7c9a9dc18f992f27b4bc0ec559c0aaa9494c4a6        HEAD

Everything up-to-date
```

If `git push --dry-run origin main` prompts, hangs, or emits
`fatal: could not read Username for 'https://github.com': terminal prompts
disabled`, do not proceed with a real push. Fix the host Git Credential Manager
or switch to an explicitly authorised GitHub authentication path first.

#### Caveats for future agents

- Normal Codex desktop agents and Codex automation sandboxes may not share the
  same filesystem permissions, `.git` writability, GitHub credential helper, or
  `gh` token state.
- A successful local test run in an automation sandbox does not prove that the
  sandbox can commit or push through the primary repo `.git` directory.
- If the sandbox uses an alternate Git dir, verify whether that commit exists
  only in the alternate object store before assuming it can be pushed from the
  primary worktree.
- Prefer the normal repo Git path when publishing already-reviewed handoff
  patches. Do not use connector blob/tree APIs as a substitute for Git unless
  the connector can ingest local file contents safely and without transcript
  truncation.
- Always stage only intended files. For handoff patches, keep the patch file
  untracked unless the user explicitly asks to preserve it in the repo.
- After merging to `main`, delete merged task branches unless there is a
  recorded reason to keep them.

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

### 7.1.1 Match validation cost to change shape

Test wall-clock is not free. A 50-minute lane is a real cost paid by the user
every time it runs, and it should be paid only when the change shape actually
warrants it. Calibrate validation effort to the architectural surface the
change touches, not to a reflex of "always run the broadest lane".

Decision rules:

- **Mechanical, backward-compatible changes** (e.g. adding a defaulted kwarg,
  renaming an internal helper, threading an additional optional field through
  telemetry) — the impacted surface is exactly what the recommender finds.
  Run targeted regression tests plus
  `pdm run python scripts/pytest_lanes.py recommend --git-diff origin/main`
  direct targets, then stop. Full-lane runs are usually waste here.
- **Behavioural changes inside a known boundary** (signature change with
  semantic effect, prompt/template wiring change, new validation branch) —
  run the recommended lane(s), but prefer running only the impacted *files*
  the recommender names rather than `run-lane <name>` if the lane is
  integration-heavy.
- **Cross-cutting or authority-surface changes** (workflow control, prompt
  authority, predicate semantics, Vontology schema, gateway routing) —
  aggregate lanes are appropriate. Plan for the cost up front instead of
  discovering it after a 50-minute run.

When a targeted run reports failures, the next step is almost never "run a
broader lane to see if it's pre-existing". Instead:

1. **Baseline-check on origin/main directly**, by running only the failing
   test ids: `git stash; pdm run pytest <failing-ids> -q --tb=no; git stash
   pop`. This typically takes 1–3 minutes versus 30–60 minutes for a full
   lane re-run, and it gives a definitive pre-existing/regression verdict
   for those exact tests.
2. Only widen if the baseline check reveals the failures are *new*, in which
   case run the smallest superset that exercises the suspect code path.

When invoking pytest for failure investigation, default to compact output:
`-q --tb=no` (or `--tb=line`) avoids dumping multi-megabyte tracebacks into
the conversation, which is itself an expensive operation that erodes context
budget and triggers summarisation.

When a long lane is genuinely required, redirect output to a log file
(`... 2>&1 | Out-File logs/<lane>_<task>.txt`) and surface only the tail
(`Get-Content ... -Tail 30`) into the conversation. The full log remains
available on disk if deeper inspection is needed.

If the same closure or helper name exists in multiple files (`_record_llm_call`
is a recent example with four definitions across two modules), grep the entire
repository before adding a new kwarg. Adding the parameter to one definition
and missing the others manifests as a wave of `unexpected keyword argument`
failures only after a long lane run — exactly the kind of cost this section
exists to avoid.

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

If an older local Settings session shows `[object PointerEvent]` as the OpenAI
premium model, clear the browser localStorage keys `von:localModelPreference`
and `von:openaiSelectedModel`, then reopen Settings > Premium Models and verify
the API key again. Current frontend code also ignores and cleans those corrupted
values on read, but manual cleanup is useful when validating stale tabs.

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

### 9.5 Repo seed-bundle invariants and version-bump guard

The workflow seed bundles in
[src/backend/workflows/repo_seed_bundles/](../../src/backend/workflows/repo_seed_bundles/)
seed missing Vontology state at runtime; Vontology remains the authority. Two
local guards reduce the chance that an edit silently breaks the runtime
seed-version gate:

- [tests/backend/test_repo_seed_bundle_invariants.py](../../tests/backend/test_repo_seed_bundle_invariants.py)
  asserts structural invariants: every bundle parses; workflow bundles declare
  a non-empty `seed_version`; every `to_state` references a defined state; the
  canonical conversation-turn workflow's `completion_gate` keeps routing
  empty/missing `response_text` and `completion_gate_requires_follow_up` to
  `recovery_decision`; `recovery_decision` exposes `thinking_card_mode`,
  `invocations`, `tool_messages`, and `response_text` in its LLM context; and
  the recovery-decision prompt seed keeps teaching the
  default/expert/debug thinking-card-mode adaptation rules.
- [scripts/check_seed_bundle_version_bumps.py](../../scripts/check_seed_bundle_version_bumps.py)
  fails when a workflow seed bundle changed against the comparison ref
  (`origin/main` by default) without a strictly increasing integer
  `seed_version`. The pure helper that powers it lives in
  [src/backend/workflows/repo_seed_bundle_version_guard.py](../../src/backend/workflows/repo_seed_bundle_version_guard.py)
  and is also exercised by
  [tests/backend/test_seed_version_bump_guard.py](../../tests/backend/test_seed_version_bump_guard.py).

Run the CLI manually before pushing seed-bundle edits:

```powershell
pdm run python scripts/check_seed_bundle_version_bumps.py
```

If the guard fires, bump `seed_version` in each affected bundle so the runtime
gate in `src/backend/services/workflow_repo_seed_bootstrap.py` will republish
the change.

## 10. Maintaining This Guide

- Put durable practical lessons here when they are too detailed for
  `AGENTS.md` but likely to help future implementation work.
- Move mature domain-specific material into its own engineering document when it
  grows beyond a compact operational note.
- If a lesson is truly constitutional rather than practical, promote a shorter
  version into `AGENTS.md` instead.
