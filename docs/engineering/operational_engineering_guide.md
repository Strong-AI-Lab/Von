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

### 6.3 GitHub

- Prefer Von's internal GitHub MCP proxy and its guardrails for GitHub access.
- Keep write behaviour fail-closed and allow-list aware.

See:

- `docs/engineering/github_internal_mcp_runbook.md`

### 6.4 Vontology and MCP field notes

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
