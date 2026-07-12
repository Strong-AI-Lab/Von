# JVNAUTOSCI-1964 Evaluator Architecture - 23 April 2026

> **Document status: Long-horizon design with a dated implementation snapshot.**
> The design may remain useful. Claims using “current”, “already”, “still”,
> source anchors, and task priority describe the April 2026 evidence base and
> require live revalidation. This is not release-status authority.

## Purpose

This note records the concrete evaluator architecture for Von after the
postcondition-critic, critique-memory, self-improvement, and enduring-memory
landings through `JVNAUTOSCI-1987`, `JVNAUTOSCI-1993`, `JVNAUTOSCI-1994`, and
`JVNAUTOSCI-1962`.

`JVNAUTOSCI-1964` is not a task to "add more critics" in the abstract. The live
code already has a real evaluator substrate:

- deterministic postcondition and completion-gate support in
  `src/backend/services/turn_execution_record_service.py`;
- bounded episode evidence bundles in
  `src/backend/services/episode_critic_evidence_service.py`;
- prompt-backed episode evaluation workflow support in
  `src/backend/workflows/durable/episode_evaluation_workflow.py`;
- durable critique-memory persistence in
  `src/backend/services/episode_critique_memory_service.py`; and
- replay or sampled benchmark reporting in
  `src/backend/services/episode_critique_benchmark_service.py`.

The actual gap is narrower and more important: evaluator coverage is still too
scalar and too completion-centric for grounded helpfulness, calibration,
abstention, recovery quality, and long-horizon task-state integrity to serve as
first-class machine-usable evidence.

This note therefore does two things:

1. states the evaluator architecture that now matches the live code and the
   design intent in `docs/engineering/Von_for_AgenticAI.md`; and
2. turns that architecture into the first concrete implementation slices rather
   than leaving `1964` as a broad umbrella.

## Current Live Substrate

The current Von code already contains the following evaluator-capable surfaces:

- Postcondition checks, completion-gate derivation, execution correctness, and
  critic summary construction in
  `src/backend/services/turn_execution_record_service.py:1300`, `:5506`,
  `:5658`, and `:6399`.
- Episode evidence bundle construction, reconstructable turn-record recovery,
  bounded receipts, and format-over-content diagnostics in
  `src/backend/services/episode_critic_evidence_service.py:1395`.
- Prompt-backed episode evaluation workflow execution and critique-memory
  persistence in
  `src/backend/workflows/durable/episode_evaluation_workflow.py:107`,
  `:228`, and `:293`.
- Durable verdict/confidence persistence and critique-memory projection in
  `src/backend/services/episode_critique_memory_service.py:716` and `:1723`.
- Sampled meta-audit and recurrence reporting in
  `src/backend/services/episode_critique_benchmark_service.py:1393`.
- Canonical workflow and prompt-family bootstrap in
  `src/backend/services/episode_evaluation_workflow_vontology_service.py:79`.

The problem is not absence. The problem is evaluator narrowness:

- the core persistent shape is still mostly `verdict`, `confidence`,
  `unresolved_check_count`, and remediation links;
- the benchmark layer still reasons mostly over verdict proxies, recurrence,
  remediation presence, and sampled rebuilds rather than explicit evaluator
  axes;
- the workflow bootstrap currently seeds one main episode-critic prompt family
  rather than distinct groundedness, calibration, recovery, and long-horizon
  evaluator families; and
- the current evidence bundle only partially exposes answer-grounding,
  abstention, and long-horizon state-reconstruction evidence as first-class
  reusable receipts.

## Architecture Decision

### 1. Evaluation Must Become Multi-Axis

Von should stop treating episode evaluation as one scalar verdict with a small
set of follow-up hints. The durable evaluator contract should carry multiple
axes that are independently measurable and independently failable.

The minimum first-class axes should be:

