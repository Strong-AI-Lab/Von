# JVNAUTOSCI-1114 Jira->Von Pilot Validation

Date: 2026-02-20

## Goal

Validate that Von can operate as the primary task system for JVNAUTOSCI-style workflows after Jira import, with explicit classification of blocking vs deferrable parity gaps.

## Runtime Output

`task_import_jira_issues` now returns:

1. `project_parity`: per-project mapped/dropped parity details.
2. `pilot_validation`: deterministic classification and recommendation payload.

`pilot_validation` fields:

1. `summary.issues_scanned`
2. `summary.must_fix_gap_count`
3. `summary.acceptable_defer_gap_count`
4. `recommendation` (`go`, `go_with_conditions`, `no_go`)
5. `must_fix_gaps[]`
6. `acceptable_defer_gaps[]`

## Classification Policy

1. `must_fix`: unmapped Jira status/priority values (`status_mapping`, `priority_mapping`).
2. `acceptable_defer`: planning-model gaps where values are preserved and queryable but not yet represented as typed planning entities:
   - components
   - fix versions
   - sprint values
   - backlog rank

## Recommendation Policy

1. `no_go`: one or more `must_fix` gaps are present.
2. `go_with_conditions`: zero `must_fix` gaps, but one or more `acceptable_defer` gaps remain.
3. `go`: no detected gaps in the scanned sample.

## Real Backlog Alignment Notes

Pilot checks against JVNAUTOSCI project data showed real statuses not covered by the original mapper. The importer now maps:

1. `SUPERSEDED` -> `cancelled`
2. `Won't Fix` / `Wont Fix` -> `cancelled`
3. `SUSPENDED` -> `blocked`

Planning metadata observed in Jira (`components`, `rank`, and where present `fixVersions`/sprints) is now mapped into Von task metadata fields and exposed through REST and MCP search/update pathways.
