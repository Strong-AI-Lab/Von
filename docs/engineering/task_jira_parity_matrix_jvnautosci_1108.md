# JVNAUTOSCI-1108 Task Parity Matrix (Jira -> Von/Vontology)

## Scope
This matrix defines the current parity position for Jira-style task operations in Von's Vontology-backed task model.

## Parity Matrix
| Jira task field/semantic | Von/Vontology representation | Status | Notes |
|---|---|---|---|
| Issue key / identifier | `task_concept_id` (`#V#task_*`) | Supported | Stable concept ID used across API and MCP. |
| Summary | `#V#hasName` text relation | Supported | Exposed in REST and MCP reads/writes. |
| Description | `#V#hasDescription` text relation | Supported | Exposed in REST and MCP reads/writes. |
| Status | `#V#hasTaskStatus` text relation | Supported | Transition graph + transition operations available. |
| Priority | `#V#hasPriority` text relation | Supported | `low/medium/high/critical`. |
| Assignee | `#V#hasAssignee` relationship | Supported | Single assignee currently. |
| Reporter/creator | `#V#hasCreatedBy` relationship | Supported | Captured on create when available. |
| Parent/subtask | `#V#hasParentTask` + `#V#hasSubtask` | Supported | Cycle protection in parent assignment path. |
| Epic linkage | `#V#hasEpicTask` relationship | Supported | CRUD/query available in service, REST, MCP. |
| Labels | `metadata.labels` | Supported | Normalised + deduplicated list semantics. |
| Components | `metadata.components` | Supported | Imported from Jira `components` and exposed in REST/MCP search and updates. |
| Fix versions | `metadata.fix_versions` | Supported | Imported from Jira `fixVersions` and exposed in REST/MCP search and updates. |
| Sprint values | `metadata.sprint_values` | Supported | Imported from Jira sprint fields and exposed in REST/MCP search and updates. |
| Backlog rank | `metadata.backlog_rank` | Supported | Imported from Jira rank fields and exposed in REST/MCP search and updates. |
| Start date | `#V#hasStartDate` text relation | Supported | CRUD/query available in service, REST, MCP. |
| Due date | `#V#hasDueDate` text relation | Supported | CRUD/query available in service, REST, MCP. |
| Typed dependencies | `#V#dependsOnTask`, `#V#blocksTask`, etc. | Supported | Link/unlink + dependency-state filtering. |
| Comments | `metadata.comments` | Supported | Add/list operations and history event entries. |
| Attachments | `metadata.attachments` | Supported | Add/list operations with attachment metadata. |
| Transition history | `metadata.task_history` events | Supported | Transition + field update + linkage changes recorded. |
| Search/query | `search_tasks` filters + `/api/tasks/search` + `task_search` | Supported | Includes hierarchy, labels, start/due windows, dependency state. |

## Migration Readiness (JVNAUTOSCI Tasks)
Recommended mapping from Jira issue export/import payloads:
1. `summary` -> `title`
2. `description` -> `description`
3. `status.name` -> `status` (mapped to Von status vocabulary)
4. `priority.name` -> `priority`
5. `assignee.accountId` -> `assignee_concept_id`
6. `labels[]` -> `labels[]`
7. `duedate` -> `due_date`
8. `custom/startDate` (or equivalent) -> `start_date`
9. `parent.key`/subtask parent -> `parent_task_concept_id`
10. `epic`/`parent epic` -> `epic_task_concept_id`
11. `issuelinks[]` -> typed task links (`depends_on`, `blocks`, `relates_to`, etc.)
12. `comments[]` -> `metadata.comments`
13. `attachments[]` -> `metadata.attachments`
14. `changelog/status transitions` -> `metadata.task_history`
15. `components[]` -> `metadata.components`
16. `fixVersions[]` -> `metadata.fix_versions`
17. `sprint/customfield_10020` -> `metadata.sprint_values`
18. `rank/customfield_10019` -> `metadata.backlog_rank`

## Pilot Validation Surface (JVNAUTOSCI-1114)
`task_import_jira_issues` now emits:
1. `project_parity`: per-project mapped/dropped parity details.
2. `pilot_validation`: deterministic `must_fix` vs `acceptable_defer` gap classification plus `go` / `go_with_conditions` / `no_go` recommendation.

## Remaining Gaps and Phased Closure
1. Multi-party collaboration roles (watchers/collaborators) are not first-class yet.
2. Resolution semantics remain status-coupled (no separate canonical resolution model).
3. Planning fields are represented as task metadata; full typed ontology modelling for components/releases/sprints/rank remains deferred.
