# Jira to Von task migration and project cutover

Last updated: 10 September 2026. Current delivery decision:
[JVNAUTOSCI-2737](https://naoinstitute.atlassian.net/browse/JVNAUTOSCI-2737).

## Full source retention and project cutover

The ordinary task projection and the retained Jira original serve different
purposes. A task count or a successful import does not establish full content
retention. `--preserve-source` keeps complete source documents, rendered fields,
field schemas/names, paginated comments, worklogs, history, properties, remote
links, votes/watchers, and original attachment bytes in immutable, hashed Von
file copies. Native task fields support day-to-day work; unsupported fields
remain retrievable in the original. Capture errors remain explicit and block a
full retention claim.

Each source project has a stable Von project record and an associated primary
task collection. Board and saved-filter snapshots can be associated with every
project they cover. Collections preserve their source definitions and selected
issue keys; native additions use explicit membership. They do not execute JQL
or reproduce Jira board automation. Project descriptions, properties, roles,
components, versions, workflow/status and security configuration are retained
as source information. Source sharing settings do not grant access in Von.

### Run and recover an import

Use the canonical runner with an explicitly configured local operator:

```sh
pdm run python scripts/run_jira_task_migration.py \
  --project-key PROJECT --actor-concept-id '#V#operator' \
  --preserve-source --include-done --passes 1 --batch-size 25 \
  --resume --report-path /private/audit/PROJECT.json
```

Supply `--organisation-concept-id` only for the intended organisational project.
Other projects can remain private to the operator. Source project IDs and site
identity distinguish projects; stable issue IDs and previous keys preserve
lookup after source moves. All issue types and terminal statuses are included
by this command. Do not use `--only-missing` when existing projections still
need original-content retention.

For attachments above the Jira proxy's default 10 MiB retrieval limit, set
`VON_INTERNAL_MCP_JIRA_ATTACHMENT_MAX_SIZE_BYTES=104857600` in the migration
process environment. This explicitly permits retrieval up to 100 MiB; larger
files remain reported gaps requiring a separate bounded capture.

Completed batches are checkpointed atomically. Repeating the command resumes a
matching source snapshot. If Jira has changed, use a fresh report path for the
delta and reconcile it against fresh full discovery. Reruns retain native edits
using the previous import projection, report conflicts, preserve prior source
archives, and deduplicate source activity. Unresolved hierarchy/link targets
can be repaired from retained import rows after all projects have imported.
Never infer deletion authority from a missing source issue.

Start large recovery runs with one importer process. Keep raw payloads in private
files and return compact progress summaries to the calling agent. Monitor host
memory pressure and the worker's memory use; stop the owned worker and preserve
its checkpoints when the host is under critical pressure. Increasing process
count is not a substitute for diagnosing a slow phase. Source registration and
task import coalesce derived relationship-index refreshes through the existing
bulk-mutation context, which flushes before the operation returns.
An issue or project source capture reuses one scoped MCP helper, while each
read retains its authority check, operation timeout and transport recovery.
The helper closes when that capture ends; unrelated calls keep their existing
one-call lifetime.

`scripts/capture_jira_project_sources.py` captures project and site source
configuration through named Jira MCP resources and associates board/filter
collections. It includes screen tabs and ordered fields, issue-type/screen
scheme mappings, and paginated field-configuration items. The service's
`capture_configuration_details` can supplement an existing site archive without
recapturing boards; retain the previous archive reference when associating the
supplement. Inspect the report for unsupported or inaccessible resources.
Classic configuration APIs can return empty results for team-managed projects;
that does not establish retention of their app-owned forms or configuration.
Automation exports, cloud exports, app-owned data, forms and linked external
documents require explicit capture or an evidence-backed scope disposition;
REST access failures are not proof that a resource is empty. Keep these private
source artefacts outside the repository.

Linked Atlassian Projects/Goals (formerly Atlas) can be read through the same
configured Jira account using named `atlas_project*` and `atlas_goal*` resources.
`capture_linked_atlassian_project` retains the selected project/goal fields and
paginated activity, custom values, access records and integration references as
source data. It checks the primary record again after capture. API errors,
changing records, missing/non-advancing pagination and unfinished nested pages
remain explicit gaps. A successful outer page does not certify nested comments
or custom-field values. These reads do not activate integrations or copy source
permissions into Von. Associate the retained archives with the intended native
projects and tasks, and keep unavailable fields in the disposition register.

### Native use and actor binding

The Tasks panel supports project/collection selection, ordinary task edits,
hierarchy, comments, attachments, worklogs and transitions. Imported tasks link
to their retained original without needing Jira. Native MCP tools expose the
same canonical services, including `task_list_projects`, `task_get_project`,
`task_get_collection`, `task_get`, `task_search` and `task_get_source_archive`.
`task_get` accepts legacy Jira keys as well as Von task concept IDs.

A local stdio connection can bind the trusted operator once in its server
environment:

```toml
[mcp_servers.vontology.env]
VON_MCP_TASK_ACTOR_CONCEPT_ID = "#V#operator"
VON_MCP_TASK_ORGANISATION_CONCEPT_ID = "#V#organisation"
```

The organisation is optional. This is local operator configuration, not a
client-supplied identity claim. It applies only to task tools, retains canonical
access checks and the configured write profile, rejects a conflicting actor
payload, and never replaces an inherited actor context. Browser and internal
workflow calls continue to derive identity from their trusted session context.

### Reconciliation, backup and writer selection

`reconcile_retained_project` compares fresh source IDs, keys, types and versions
with task membership, source relations, project originals and attachment hashes.
Keep its missing counts and discrepancy list alongside the import report. A
successful result proves the checked retention scope; it does not itself prove
the project is ready for cutover.

Run cross-project reconciliation in the operator's normal organisation context
so accessible organisational relation targets are included alongside private
projects. A narrower context can report a target as unavailable without proving
that it is absent.

Database backups do not include separately stored file bytes. Pair the encrypted
database backup with `scripts/backup_task_project_files.py`: export file copies
against the restored snapshot, then restore those bytes into an isolated blob
directory beside the restored database. The manifest binds the database receipt,
projects, tasks, file identities, sizes and hashes. File restore refuses the
source database and verifies every restored file through canonical reads. Check
that representative restored tasks can still retrieve their original content
and attachments without Jira.

Take the final paired snapshot after migration writes have settled. A database
dump spanning an in-progress file-copy creation can contain its concept before
its locator relations; a successful document restore alone does not establish
a usable content restore.

`set_task_project_writer` records a reversible, per-project writer decision and
its evidence. Moving to `von` requires successful project reconciliation plus
site-retention, normal-use, restore and final-delta receipts. The smallest
delivery evidence includes ordinary browser/MCP operations for the affected
project, no unexplained missing issues or broken source relations, retrievable
original content, and a verified final delta. Missing source bytes, incorrect
visibility, lost native edits, duplicate identity, or an unverified recovery
path block the corresponding cutover claim. Merely wanting broader analytics,
typed sprint modelling or Jira UI parity does not.

Until that decision is recorded, Jira remains the writer. Once it is `von`,
source importers refuse to overwrite or create task projections in that project;
coding agents use native task tracking and preserve Jira keys as aliases. Jira
access and retained archives remain available. Do not cancel Jira, delete its
data, or silently change collaborators' workflows as part of the writer switch.

## Historical implementation slices

The following records describe earlier bounded deliveries. Their completion
does not certify the current all-project migration.

## Scope

This slice adds workflow-first execution coverage for Jira->Von task migration by expressing the migration pipeline as a Vontology process graph that invokes internal MCP tools:

1. `jira_search` (project-scoped discovery)
2. `task_import_jira_issues` (issue mapping/import with idempotent reruns)

The runtime path is MCP-first and uses existing tooling (`jira_search`, `task_import_jira_issues`) rather than bespoke orchestration code.

## Execution Surface

For live project-scale execution, use `scripts/run_jira_task_migration.py`. It pages Jira discovery with `jira_search`, reuses the returned issue documents when calling `task_import_jira_issues`, and can optionally import missing JVNAUTOSCI referenced targets before a final repair pass.

This keeps the migration on the canonical Jira/Von MCP path while avoiding the earlier per-issue refetch bottleneck that made a single large project run operationally unreliable.

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

## JVNAUTOSCI-1407 Update (2026-03-09)

The migration path now has a shared incremental runner in
`src/backend/services/jira_task_migration_runner_service.py` so:

1. `scripts/run_jira_task_migration.py`
2. the durable workflow `#V#jira_task_incremental_import_workflow`
3. persisted schedules / ad-hoc workflow instances

all reuse the same discovery, batching, referenced-target repair, and
reporting logic.

New operational surfaces:

1. Recent-window incremental sync via `--updated-within-hours N`
2. One-off catch-up via `--min-issue-number` / `--max-issue-number`
3. Durable workflow action `jira_task_incremental_import.run_sync`

This keeps automation on the canonical Jira proxy -> `task_import_jira_issues`
gateway path while allowing a persistent schedule to keep importing newly
appearing Jira tasks without another bespoke sync mechanism.