| Axis | Main question | Primary live evidence | Why it matters |
| --- | --- | --- | --- |
| Execution correctness | Did the required effects and declared completion state actually match reality? | Postcondition checks, completion gate, tool or workflow receipts | Already partially live; remains the foundation |
| Grounded helpfulness | Did the answer or action stay faithful to retrieved, observed, or memory-backed evidence while still being useful? | Shared turn context, search or tool receipts, answer artefacts, evidence bundle | Prevents "correct sounding" but ungrounded or weakly supported output |
| Calibration and abstention | Did the agent commit, abstain, escalate, or ask follow-up in a way that matched uncertainty and evidence strength? | Completion gate, answer text, evidence gaps, selected workflow or route, explicit abstention path | Needed for trustworthy non-bluffing behaviour |
| Recovery quality | When evidence or execution failed, did the system choose a bounded, appropriate recovery path? | Critique memory, remediation routing, later turn outcomes, recovery actions | Needed for safe self-improvement and practical deployability |
| Long-horizon task-state integrity | Did the agent reconstruct the right task state over time, use memory appropriately, and preserve continuity across interruptions or updates? | Durable memory refs, dossiers, workflow episodes, critique memory, later replay evidence | Needed for long-horizon collaboration rather than turn-local correctness |

The engineering consequence is simple: a later promotion, benchmark, or
roll-back gate should not have to infer all of this from one generic verdict.

### 2. Every Axis Needs a Common Machine-Usable Contract

Across all axes, persisted evaluator outputs should carry a common shape with
axis-specific evidence inside it.

Each axis result should carry, directly or by support-surface projection:

- `axis_id`
- `axis_version`
- `status`
- `score` or `utility_estimate` when the axis supports one
- `confidence`
- `reason_codes`
- `summary`
- `evidence_receipt_ids`
- `source_memory_ids` or other durable artefact refs
- `subject_workflow_ids`, `subject_tool_names`, and `subject_concept_ids`
- `counterfactual_recommended_action` when the axis is about abstention,
  escalation, or recovery
- `measured_at_utc`

This contract should sit above individual workflows or prompt variants.
`JVNAUTOSCI-1964` is therefore not mainly a prompt-writing task. It is a
support-surface and represented-workflow-family task.

### 3. Evaluator Families Should Be Workflow-First and Composable

Von should not respond to the missing evaluator coverage by turning one larger
Python coordinator into the new policy home.

The intended structure is:

1. one shared bounded evidence bundle and locator contract;
2. axis-specific critic or evaluator workflows that consume the same evidence
   substrate and shared turn context;
3. durable persistence of axis results into critique memory and benchmark
   projections; and
4. promotion, rollback, or remediation consumers that operate over those
   machine-usable axis results.

That means the current postcondition critic should remain one evaluator family,
not the universal critic.

### 4. The Evidence Bundle Must Grow Stable Answer and Memory Receipts

The shared evidence bundle should become the canonical input substrate for the
new evaluator families. To do that, it needs stronger first-class receipts for:

- final answer or user-visible response artefacts;
- answer-support alignment evidence, not only tool or search receipts;
- durable memory refs and task-state refs once `JVNAUTOSCI-1995` lands;
- explicit abstention, refusal, escalation, and follow-up prompts or outputs;
- recovery-route evidence and later-turn continuation evidence; and
- context-lineage or reconstruction receipts showing what the evaluator and the
  actor actually saw.

This is support-surface work, not a justification for new answer-repair policy
in Python.

### 5. Critique Memory Should Persist Evaluator State, Not Only Verdict State

`episode_critique_memory` should remain the durable episodic evaluator artefact,
but its persistent contract should expand from scalar verdict state to
multi-axis evaluator state.

The durable object should preserve:

- the top-level episode summary and actionable follow-up state;
- axis-level results;
- durable evidence and memory refs;
- revision lineage when a later evaluator run supersedes an earlier one; and
- links to remediation, proposal, and promotion decisions driven by those
  results.

This is the missing bridge between current critic runs and later self-improving
or long-horizon memory work.

### 6. Benchmark and Audit Surfaces Should Score by Axis

The current benchmark surface is useful, but it still acts mostly as a verdict
and recurrence audit layer.

The intended acceptance surface should score and sample by evaluator axis:

- grounded helpfulness and evidence-answer consistency;
- calibration and abstention quality;
- recovery quality;
- long-horizon task-state reconstruction and continuity; and
- interaction with execution correctness rather than replacement of it.

Sampled rebuilds should remain, but the reporting layer should stop treating
aggregate success or a single verdict as the main unit of truth.

