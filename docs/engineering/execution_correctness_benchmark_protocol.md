# Execution-Correctness Benchmark Protocol

**Status**: Active engineering protocol  
**Primary Jira task**: `JVNAUTOSCI-967`  
**Phase context**: `JVNAUTOSCI-1657` Phase 1

## 1. Purpose

This document is the operating standard for Phase 1 execution-correctness evaluation.

Its purpose is to ensure that Von's Phase 1 measurement work behaves as one layered evaluation stack rather than as several disconnected benchmark efforts.

In practice, this protocol ties together:

- the shared execution-correctness schema and label surface from `JVNAUTOSCI-965`
- the selector-routing benchmark layer from `JVNAUTOSCI-1664`
- the pre-dispatch latency attribution surfaces from `JVNAUTOSCI-1429`
- the reporting and regression views from `JVNAUTOSCI-966`

Later benchmark overlays should continue to attach to this same stack rather
than creating parallel benchmark families. The current example is the
minimal-imposition assessment model documented in
[minimal_imposition_benchmark_model.md](./minimal_imposition_benchmark_model.md),
which reuses the same turn-execution and dashboard surfaces.

## 2. What This Protocol Governs

This protocol governs:

- how we classify turn outcomes
- how we run the selector-layer and turn-layer benchmarks
- when benchmark results are informative only
- when benchmark results should gate workflow, selector, and release-adjacent changes
- how `JVNAUTOSCI-833`, `JVNAUTOSCI-964`, and `JVNAUTOSCI-536` divide responsibility in Phase 1

This protocol does not make repo-side markdown or seed files authoritative for behaviour. Behaviour remains workflow-first and KB-governed. This document is an operator guide for the current Phase 1 measurement surfaces.

## 3. Layered Evaluation Model

Phase 1 uses a layered model:

### 3.1 Selector-routing layer

Question:

Did Von choose the right workflow or the right safe abstention route for the user's intent?

Primary surface:

- `turn_execution_build_selector_benchmark`

Primary epic:

- `JVNAUTOSCI-833`

Typical failures:

- routed to `#V#chat_assistant_workflow` when tool or workflow execution was required
- failed to route to a specialised workflow
- failed to abstain or escalate when no safe executable route existed

### 3.2 Turn-level execution layer

Question:

Did Von actually carry out the intended action, rather than merely narrating completion?

Primary surfaces:

- `turn_execution_list`
- `turn_execution_get`
- `turn_execution_search_failures`
- `turn_execution_build_benchmark`

Primary epic:

- `JVNAUTOSCI-964`

Typical failures:

- false success
- unresolved required effects
- blocked or failed mutation
- incorrect follow-up handling

### 3.3 Latency-attribution layer

Question:

Where did the user-visible time go, especially between workflow discovery and actual dispatch?

Primary surfaces:

- `dispatch.pre_dispatch` routing diagnostics
- `workflow_dispatch_prepare` stage telemetry
- pre-dispatch latency summaries in `turn_execution_build_benchmark`
- trend and drill-down views in `turn_execution_build_dashboard`

Primary epic:

- `JVNAUTOSCI-536`

Typical failures:

- long unexplained pre-dispatch corridor
- latency regression hidden inside coarse routing totals
- non-comparable timing because stage attribution is missing or inconsistent

### 3.4 Shared reporting layer

Question:

Can we inspect selector correctness, end-to-end execution correctness, and latency on one joined surface?

Primary surface:

- `turn_execution_build_dashboard`

This is a composition layer, not a separate benchmark family.

## 4. Shared Outcome Taxonomy

### 4.1 Core rule

Every persisted turn record should expose:

- one primary `overall_outcome`
- one detailed `failure_mode`
- one `metric_labels` object for cross-cutting reporting

The `overall_outcome` is the dominant per-turn verdict.

The `metric_labels` are not mutually exclusive. A turn can be both `tool_or_workflow_misrouting=true` and `unresolved_follow_up_needed=true`, for example.

### 4.2 Phase 1 labels

### `successful_completion`

Use when:

- the selected route was appropriate for the intent, and
- required effects were satisfied or no mutation was required, and
- the assistant did not claim completion prematurely

Do not use when:

- a user-visible request remains unresolved
- required effects are blocked, missing, or inconclusive

### `false_success`

Use when:

- Von claimed or implied completion, or the completion gate marked the turn complete, but the required effect was not actually verified

Common examples:

- a mutation was described as done but no mutation evidence exists
- a read-only lookup happened on a mutation-required turn and the turn still completed
- a required postcondition remained unresolved but the response sounded complete

