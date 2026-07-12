# JVNAUTOSCI-1963 Isolated Self-Improvement Worlds Architecture - 23 April 2026

> **Document status: Long-horizon design with a dated implementation snapshot.**
> The design may remain useful. Claims using “current”, “already”, “still”,
> source anchors, and task priority describe the April 2026 evidence base and
> require live revalidation. This is not release-status authority.

## Purpose

This note records the concrete self-improvement-world architecture for Von after
the represented self-improvement and proposal-addressability landings through
`JVNAUTOSCI-1987`, `JVNAUTOSCI-1993`, `JVNAUTOSCI-1994`, `JVNAUTOSCI-1962`,
and `JVNAUTOSCI-1964`.

`JVNAUTOSCI-1963` is not a task to "add self-improvement" in the abstract. The
live code already has a real represented self-improvement loop and a separate
testing or experiment substrate. The actual gap is narrower and more important:
those two lines are still only loosely connected.

Von can already:

- generate represented workflow-improvement proposals from critique memory;
- store proposal-addressable procedural candidates and promotion evaluations;
- run experiment specs and experiment runs with verdict computation;
- use testing theories for local assertions, diffing, promotion, and rollback.

What it still lacks is one coherent release-engineering architecture that turns
those pieces into isolated candidate worlds with explicit:

- baseline-vs-candidate identity;
- evaluator- and memory-backed comparison;
- shadow or bounded pre-activation validation;
- rollback-safe promotion gates; and
- a path beyond workflow-only candidates into prompt, policy, and retrieval
  candidates.

This note therefore does two things:

1. states the self-improvement-world architecture that now matches the live code
   and the design intent in `docs/engineering/Von_for_AgenticAI.md`; and
2. turns that architecture into the first concrete implementation slices rather
   than leaving `1963` as a broad umbrella.

## Current Live Substrate

The current Von code already contains the following self-improvement-capable
surfaces:

- Represented workflow-improvement context building, proposal submission,
  promotion-context construction, and promotion-evaluation persistence in
  `src/backend/services/episode_self_improvement_service.py:440`, `:555`,
  `:638`, and `:706`.
- Critique-triggered launch of the represented self-improvement loop in
  `src/backend/workflows/durable/episode_evaluation_workflow.py:318`.
- Proposal-addressable workflow candidate validation, promotion evaluation,
  approval review, and rollback in
  `src/backend/workflows/workflow_studio_service.py:1684`, `:2000`, `:2093`,
  and `:2251`.
- Represented experiment spec creation, experiment-run execution, and
  experiment verdict computation in
  `src/backend/services/experiment_run_service.py:1046`, `:1181`, and `:1688`.
- Testing-theory import, local assertion, diff, rollback, and validated-claim
  promotion in `src/backend/services/testing_theory_service.py:363`, `:420`,
  `:478`, `:573`, and `:625`.
- Benchmark reporting over critique memories in
  `src/backend/services/episode_critique_benchmark_service.py:1393`.

The problem is not absence. The problem is separation:

- the represented self-improvement loop still builds promotion context mostly
  from critique memory plus a benchmark summary rather than from explicit
  candidate-world evidence;
- the experiment-run substrate already has `baseline_workflow_id`,
  `candidate_workflow_ids`, `benchmark_world_id`, degradation assessment, and
  promotion recommendation, but the main self-improvement loop does not yet use
  it as its default control surface;
- testing theories still sit more in the testing line than in the main
  self-improvement release path; and
- the live self-improvement path is still mostly workflow-only even though the
  design note calls for prompt, retrieval-policy, and policy-memory revision as
  well.

## Architecture Decision

### 1. Self-Improvement Should Be Treated as Release Engineering

Von should not model self-improvement as opaque in-agent recursion or as a set
of candidate artefacts that immediately compete for canonical status.

The intended model is:

- one canonical published line for each durable artefact family;
- one or more isolated candidate worlds that may evaluate revisions against the
  canonical line;
- explicit regression-aware evidence before promotion; and
- explicit rollback or demotion support after publication if later evidence
  invalidates the promotion.

This means `1963` should consume the already represented proposal and experiment
surfaces, not bypass them with prompt-local or benchmark-summary-only
promotion decisions.