## Evaluation Requirements

The evaluator architecture should be accepted on real or near-real Von paths,
not only on synthetic unit-shaped examples.

Minimum acceptance dimensions:

- answer-evidence consistency on direct-response and workflow-answer paths
- correct abstention or follow-up under incomplete evidence
- recovery quality after grounded failure or incomplete execution
- long-horizon task-state reconstruction after interruption or memory update
- explicit disagreement detection between actor claims and evaluator evidence
- durable axis persistence suitable for later proposal or promotion decisions

Acceptance should combine:

- targeted replay or nearest-real-path evaluator tests on the live workflow
  family; and
- smaller benchmark or sampled-audit sets that score the new axes directly.

## Short Literature Note

The architecture direction here was checked against recent primary sources:

- [Groundedness in Retrieval-augmented Long-form Generation: An Empirical Study](https://arxiv.org/abs/2404.07060)
  reinforced that answer correctness alone is too weak when generated sentences
  are not actually grounded in retrieved evidence. This supports a first-class
  grounded helpfulness axis rather than relying on aggregate answer success.
- [Calibrating the Confidence of Large Language Models by Eliciting Fidelity](https://arxiv.org/abs/2404.02655)
  reinforced that confidence should be decomposed and measured, not inferred
  from one generic score.
- [BAS: A Decision-Theoretic Approach to Evaluating Large Language Model Confidence](https://arxiv.org/abs/2604.03216)
  reinforced that abstention-aware utility should be treated as a distinct
  evaluation problem, especially when overconfident errors are costlier than
  underconfidence.
- [Agentic Confidence Calibration](https://arxiv.org/abs/2601.15778)
  reinforced that calibration for agents should be process-centric and
  trajectory-aware rather than borrowed unchanged from single-turn settings.
- [Process Reward Models for LLM Agents: Practical Framework and Directions](https://arxiv.org/abs/2502.10325)
  reinforced that agent improvement needs machine-usable process signals, not
  only outcome labels.
- [OdysseyBench: Evaluating LLM Agents on Long-Horizon Complex Office Application Workflows](https://arxiv.org/abs/2508.09124)
  reinforced that long-horizon evaluation must include history-sensitive,
  multi-step tasks rather than only atomic end states.

These papers did not overturn Von's design direction. They sharpened the need
for explicit evaluator axes, process-level evidence, and long-horizon
acceptance.

## First Implementation Slices

`JVNAUTOSCI-1964` now decomposes into the following concrete tasks:

1. `JVNAUTOSCI-1998`
   Implement a canonical multi-axis episode evaluator contract and critique
   memory projection expansion across execution correctness, grounded
   helpfulness, calibration, recovery, and long-horizon quality.
2. `JVNAUTOSCI-1999`
   Implement an authority-backed grounded helpfulness and evidence-answer
   consistency critic workflow using the shared evidence bundle and durable
   answer receipts.
3. `JVNAUTOSCI-2000`
   Implement an authority-backed calibration, abstention, and recovery-quality
   evaluator workflow and benchmark surface.
4. `JVNAUTOSCI-2001`
   Implement long-horizon task-state reconstruction and memory-conditioned
   evaluator support so later memory and benchmark work can score continuity
   and interruption handling on stable references.

These tasks are intentionally not replacements for `JVNAUTOSCI-1963`,
`JVNAUTOSCI-1995`, `JVNAUTOSCI-1996`, or `JVNAUTOSCI-1997`. They are the
concrete continuation of the evaluator line that those broader memory and
self-improvement tasks should now consume.

## Open Questions Deferred

This note does not settle everything. The following remain intentionally open
for later implementation work:

- whether some evaluator axes should support both scalar utility and categorical
  pass/fail state, rather than one or the other;
- how much actor-side confidence or abstention should be made explicitly
  user-visible versus remaining internal evaluator evidence;
- whether some long-horizon evaluator evidence should be persisted only as
  sampled benchmark artefacts rather than every-day critique memory; and
- how quickly current lightweight diagnostics inside
  `episode_critic_evidence_service.py` should be replaced by represented
  workflow-family outputs.

Those are follow-on implementation questions, not reasons to keep the
evaluator architecture implicit.
