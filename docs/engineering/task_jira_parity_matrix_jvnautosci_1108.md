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

## Remaining Gaps and Phased Closure
1. Multi-party collaboration roles (watchers/collaborators) are not first-class yet.
2. Resolution semantics remain status-coupled (no separate canonical resolution model).
3. Release planning fields (fix version, sprint, components, rank) are not yet first-class.
4. Dedicated Jira import/export tooling with fidelity metrics is still required.
