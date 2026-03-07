# Jira-GitHub Autofix Loop Workflow

Status: Design-only canonical VWL artefact  
Last updated: 2026-03-07 (Pacific/Auckland)  
Primary Jira task: `JVNAUTOSCI-1338`  
Canonical concept ID: `#V#jira_github_autofix_loop_workflow`

## Purpose

Define the canonical workflow-first design for a Codex-style Jira-to-GitHub autofix loop in Von.

This workflow is intended to:

- discover Jira issues that are explicitly eligible for automation,
- map them to an allowed GitHub repository and base branch,
- launch a guarded implementation path,
- publish the resulting GitHub metadata back to Jira,
- and persist auditable per-run and per-issue outcomes.

The design is intentionally KB-first. The workflow should live in Vontology and be executed by the lightweight VWL runtime. Supporting Python code should remain limited to reusable actions, validation, scheduling, and telemetry.

## Non-goals

- No bespoke Python orchestration service for this loop.
- No silent fallback to ad-hoc scripts when workflow data is missing.
- No unreviewed cross-repository or cross-namespace writes.
- No assumption that a tool success response is equivalent to completed remediation.

## Trigger Modes

The workflow supports two trigger modes:

1. Scheduled polling mode.
   Use `workflow_create_schedule` with a bounded interval and explicit default inputs.

2. Manual operator mode.
   Use `workflow_create_instance` directly, or `workflow_trigger_schedule` for an existing schedule.

The workflow MUST record which trigger mode launched the run.

## Required Inputs

- `jira_jql`: bounded JQL used to discover candidate issues.
- `required_labels`: explicit label guard for autofix eligibility.
- `repository_owner`: GitHub owner or organisation.
- `repository_name`: GitHub repository.
- `base_branch`: target base branch for generated work.
- `issue_batch_cap`: maximum issues processed in one run.
- `per_issue_write_mode`: `dry_run|proposal_only|guarded_write`.
- `allowed_components`: optional Jira component filter.
- `allowed_issue_types`: optional Jira issue-type filter.
- `namespace`: authoritative namespace for the run.
- `actor_concept_id`: operator or workflow actor identity.

## Canonical Context Keys

The workflow SHOULD use the following stable context keys:

- `run_correlation_id`
- `trigger_mode`
- `jira_candidate_issues`
- `jira_filtered_issues`
- `current_issue`
- `current_issue_index`
- `issue_outcomes`
- `github_repository`
- `github_branch_name`
- `github_pr_url`
- `github_pr_number`
- `github_copilot_job_id`
- `approval_required`
- `approval_state`
- `idempotency_key`
- `run_summary`
- `blocked_reason`
- `failure_diagnostics`

These keys are intended to remain stable so later runtime features can rely on them without rewriting the design.

## End-to-End Step Contract

### 1. Discover eligible Jira issues

Tool surface:

- `jira_search`

Outputs:

- `jira_candidate_issues`

Rules:

- Query must be bounded by `issue_batch_cap`.
- Only explicitly labelled issues are eligible.
- Discovery must fail closed on Jira auth or query errors.

### 2. Filter and normalise candidate issues

Purpose:

- remove issues already in flight,
- remove issues outside the allow-list policy,
- produce deterministic per-issue input envelopes.

Outputs:

- `jira_filtered_issues`
- `failure_diagnostics`

Required checks:

- existing PR link or existing branch marker,
- missing repo mapping,
- missing write approval,
- disallowed repository,
- missing required label at execution time.

### 3. Iterate over eligible issues

Conceptual behaviour:

- for each filtered issue, execute the same bounded per-issue subworkflow,
- preserve deterministic ordering,
- collect one outcome envelope per issue.

Current status:

- this is the major declarative gap in current VWL and should be implemented via `JVNAUTOSCI-1339`, not by hand-written Python control flow.

### 4. Per-issue subworkflow

For each issue, the subworkflow should perform:

