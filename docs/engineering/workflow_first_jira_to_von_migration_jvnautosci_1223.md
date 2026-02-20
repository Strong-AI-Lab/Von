# JVNAUTOSCI-1223 Workflow-First Jira -> Von Migration

Date: 2026-02-21

## Scope

This slice adds workflow-first execution coverage for Jira->Von task migration by expressing the migration pipeline as a Vontology process graph that invokes internal MCP tools:

1. `jira_search` (project-scoped discovery)
2. `task_import_jira_issues` (issue mapping/import with idempotent reruns)

The runtime path is MCP-first and uses existing tooling (`jira_search`, `task_import_jira_issues`) rather than bespoke orchestration code.

## Project-Level Parity Findings

`task_import_jira_issues` now emits `project_parity` in the migration report. The report is deterministic and grouped by Jira project key.

Per project, the report includes:

1. `mapped_fields` (current supported parity surface)
2. `dropped_fields` with explicit reason codes and sample values
3. status/priority mapping coverage (`observed`, `mapped`, `unmapped`)
4. `follow_up_work` recommendations for unresolved parity items

Current findings from the implemented parity analyser:

1. Supported at migration/report level:
   - project identity (`project.key`, `project.name`)
   - Jira status -> Von status mapping diagnostics
   - Jira priority -> Von priority mapping diagnostics
2. Explicitly deferred (reported with reasons when present):
   - `components`
   - `fixVersions`
   - sprint metadata
   - backlog rank metadata

## Follow-Up Work

The following remain open parity improvements under `JVNAUTOSCI-1108` scope:

1. First-class task component modelling and migration mapping.
2. First-class release/fix-version modelling.
3. First-class sprint/iteration modelling.
4. First-class backlog rank/order modelling.
5. Project-specific status/priority mapping expansions where unmapped values are observed.

These are surfaced directly in migration reports so pilot validation work (`JVNAUTOSCI-1114`) can prioritise closure using real project data.
