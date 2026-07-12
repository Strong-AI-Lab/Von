# Agentic Architecture Status - 23 April 2026

> **Document status: Frozen architecture and task-ordering snapshot.** This
> records evidence and conclusions from 23 April 2026. It is not current
> programme status. Revalidate code, Vontology, telemetry, and live Jira before
> using any “current”, “remaining”, or “next” claim.

- **Kind:** Status snapshot
- **Lifecycle:** Frozen
- **Authority:** Evidence only
- **State as of:** 2026-04-23

## Purpose

This note records the current engineering status of the Von codebase against the
design intent described in `docs/engineering/Von_for_AgenticAI.md`, using a
bounded static review of the live code on 23 April 2026. It was first written
earlier the same day, refreshed after the landings through
`JVNAUTOSCI-1994` and `JVNAUTOSCI-1993`, then refreshed again as the
implemented output of `JVNAUTOSCI-1989`, and then refreshed again after
`JVNAUTOSCI-1962` split the enduring-memory line into a concrete architecture
note plus first implementation slices, and then refreshed again after
`JVNAUTOSCI-1964` did the same for the evaluator line, and then refreshed again
after `JVNAUTOSCI-1963` did the same for the isolated self-improvement and
benchmark-world line, and then refreshed again after `JVNAUTOSCI-1998` landed
the first concrete multi-axis evaluator contract slice.

It is not a replacement for `Von_for_AgenticAI.md`. That document remains the
long-horizon design note. This document is the current engineering-status and
task-ordering companion that answers a narrower question:

- where the live code still falls materially short of the intended AGI-shaping
  architecture;
- which remaining gaps are the sharpest next engineering priorities; and
- which Jira tasks now carry those gaps in a cold-start-useful form.

## Current Headline

The sharpest remaining problems are no longer the older write-policy,
buttonify, turn-contract prose, workflow-discovery fallback, or broad
tool-metadata seams. Those were materially reduced first by the April 21
landings through `JVNAUTOSCI-1972`, then again by the 23 April landings of
`JVNAUTOSCI-1974`, `JVNAUTOSCI-1975`, `JVNAUTOSCI-1985`,
`JVNAUTOSCI-1973`, `JVNAUTOSCI-1986`, `JVNAUTOSCI-1988`,
`JVNAUTOSCI-1987`, `JVNAUTOSCI-1992`, `JVNAUTOSCI-1994`, and
`JVNAUTOSCI-1993`.

After the `JVNAUTOSCI-1962`, `JVNAUTOSCI-1964`, and `JVNAUTOSCI-1963`
architecture passes, the
current shortfall is now concentrated in four areas:

1. The evaluator line now has concrete child tasks rather than only the
   umbrella `JVNAUTOSCI-1964`: `JVNAUTOSCI-1998`, `JVNAUTOSCI-1999`,
   `JVNAUTOSCI-2000`, and `JVNAUTOSCI-2001` now carry the multi-axis evaluator
   contract, grounded helpfulness critic, calibration and abstention evaluator,
   and long-horizon evaluator support. `1998` and `1999` are now landed, so
   the remaining work on this line is the calibration/recovery slice plus the
   long-horizon support slice.
2. The self-improvement-world line now also has concrete child tasks rather
   than only the umbrella `JVNAUTOSCI-1963`: `JVNAUTOSCI-2002`,
   `JVNAUTOSCI-2003`, `JVNAUTOSCI-2004`, and `JVNAUTOSCI-2005` now carry the
   candidate-world manifest, baseline-vs-candidate comparison, experiment-backed
   promotion and rollback gating, and later prompt or policy candidate-world
   expansion.
3. The enduring-memory line still has its own concrete child tasks:
   `JVNAUTOSCI-1995`, `JVNAUTOSCI-1996`, and `JVNAUTOSCI-1997` carry the
   canonical manifest/query surface, promotion/revision workflow, and
   long-horizon evaluation harness that `1962` identified.
4. Monolith pressure remains real, especially in the orchestrator and
   catalogue, but it is now secondary to the broader memory/evaluator/
   self-improvement substrate work. The files are still too large, but they are
   materially smaller than the stale earlier same-day snapshot.

## Current Code Reality

As measured on the live branch during the `JVNAUTOSCI-1989` review:

- `src/backend/integrations/internal_mcp/orchestrator.py` is `35095` lines
- `src/backend/integrations/internal_mcp/catalogue.py` is `27480` lines
- `src/backend/server/routes/von_routes.py` is `13351` lines
- `src/backend/services/turn_execution_record_service.py` is `6916` lines
- `src/backend/server/utils_flask.py` is `3036` lines