1. Read issue details and comments.
2. Derive a deterministic `idempotency_key`.
3. Resolve repository, owner, and base branch.
4. Re-check for open PRs or existing automation markers.
5. If no write is allowed, emit a blocked outcome and continue.
6. Launch the implementation path.
7. Persist GitHub job, branch, and PR metadata.
8. Comment the resulting status back to Jira.

Preferred implementation path:

- `github_create_pull_request_with_copilot`

Fallback write path:

- `github_create_branch`
- file mutation or branch update tools only when the policy allows them and the workflow explicitly declares that branch strategy.

Read-only evidence path:

- `github_list_pull_requests`
- `github_issue_read`
- `github_pull_request_read`
- `github_list_branches`

### 5. Summarise run outcome

Outputs:

- `issue_outcomes`
- `run_summary`

The final summary MUST contain:

- discovered issue count,
- filtered issue count,
- processed issue count,
- blocked issue count,
- successful launch count,
- failed issue count,
- stable references to any PR URLs or Copilot job IDs.

## Idempotency Model

Each per-issue execution MUST derive a stable idempotency key from:

- `issue_key`
- `workflow_id`
- `repository_owner`
- `repository_name`
- `base_branch`
- and a bounded scheduling window or explicit manual-run token.

The workflow MUST treat the following as duplicate-protection signals:

- an open PR already linked to the issue,
- a cached job ID still active for the same idempotency key,
- a deterministic branch name already present.

## Fail-Closed Behaviour

The workflow MUST no-op with explicit diagnostics when any of the following are missing:

- Jira auth,
- GitHub auth,
- repository allow-list approval,
- namespace,
- label guard,
- required workflow metadata,
- durable per-issue loop support.

Blocked states are valid outcomes. Silent degradation is not.

## Operator Controls

Operators must be able to:

- enable or disable a schedule,
- trigger a schedule manually,
- cancel a running workflow instance,
- retry a failed workflow instance,
- inspect current status and stored outputs.

Control surface:

- `workflow_list_schedules`
- `workflow_get_schedule`
- `workflow_set_schedule_enabled`
- `workflow_trigger_schedule`
- `workflow_list_instances`
- `workflow_get_instance`
- `workflow_cancel_instance`
- `workflow_retry_instance`

## Observability Requirements

Every run should emit:

- `run_correlation_id`
- `issue_key`
- `current_issue_index`
- `tool_name`
- `tool_request_id` where available
- `idempotency_key`
- `approval_state`
- `branch_name`
- `pr_url`
- `copilot_job_id`
- `blocked_reason`
- `failure_diagnostics`

The summary and per-issue envelopes should be stored so an operator can reconstruct:

- what was attempted,
- what was skipped,
- what was blocked by policy,
- and what external state was created.

## Current Capability Assessment

Executable today with existing surfaces:

- scheduled and manual launch,
- Jira discovery and Jira commenting,
- guarded GitHub proxy invocation,
- durable workflow instance management,
- high-level run summary persistence.

Not cleanly expressible yet without follow-on VWL work:

- issue-level `for_each` fan-out,
- declarative per-step approval gates,
- explicit retry/backoff policy declarations,
- declarative idempotency annotations,
- long-horizon resumable per-issue checkpoints,
- completion gates that prove all declared deliverables were met.

## Follow-on Mapping

- `JVNAUTOSCI-1339`: iterator semantics, richer conditions, approval gates, retries, idempotency declarations.
- `JVNAUTOSCI-1358`: prompt and tool metadata resolution for deterministic workflow authoring.
- `JVNAUTOSCI-1359`: plan-state, resumable checkpoints, and completion gates for long-running runs.
- `JVNAUTOSCI-1360`: optional future import or execution of external SKILL artefacts against the stabilised VWL surface.

## Conformance Note

This workflow is deliberately specified as a Vontology-first design artefact. When it becomes executable, the implementation should extend the same canonical workflow concept rather than introducing a parallel Python-specific orchestration path.