### 2. Candidate Worlds Must Become First-Class Represented Objects

Von should stop treating the phrase "benchmark world" as a loose idea spanning
proposal records, theory sandboxes, experiment runs, and benchmark outputs.

Every isolated candidate world should carry, directly or by support-surface
projection:

- `candidate_world_id`
- `candidate_surface`
- `candidate_artefact_ids`
- `baseline_artefact_ids`
- `proposal_id`
- `experiment_spec_id`
- `experiment_run_id`
- `benchmark_world_id`
- `testing_theory_id`
- `namespace`
- `user_id`
- `org_id`
- `memory_manifest_fingerprint`
- `evaluator_axis_versions`
- `promotion_gate_state`
- `rollback_plan`
- `created_at_utc`
- `updated_at_utc`

This is the contract the current code lacks. The constituent objects exist, but
the self-improvement path still does not expose one machine-usable identity for
"the candidate world being evaluated".

### 3. Reuse the Existing Experiment and Testing-Theory Substrate

`JVNAUTOSCI-1963` should not create a second experiment system parallel to
`experiment_run_service.py`.

The intended division of labour is:

- workflow, prompt, or policy candidate records remain the represented artefact
  candidates;
- experiment specs and experiment runs remain the canonical execution and
  verdict container for isolated evaluation;
- testing theories remain the bounded local-write surface for ephemeral claims,
  assumptions, and candidate-only semantic or policy assertions; and
- workflow publication lifecycle and proposal review remain the canonical
  publication and rollback surface.

The missing work is integration and contract-shaping, not a fresh storage model.

### 4. World Types Should Be Explicit

Von should distinguish at least four world types:

| World type | Main role | Durability | Authority |
| --- | --- | --- | --- |
| Canonical world | The currently published line used for real execution | Durable | Canonical authority |
| Candidate world | A represented revision bundle under experiment | Durable manifest, isolated mutable state | Non-canonical |
| Benchmark world | Replay, fixture, or simulated environment used to score the candidate world | Usually durable manifest plus bounded run state | Non-canonical evidence surface |
| Shadow world | Bounded post-sandbox, pre-activation observation against live-like traffic or live traces without canonical replacement | Durable summary plus bounded run state | Non-canonical evidence surface |

Not every candidate must traverse every world type, but the architecture should
make the phase explicit. A promotion gate should know whether it is looking at
sandbox-only evidence, benchmark-world evidence, or shadow-world evidence.

### 5. Promotion Gates Must Consume Evaluator and Memory Evidence

The future promotion gate for self-improvement should not be driven mainly by a
generic benchmark summary, scalar verdict, or free-text justification.

It should consume:

- the multi-axis evaluator contract from `JVNAUTOSCI-1998`;
- grounded helpfulness, calibration, recovery, and long-horizon evaluator
  families from `JVNAUTOSCI-1999`, `JVNAUTOSCI-2000`, and `JVNAUTOSCI-2001`;
- stable memory refs or manifests from `JVNAUTOSCI-1995`; and
- explicit flip or regression accounting between baseline and candidate worlds.

The important engineering consequence is that candidate promotion should be
reasoned over machine-usable axis evidence and stable memory bindings, not only
over one aggregate score or one benchmark prose summary.

### 6. Rollback and Activation Need Explicit Lifecycle Stages

Von already has approval review, publication lifecycle, and rollback surfaces
for workflow candidates. `1963` should extend that into an explicit
candidate-world lifecycle rather than treating rollback as a rare exceptional
afterthought.

The intended high-level lifecycle is:

1. proposal created;
2. candidate validated;
3. isolated experiment running;
4. experiment verdict computed;
5. shadow or bounded pre-activation evaluation, when required;
6. review-ready and approval-gated;
7. canonical promotion or rejection;
8. post-promotion monitoring and possible rollback or demotion.

This does not mean every stage must be fully automatic. It means the stage
boundary should be represented and auditable.

### 7. Workflow-First Now, Broader Candidate Surfaces Next

The current live self-improvement loop is still workflow-first, and that is the
right immediate implementation order because workflow proposal, validation,
review, and rollback surfaces already exist.