### `unresolved_follow_up_needed`

Use when:

- the turn did not safely complete the intended work and user-visible follow-up, retry, or continuation is still needed

This label is about unresolved user intent, not about why the failure happened.

### `tool_or_workflow_misrouting`

Use when:

- the wrong workflow was selected, or
- the route prevented the correct tool or workflow path from being taken

Examples:

- plain chat route used when tools were required
- generic tool path used when a specialised workflow should have run

### `abstain_escalate_no_safe_route`

Use when:

- no safe executable route existed, and
- Von correctly abstained, escalated, or routed into a recovery path instead of hallucinating capability

This is not a failure label by itself. It is the correct outcome for a no-safe-route case.

### 4.3 Review precedence

Apply these precedence rules during review:

1. Decide whether the turn was actually completed.
2. If not completed, decide whether Von incorrectly claimed completion.
3. Then decide whether the main cause was misrouting, execution failure, or correct abstention.
4. Preserve the cause in `failure_mode` and cross-cutting booleans in `metric_labels`.

Practical interpretation:

- `false_success` is about incorrect completion signalling.
- `tool_or_workflow_misrouting` is about route selection or route-shape failure.
- `unresolved_follow_up_needed` is about the user's state after the turn.
- `abstain_escalate_no_safe_route` is the safe-route success case for unsupported work.

### 4.4 Review consistency rules

During replay review:

- do not label from assistant prose alone; use the persisted turn record and routing diagnostics
- do not mark `successful_completion` if a required effect is unresolved, even if the answer looks sensible
- prefer `abstain_escalate_no_safe_route` over `tool_or_workflow_misrouting` when the route correctly refused unsafe execution
- if multiple reviewers disagree, keep the stricter interpretation until the schema or workflow evidence clarifies the case

## 5. Authoritative and Supporting Artefacts

### 5.1 Authoritative runtime and KB artefacts

For Phase 1, the authoritative artefacts are:

- persisted `turn_execution_record` payloads in `chat_history.history.$.llm_debug_data.turn_execution_record`
- projected `turn_execution_records`
- workflow-instance inputs, runtime state, and outputs for the turn-execution path
- the current workflow concepts identified in the schema proposal:
  - `#V#conversation_turn_execution_workflow`
  - `#V#kb_mutation_postcondition_critic_workflow`
  - `#V#turn_completion_gate_workflow`
- runtime telemetry emitted on the actual orchestrator and workflow path, including `workflow_dispatch_prepare`

These artefacts are the basis for classification and regression evidence.

### 5.2 Supporting code surfaces

The main supporting code surfaces are:

- `src/backend/services/turn_execution_record_service.py`
- `src/backend/services/workflow_selector_benchmark_service.py`
- `src/backend/integrations/internal_mcp/catalogue.py`
- `src/backend/mcp_server/mcp_stdio_server.py`

These surfaces expose and compose the measurements. They are not the authority for the intended workflow behaviour.

### 5.3 Temporary non-authoritative repo artefacts

The following are currently useful but non-authoritative:

- `docs/engineering/execution_correctness_benchmark_protocol.md`
- `docs/engineering/turn_execution_completion_schema_proposal.md`
- `src/backend/workflows/repo_seed_bundles/selector_routing_benchmark_seed_bundle.json`

The selector seed bundle is acceptable for Phase 1 as a seed corpus, test fixture, and export-like engineering artefact. It should not be treated as the final long-term authority for selector benchmark content if the corpus later moves into KB-managed benchmark assets.

## 6. Standard Run Procedure

Use this procedure whenever a change may affect workflow selection, completion correctness, or latency attribution.

### 6.1 Readiness check

1. Confirm the relevant instrumentation exists on the changed path.
2. Run `turn_execution_namespace_coverage_report` if there is any doubt about projection or namespace coverage.
3. If needed, run `turn_execution_backfill_from_chat_history` in dry-run mode first, then in write mode only if coverage gaps make the benchmark misleading.

If coverage is incomplete or projections are stale, results are informative only.

### 6.2 Selector-layer run

Run:

- `turn_execution_build_selector_benchmark`

Review:

- selector accuracy
- abstain/no-safe-route correctness
- replay cases marked as `tool_or_workflow_misrouting`

Use this layer to answer whether route selection changed for the better or worse.

### 6.3 Turn-level run

Run:

- `turn_execution_build_benchmark`

Review:

- `successful_completion`
- `false_success`
- `unresolved_follow_up_needed`
- `tool_or_workflow_misrouting`
- `abstain_escalate_no_safe_route`
- detailed `failure_mode` counts
- replay cases and issue-key extraction

