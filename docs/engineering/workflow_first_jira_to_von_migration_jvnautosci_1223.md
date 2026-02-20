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
   - `components` -> task metadata mapping
   - `fixVersions` -> task metadata mapping
   - sprint metadata -> task metadata mapping
   - backlog rank metadata -> task metadata mapping

## Follow-Up Work

The following remain open parity improvements under `JVNAUTOSCI-1108` scope:

1. Typed planning ontology modelling (components/releases/sprints/rank) beyond metadata-level storage.
2. Project-specific status/priority mapping expansions where unmapped values are observed.

These are surfaced directly in migration reports so pilot validation work (`JVNAUTOSCI-1114`) can prioritise closure using real project data.

## JVNAUTOSCI-1114 Update (2026-02-20)

Pilot validation now adds `pilot_validation` to the importer report with:

1. Deterministic gap classification (`must_fix`, `acceptable_defer`).
2. Recommendation output (`go`, `go_with_conditions`, `no_go`).
3. Expanded Jira status mapping for real JVNAUTOSCI backlog states:
   - `SUPERSEDED` -> `cancelled`
   - `Won't Fix` -> `cancelled`
   - `SUSPENDED` -> `blocked`