The earlier same-day status snapshot was therefore materially stale on file
sizes alone. Large files are not the whole problem, but they remain the
strongest pressure surface for hidden policy drift and inline patching.

## Live Code Anchors

The remaining umbrellas should now be interpreted against the following live
surfaces rather than against older pre-`1988` or pre-`1993` wording:

- Memory substrate and turn-context integration:
  [conversation_turn_memory_context_service.py](../../src/backend/services/conversation_turn_memory_context_service.py),
  [context_bundle_service.py](../../src/backend/services/context_bundle_service.py),
  [context_bundle_service.py](../../src/backend/services/context_bundle_service.py),
  [orchestrator.py](../../src/backend/integrations/internal_mcp/orchestrator.py),
  [orchestrator.py](../../src/backend/integrations/internal_mcp/orchestrator.py),
  [von_routes.py](../../src/backend/server/routes/von_routes.py)

- Episode self-improvement and proposal/promotion loop:
  [episode_evaluation_workflow.py](../../src/backend/workflows/durable/episode_evaluation_workflow.py),
  [episode_self_improvement_service.py](../../src/backend/services/episode_self_improvement_service.py),
  [episode_self_improvement_service.py](../../src/backend/services/episode_self_improvement_service.py),
  [episode_self_improvement_service.py](../../src/backend/services/episode_self_improvement_service.py),
  [episode_self_improvement_workflow.py](../../src/backend/workflows/durable/episode_self_improvement_workflow.py),
  [workflow_studio_service.py](../../src/backend/workflows/workflow_studio_service.py)

- Experiment worlds, testing theories, and publication rollback support:
  [experiment_run_service.py](../../src/backend/services/experiment_run_service.py),
  [experiment_run_service.py](../../src/backend/services/experiment_run_service.py),
  [experiment_run_service.py](../../src/backend/services/experiment_run_service.py),
  [testing_theory_service.py](../../src/backend/services/testing_theory_service.py),
  [testing_theory_service.py](../../src/backend/services/testing_theory_service.py),
  [testing_theory_service.py](../../src/backend/services/testing_theory_service.py),
  [workflow_studio_service.py](../../src/backend/workflows/workflow_studio_service.py),
  [workflow_studio_service.py](../../src/backend/workflows/workflow_studio_service.py)

- Current critic/evaluator surfaces:
  [episode_evaluator_contract_service.py](../../src/backend/services/episode_evaluator_contract_service.py),
  [turn_execution_record_service.py](../../src/backend/services/turn_execution_record_service.py),
  [turn_execution_record_service.py](../../src/backend/services/turn_execution_record_service.py),
  [episode_critic_evidence_service.py](../../src/backend/services/episode_critic_evidence_service.py),
  [episode_evaluation_workflow.py](../../src/backend/workflows/durable/episode_evaluation_workflow.py),
  [episode_critique_memory_service.py](../../src/backend/services/episode_critique_memory_service.py),
  [episode_critique_memory_service.py](../../src/backend/services/episode_critique_memory_service.py),
  [episode_critique_benchmark_service.py](../../src/backend/services/episode_critique_benchmark_service.py),
  [episode_evaluation_workflow_vontology_service.py](../../src/backend/services/episode_evaluation_workflow_vontology_service.py)

These anchors matter because the remaining gaps are no longer mostly "missing
live-path repair" tasks. They are broader substrate-shaping tasks that should
start from the current represented surfaces rather than restating older absence
claims.

## Targeted Literature Check

The `JVNAUTOSCI-1989`, `JVNAUTOSCI-1962`, and `JVNAUTOSCI-1964` passes
together included a short targeted literature pass on memory, evaluator design,
and safe self-improvement.

