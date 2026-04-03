# Minimal Imposition Benchmark Model

**Status**: Implemented baseline model  
**Date**: 2026-04-04

## Purpose

This document defines the first explicit **minimal-imposition benchmark model**
used by Von's autopilot-oriented benchmark surfaces.

It is the evaluation-side counterpart to runtime minimal-imposition work such as
write-policy and escalation behaviour. The implementation intentionally reuses
the existing turn-execution benchmark and dashboard stack rather than creating a
parallel evaluator.

Current reporting surfaces:
- `turn_execution_build_benchmark`
- `turn_execution_build_dashboard`

Authoritative benchmark-model state:
- canonical Vontology profile type: `#V#minimal_imposition_benchmark_profile`
- canonical profile concept: `#V#minimal_imposition_benchmark_profile_autopilot_v1`
- canonical profile predicates:
  - `#V#has_minimal_imposition_benchmark_profile`
  - `#V#has_minimal_imposition_benchmark_profile_json`

## Model structure

The current model treats imposition as a **weighted family of dimensions** rather
than a single unstructured scalar.

Each dimension has:
- a semantic description
- a current evidence source
- an evidence kind: `direct`, `proxy`, or `missing`
- thresholds for `good`, `caution`, and `high`
- a weight in the composite score

The composite is:
- `weighted_cost_pct`: `0` = lowest observed imposition, `100` = highest
- `weighted_score_pct`: `100 - weighted_cost_pct`

Missing dimensions are **excluded from the weighted average and reported
explicitly**. This is deliberate: the current runtime does not yet expose all
the evidence needed for a full imposition taxonomy, and the benchmark should
not hide that gap.

## Dimension inventory

Current canonical dimensions:

1. `user_interruption_burden`
   Current evidence: direct
   Metric: unresolved follow-up turn rate

2. `clarification_burden`
   Current evidence: proxy
   Metric: unresolved follow-up turn rate
   Note: explicit clarification taxonomy is not yet instrumented

3. `approval_burden`
   Current evidence: proxy
   Metric: escalation-signal rate
   Note: current telemetry does not cleanly separate approval prompts from other escalation paths

4. `workflow_process_disruption`
   Current evidence: direct
   Metric family: likely-failure rate plus selector misrouting contribution when available

5. `normative_overreach`
   Current evidence: proxy
   Metric family: abstain/escalate-no-safe-route outcomes and escalation signal pressure

6. `epistemic_intrusion`
   Current evidence: missing
   Missing metric: explicit detection that the system asked for information that should have been recoverable from prompt, repo, KB, or tools

7. `reversibility_recovery_cost`
   Current evidence: proxy
   Metric family: false-success rate plus unresolved follow-up rate

8. `unsafe_under_escalation`
   Current evidence: direct
   Metric: false-success rate

9. `unnecessary_over_escalation`
   Current evidence: proxy
   Metric family: abstain/escalate-no-safe-route and escalation-signal burden

## Scenario families

The benchmark profile also records the scenario families the suite is meant to
cover:

- low-risk additive internal writes
- reversible updates
- destructive actions
- high-fan-out changes
- external-system writes
- ambiguous intent
- low-confidence evidence
- organisation policy conflicts
- cases where asking the user is itself a significant imposition

These families are not a separate benchmark runner. They are the intended
coverage map for the replay/evidence stack already used by turn-execution
benchmarking.

## Current evidence interpretation

The baseline model is intentionally honest about current evidence quality.

What is already measured reasonably well:
- interruption burden
- failure-to-act / workflow disruption
- false success as unsafe under-escalation
- some escalation burden

What is still only proxied:
- clarification burden
- approval burden
- normative overreach
- reversibility/recovery cost
- unnecessary over-escalation

What is not yet measured directly:
- epistemic intrusion

This means the current composite score is **useful but incomplete**. It is
appropriate for trend tracking, replay comparison, and regression discussion,
but not yet for hard doctrinal gating.

## Telemetry gaps

The main missing telemetry categories are:

- explicit interruption taxonomy
  - clarification ask
  - approval ask
  - escalation without user question
  - no-ask machine-side resolution

- provenance-exhaustion / epistemic-intrusion telemetry
  - whether prompt/repo/KB/tool context was exhausted before the system asked

- over-escalation rationale telemetry
  - enough structured evidence to distinguish justified abstention from brittle conservatism

These gaps should be tracked as implementation work rather than being patched
with stronger heuristics inside the benchmark.

Tracked follow-up:
- `JVNAUTOSCI-1678` for the missing minimal-imposition telemetry dimensions

## Relation to other benchmark surfaces

The minimal-imposition model is layered onto, not separate from:

- execution correctness (`JVNAUTOSCI-965`)
- selector-routing benchmark (`JVNAUTOSCI-1664`)
- pre-dispatch latency attribution (`JVNAUTOSCI-1429`)
- dashboard/reporting (`JVNAUTOSCI-966`)

This keeps one shared benchmark stack with multiple evaluation dimensions:
- correctness
- routing
- latency
- imposition

## Interpretation guidance

The benchmark should not be read as:
- "lower interruption is always better"
- "over-escalation and under-escalation are interchangeable"
- "a good composite score proves the policy is safe"

It should be read as:
- a structured way to quantify burden-shifting and disruption
- a way to compare safety and imposition together
- a way to reveal which parts of minimal-imposition evaluation are still missing