Use baseline comparison when a stable prior rate exists and the change is large enough to justify regression judgement.

### 6.4 Joined dashboard run

Run:

- `turn_execution_build_dashboard`

Review:

- selector-layer and turn-layer results together
- pre-dispatch latency summaries
- day-bucket trend views
- drill-downs into replay cases and Jira-linked triage evidence

This is the default review surface for engineering judgement once the individual layers have run.

### 6.5 Triage output

For any material regression:

1. inspect the replay cases first
2. determine whether the root cause is route selection, execution correctness, or observability/latency
3. attach the resulting evidence to the responsible Jira task or epic

## 7. When Results Are Informative Only

Treat results as informative only when any of the following hold:

- coverage is incomplete or stale
- the selector corpus has changed but not yet been reviewed
- the benchmark is being used on a path with known missing instrumentation
- the sample size is too small to support a regression claim
- the changed work is exploratory and has no agreed baseline yet

Informative results should still be recorded and discussed. They just should not block merges or workflow publication by themselves.

## 8. When Results Should Gate Changes

The Phase 1 benchmark should gate changes when all of the following are true:

- the changed path is covered by the relevant instrumentation
- the benchmark corpus and replay cases are reviewable
- a meaningful baseline exists
- the change affects workflow routing, completion semantics, or latency attribution on a real user path

### 8.1 Changes that should normally be gated

- workflow selector logic changes
- workflow publication or routing-metadata changes that can alter selected workflow
- orchestrator changes that affect completion, dispatch, or postcondition semantics
- changes to completion-gate or critic logic
- latency-attribution changes that alter pre-dispatch visibility or timing interpretation

### 8.2 Minimum gate expectations

For workflow-routing changes:

- no material regression in selector accuracy
- no unjustified increase in `tool_or_workflow_misrouting`

For completion-path changes:

- no unjustified increase in `false_success`
- no unjustified increase in `unresolved_follow_up_needed`

For observability or latency changes:

- no loss of timing attribution fidelity
- no unexplained regression in pre-dispatch latency metrics

If a change intentionally worsens one metric to improve a more important one, that trade-off must be explicit in Jira and supported by replay evidence.

## 9. Responsibility Split Across Epics

Phase 1 responsibility is deliberately split as follows.

### `JVNAUTOSCI-833` Agentic Behaviours

Owns:

- selector-routing benchmark behaviour layer
- expected workflow-choice and safe-abstention cases
- route-shape regressions that start before tool execution

Current concrete task:

- `JVNAUTOSCI-1664`

### `JVNAUTOSCI-964` Evaluation and Measurement

Owns:

- shared execution-correctness taxonomy
- turn-level benchmark logic
- dashboard and regression reporting
- operating protocol and review rules

Current concrete tasks:

- `JVNAUTOSCI-965`
- `JVNAUTOSCI-966`
- `JVNAUTOSCI-967`

### `JVNAUTOSCI-536` Observability and Performance

Owns:

- timing and stage-attribution substrate
- latency diagnostics required to interpret performance regressions
- pre-dispatch corridor visibility

Current concrete anchor task:

- `JVNAUTOSCI-1429`

## 10. Review Cadence

Use this cadence during Phase 1:

- run the relevant benchmark layer before merging substantial routing, completion, or observability changes
- update Jira with replay-backed findings whenever a benchmark exposes a meaningful regression or coverage gap
- use `JVNAUTOSCI-1660` as the formal review-and-plan-update gate after the Phase 1 tranche is complete

For larger workstreams, prefer one concise progress comment on the parent plan task rather than scattering status across many planning issues.

## 11. Current Limits and Follow-On Expectations

Current limits:

- the selector benchmark corpus is still maintained as a repo seed bundle
- not every path yet has equally strong projection or latency coverage
- the dashboard is an engineering review surface, not a polished product dashboard

Expected follow-on:

- move from seed-corpus management toward more explicitly governed benchmark assets when the KB/tooling path is ready
- tighten gate thresholds once coverage and baselines are more stable
- feed Phase 1 findings into `JVNAUTOSCI-1660`, then into the Phase 2 KB-substrate work

## 12. Closure Rule for Phase 1

Phase 1 should be considered operationally complete only when:

- selector routing, turn execution, and latency attribution are reviewable on one coherent measurement stack
- the shared labels are being reused consistently
- benchmark output is strong enough to influence merge and workflow-change decisions
- `JVNAUTOSCI-1660` has reviewed the tranche and updated `JVNAUTOSCI-1657`