- [Memory for Autonomous LLM Agents: Mechanisms, Evaluation, and Emerging Frontiers](https://arxiv.org/abs/2603.07670)
  argues that agent memory should be treated as a write-manage-read loop over
  temporal scope, representational substrate, and control policy. That
  reinforces `1962` as an architecture task, not just a retrieval feature task.
- [E-mem: Multi-agent based Episodic Context Reconstruction for LLM Agent Memory](https://arxiv.org/abs/2601.21714)
  reinforces keeping some episodic context reconstructable rather than reducing
  everything to flattened preprocessed memory.
- [MemoryArena: Benchmarking Agent Memory in Interdependent Multi-Session Agentic Tasks](https://arxiv.org/abs/2602.16313)
  reinforces that memory and action should be evaluated together on
  interdependent multi-session tasks rather than only through static recall.
- [LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813)
  reinforces that update handling, temporal reasoning, multi-session grounding,
  and abstention belong in the enduring-memory acceptance surface.
- [ProcMEM: Learning Reusable Procedural Memory from Experience via Non-Parametric PPO for LLM Agents](https://arxiv.org/abs/2602.01869)
  supports promoting episodic traces into reusable procedural artefacts with
  explicit executability and gating, which maps directly onto Von's
  memory-promotion direction under `1962`.
- [Agentic Confidence Calibration](https://arxiv.org/abs/2601.15778) and
  [Four-Axis Decision Alignment for Long-Horizon Enterprise AI Agents](https://arxiv.org/abs/2604.19457)
  both reinforce that aggregate success is too weak a target. Richer evaluator
  axes for calibration, abstention, reasoning quality, and groundedness are
  needed before broader self-improvement rollouts. That is the main reason
  the evaluator line was decomposed before `1963`.
- [Groundedness in Retrieval-augmented Long-form Generation: An Empirical Study](https://arxiv.org/abs/2404.07060)
  reinforced that answer correctness is too weak if generated claims are not
  actually grounded in retrieved evidence.
- [Calibrating the Confidence of Large Language Models by Eliciting Fidelity](https://arxiv.org/abs/2404.02655)
  and [BAS: A Decision-Theoretic Approach to Evaluating Large Language Model Confidence](https://arxiv.org/abs/2604.03216)
  reinforced that calibration and abstention need distinct evaluator treatment
  rather than one generic confidence field.
- [Process Reward Models for LLM Agents: Practical Framework and Directions](https://arxiv.org/abs/2502.10325)
  reinforced that later self-improvement work benefits from machine-usable
  process signals rather than only outcome labels.
- [OdysseyBench: Evaluating LLM Agents on Long-Horizon Complex Office Application Workflows](https://arxiv.org/abs/2508.09124)
  reinforced that long-horizon quality has to be evaluated over history- and
  dependency-sensitive tasks, not only atomic completions.
- [AgentDevel: Reframing Self-Evolving LLM Agents as Release Engineering](https://arxiv.org/abs/2601.04620)
  is directionally aligned with the current Von state after `1987`/`1994`/`1993`:
  keep a single canonical version line, require regression-aware promotion
  evidence, and treat self-improvement as an auditable release pipeline rather
  than an opaque in-agent recursion.
- [Governed Capability Evolution for Embodied Agents: Safe Upgrade, Compatibility Checking, and Runtime Rollback for Embodied Capability Modules](https://arxiv.org/abs/2604.08059)
  reinforced staged validation, sandbox evaluation, shadow deployment, gated
  activation, and rollback as first-class system phases rather than ad-hoc
  recovery behaviour.
- [Benchmark Self-Evolving: A Multi-Agent Framework for Dynamic LLM Evaluation](https://arxiv.org/abs/2402.11443)
  reinforced that the right benchmark-world line should be able to evolve or
  harden over time rather than relying only on one static suite.
- [OccuBench: Evaluating AI Agents on Real-World Professional Tasks via Language Environment Simulation](https://arxiv.org/abs/2604.10866)
  reinforced the value of simulator-backed evaluation worlds plus controlled
  fault injection when measuring robustness before canonical promotion.

This literature did not overturn Von's design direction. It did sharpen the task
ordering.

## Priority Ordering

### Current Tranche Status

The following architecture tasks from the 21-23 April tranche are now landed:

1. `JVNAUTOSCI-1974`
   Missing-tool-call retry no longer synthesises tool payloads from prompt text
   on the live path.

2. `JVNAUTOSCI-1975`
   Dispatch surface-family inference in turn-contract preflight is now driven by
   represented tool metadata rather than the previous hard-coded family table.

3. `JVNAUTOSCI-1985`
   Registry exposure and turn-record write/evidence semantics now consume the
   unified tool-metadata path instead of separate Python-owned slabs.

4. `JVNAUTOSCI-1973`
   Workflow discovery no longer executes English keyword/name fallback on the
   live routing path.

5. `JVNAUTOSCI-1986`
   Rumination relation candidate ranking and auto-apply semantics now resolve
   from the linked knowledge-acquisition profile instead of Python keyword/class
   heuristics.

6. `JVNAUTOSCI-1988`
   Main-turn execution now consumes represented turn-memory substrate directly.

7. `JVNAUTOSCI-1987`
   Episode evaluation now launches represented workflow-improvement proposal and
   promotion workflows instead of relying on Python fallback suggestion
   synthesis.

8. `JVNAUTOSCI-1992`
   User-only namespaces now stay on the represented self-improvement path
   instead of failing closed behind an unnecessary org requirement.

9. `JVNAUTOSCI-1994`
   Workflow-authoring proposal persistence is now proposal-addressable rather
   than singleton-per-workflow.

10. `JVNAUTOSCI-1993`
    The remaining self-improvement launch and benchmark-policy slab now resolves
    from a represented Vontology-backed profile.

11. `JVNAUTOSCI-1989`
    This repeat review is now complete: the dated status note is refreshed, the
    older sequencing companion note is marked as historical for ordering
    purposes, the main monolith sizes were re-measured, and the remaining
    umbrella tasks were updated with cold-start comments against the live code.

12. `JVNAUTOSCI-1962`
    The enduring-memory line now has a dated architecture note at
    `docs/engineering/jvnautosci_1962_enduring_memory_architecture_2026-04-23.md`,
    plus concrete linked child tasks for the first implementation slices:
    `JVNAUTOSCI-1995`, `JVNAUTOSCI-1996`, and `JVNAUTOSCI-1997`.

13. `JVNAUTOSCI-1964`
    The evaluator line now has a dated architecture note at
    `docs/engineering/jvnautosci_1964_evaluator_architecture_2026-04-23.md`,
    plus concrete linked child tasks for the first implementation slices:
    `JVNAUTOSCI-1998`, `JVNAUTOSCI-1999`, `JVNAUTOSCI-2000`, and
    `JVNAUTOSCI-2001`.

14. `JVNAUTOSCI-1963`
    The isolated self-improvement-world line now has a dated architecture note
    at
    `docs/engineering/jvnautosci_1963_self_improvement_worlds_architecture_2026-04-23.md`,
    plus concrete linked child tasks for the first implementation slices:
    `JVNAUTOSCI-2002`, `JVNAUTOSCI-2003`, `JVNAUTOSCI-2004`, and
    `JVNAUTOSCI-2005`.

15. `JVNAUTOSCI-1998`
    Critique memory, fallback episode assessment, and benchmark reporting now
    share a canonical multi-axis evaluator contract across execution
    correctness, grounded helpfulness, calibration and abstention, recovery
    quality, and long-horizon task-state integrity, with legacy
    verdict/confidence retained only as roll-up compatibility state.

16. `JVNAUTOSCI-1999`
    The evaluator line now has a represented grounded-helpfulness critic
    workflow and prompt, plus shared evidence-bundle support for answer
    artefacts, answer-support consistency evidence, and response-context
    lineage. The live episode-evaluation path now persists grounded
    helpfulness as a first-class evaluator axis rather than inferring it only
    from the older format-over-content diagnostic.

### P1 Next Tranche

The next substantive sequence should now be:

1. `JVNAUTOSCI-2000`
   Calibration, abstention, and recovery-quality evaluator workflow.

2. `JVNAUTOSCI-1995`
   Canonical enduring-memory manifest and query surface.

3. `JVNAUTOSCI-2001`
   Long-horizon task-state reconstruction and memory-conditioned evaluator
   support.

4. `JVNAUTOSCI-2002`
   Represented candidate-world manifests and proposal-to-experiment-run
   linkage for workflow self-improvement.

5. `JVNAUTOSCI-2003`
   Evaluator- and memory-backed baseline-vs-candidate comparison for
   self-improvement experiment runs.

6. `JVNAUTOSCI-2004`
   Experiment-backed promotion, bounded shadow evaluation, and rollback-safe
   publication gating for workflow candidates.

7. `JVNAUTOSCI-1996`
   Represented memory-promotion and revision workflow.

8. `JVNAUTOSCI-1997`
    Long-horizon enduring-memory evaluation and task-state reconstruction
    harness.

9. `JVNAUTOSCI-2005`
    Prompt, policy, and retrieval or memory candidate-world expansion beyond
    the workflow-first line.

Why this is now the right order:

- `1964` is now done as architecture and decomposition work, so the next moves
  should be concrete evaluator slices rather than another umbrella pass.
- `1998` and `1999` are now landed, so `2000` and `2001` can build on a live
  stored axis contract plus a represented grounded-helpfulness critic rather
  than inventing local evaluator result shapes.
- `2000` now comes before the later `1963` child tasks because
  self-improvement and
  promotion gating become more valuable once they can reason over richer
  evaluator outputs for grounded helpfulness, calibration, abstention, and
  recovery quality.
- `1995` comes before the long-horizon evaluator slice because `2001` should
  score memory-conditioned continuity against stable memory refs rather than
  bespoke point lookups.
- `2002` now comes before the later self-improvement-world slices because the
  live code still lacks one explicit candidate-world identity linking proposal,
  experiment run, benchmark world, and baseline.
- `2003` and `2004` then build on that manifest plus the richer evaluator and
  memory evidence from `1998`-`2001` and `1995`.
- `1996` and `1997` remain cold-startable enduring-memory slices rather than
  abstract future work, but broadening candidate worlds beyond workflows is now
  lower priority than landing the workflow-first experiment and evaluation line.

### P2 Longer-Horizon Platform Work

These remain important, but they are not the sharpest next moves on the current
code:

- `JVNAUTOSCI-1961`
  Minimal-imposition benchmark and deployment acceptance model.

- `JVNAUTOSCI-1965`
  Represented multi-agent coordination, delegation, and shared-memory
  substrates.

- `JVNAUTOSCI-1966`
  Multimodal evidence fusion and provenance architecture.

- `JVNAUTOSCI-1967`
  Capability-suite and secure partner-deployment architecture.

- `JVNAUTOSCI-1968`
  Represented normative-governance, uncertainty, and escalation architecture.

- `JVNAUTOSCI-1120`
  Frontend monolith deconstruction.

Why this is P2:

- these are still real gaps, but they are not the sharpest blockers to the
  current memory/evaluator/self-improvement architecture line;
- `1961` becomes more valuable once richer evaluator outputs exist to serve as
  benchmark evidence rather than only post hoc logs; and
- monolith reduction remains worthwhile, but it is no longer the clearest
  AGI-shaping next move relative to the broader memory/evaluator substrate work.

## Existing Tasks Not Duplicated

Some older tasks remain useful as umbrellas or adjacent work, but were not
reopened or duplicated here:

- `JVNAUTOSCI-1913` remains the main anti-drift / monolith-pressure umbrella.
- `JVNAUTOSCI-1960` remains the broader minimal-imposition agentic programme
  epic.
- `JVNAUTOSCI-1969` remains a worthwhile MCP contract/diagnostics audit, but it
  is not the same as the current remaining memory/evaluator/self-improvement
  gaps.
- `JVNAUTOSCI-768` and `JVNAUTOSCI-770` still point in the represented tool
  metadata direction, but their wording remains materially stale and they should
  not be treated as the current implementation guide without reinterpretation.

## Ongoing Review Cadence

`JVNAUTOSCI-1989` has now executed the first bounded repeat review after the
same-day architecture tranche. The review cadence itself should remain in force.

After completion of any substantive P1 slice under `1998`, `1999`, `2000`,
`2001`, `1963`, `1995`, `1996`, or `1997`:

1. rerun a bounded architecture scan against the then-current code;
2. re-measure the main monolith sizes and re-check the strongest remaining live
   Python authority seams;
3. update this note and any now-stale sequencing companion notes;
4. refresh Jira comments or wording where the cold-start framing has gone stale;
   and
5. only then decide the next highest-priority tranche.

The key rule is simple:

> do not keep steering work from old architecture notes once the live code has
> moved and the highest-value remaining gaps have changed.

## Doc Status

This repeat review plus the `1962` and `1964` architecture passes produced five
documentation conclusions:

- this dated status note needed a real refresh and now reflects the live
  post-`1993`/`1994`/`1989`/`1962`/`1964` state;
- `docs/engineering/jvnautosci_1962_enduring_memory_architecture_2026-04-23.md`
  now carries the concrete enduring-memory architecture and first child-task
  decomposition;
- `docs/engineering/jvnautosci_1964_evaluator_architecture_2026-04-23.md`
  now carries the concrete evaluator architecture and first child-task
  decomposition;
- `docs/engineering/recent_architecture_progress_2026-04-20.md` still remains
  useful as a historical anti-drift note through the April 21 tranche, but its
  sequencing advice should no longer be used as the current programme-wide next
  step list; and
- `docs/engineering/Von_for_AgenticAI.md` did not require direct editing in this
  review because its long-horizon design claims remain directionally correct.
  The stale surfaces were the dated status and sequencing notes, not the
  underlying design intent.