But the architecture should not stop there. The same candidate-world contract
should eventually support:

- prompt-family revisions;
- retrieval or routing profile revisions;
- policy-memory or guideline revisions; and
- other represented procedural or policy artefacts.

The rule is the same across all of them: represented candidate artefact, bound
to an isolated candidate world, promoted only after evaluable evidence.

## Evaluation Requirements

The self-improvement-world architecture should be accepted on real or near-real
Von paths, not only by creating isolated data structures.

Minimum acceptance dimensions:

- proposal-to-world identity and traceability
- explicit baseline-vs-candidate comparison
- forbidden side-effect and policy-violation handling
- evaluator-axis evidence preservation across experiment and promotion
- rollback or demotion readiness after adverse evidence
- correct fail-closed behaviour when required memory refs, evaluator outputs, or
  candidate artefact bindings are missing

Acceptance should combine:

- targeted replay or nearest-real-path experiments over the actual candidate
  workflow line; and
- smaller benchmark-world or simulator-backed cases that include fault
  injection, missing evidence, or degraded inputs.

## Short Literature Note

The architecture direction here was checked against recent primary sources:

- [AgentDevel: Reframing Self-Evolving LLM Agents as Release Engineering](https://arxiv.org/abs/2601.04620)
  reinforced treating self-improvement as an auditable release pipeline with a
  single canonical version line and regression-aware promotion evidence rather
  than opaque in-agent recursion.
- [Governed Capability Evolution for Embodied Agents: Safe Upgrade, Compatibility Checking, and Runtime Rollback for Embodied Capability Modules](https://arxiv.org/abs/2604.08059)
  reinforced staged validation, sandbox evaluation, shadow deployment, gated
  activation, and rollback as first-class system stages. That directly supports
  making candidate-world phase explicit in Von.
- [Benchmark Self-Evolving: A Multi-Agent Framework for Dynamic LLM Evaluation](https://arxiv.org/abs/2402.11443)
  reinforced that static benchmark suites understate failure modes and that
  evaluation worlds should be able to evolve or harden over time.
- [OccuBench: Evaluating AI Agents on Real-World Professional Tasks via Language Environment Simulation](https://arxiv.org/abs/2604.10866)
  reinforced the value of environment simulation plus controlled fault injection
  when measuring agent robustness, and therefore supports explicit
  benchmark-world modelling rather than only replaying happy-path traces.

These papers did not overturn Von's design direction. They sharpened the need
for represented candidate-world identity, staged promotion, and evaluator-backed
regression evidence.

## First Implementation Slices

`JVNAUTOSCI-1963` now decomposes into the following concrete tasks:

1. `JVNAUTOSCI-2002`
   Implement represented candidate-world manifests and proposal-to-experiment-run
   linkage for workflow self-improvement candidates.
2. `JVNAUTOSCI-2003`
   Implement evaluator- and memory-backed baseline-vs-candidate comparison for
   self-improvement experiment runs.
3. `JVNAUTOSCI-2004`
   Implement experiment-backed promotion, bounded shadow evaluation, and
   rollback-safe publication gating for workflow self-improvement candidates.
4. `JVNAUTOSCI-2005`
   Extend isolated self-improvement worlds beyond workflows to prompt, policy,
   and retrieval or memory candidate surfaces.

These tasks are intentionally not replacements for `JVNAUTOSCI-1998`,
`JVNAUTOSCI-1999`, `JVNAUTOSCI-2000`, `JVNAUTOSCI-2001`, `JVNAUTOSCI-1995`,
or `JVNAUTOSCI-1996`. They are the concrete continuation of the
self-improvement-world line that those evaluator and memory tasks should now
feed.

## Open Questions Deferred

This note does not settle everything. The following remain intentionally open
for later implementation work:

- how much simulator-backed benchmark-world generation should be centralised
  versus domain-specific;
- when shadow-world evidence is mandatory versus optional before activation;
- how prompt and policy candidate packaging should look once workflow-first
  slices land; and
- which failed candidate worlds should be retained durably for audit versus
  garbage-collected after a bounded period.

Those are follow-on implementation questions, not reasons to leave the
architecture implicit.
