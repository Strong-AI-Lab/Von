# Agentic Architecture Status - 23 April 2026

## Purpose

This note records the current engineering status of the Von codebase against the
design intent described in `docs/engineering/Von_for_AgenticAI.md`, using a
bounded static review of the live code on 23 April 2026. It was first written
earlier the same day, refreshed after the landings through
`JVNAUTOSCI-1994` and `JVNAUTOSCI-1993`, then refreshed again as the
implemented output of `JVNAUTOSCI-1989`, and then refreshed again after
`JVNAUTOSCI-1962` split the enduring-memory line into a concrete architecture
note plus first implementation slices.

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

After the `JVNAUTOSCI-1962` architecture pass, the current shortfall is now
concentrated in four areas:

1. `JVNAUTOSCI-1964` is now the sharpest next umbrella. Von has a real
   authority-backed critic substrate, but evaluator coverage is still too narrow
   for grounded helpfulness, calibration, uncertainty, and long-horizon success
   to act as first-class machine-usable evidence.
2. `JVNAUTOSCI-1963` remains essential, but its gap has changed shape. The
   self-improvement loop is no longer absent; the remaining need is isolated
   experiment worlds, baseline-vs-candidate comparison, rollback, and promotion
   gating that build on richer evaluator outputs rather than bypass them.
3. The enduring-memory line now has concrete child tasks rather than only an
   umbrella: `JVNAUTOSCI-1995`, `JVNAUTOSCI-1996`, and `JVNAUTOSCI-1997`
   carry the canonical manifest/query surface, promotion/revision workflow, and
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
  [conversation_turn_memory_context_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/conversation_turn_memory_context_service.py:547),
  [context_bundle_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/context_bundle_service.py:630),
  [context_bundle_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/context_bundle_service.py:1296),
  [orchestrator.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/integrations/internal_mcp/orchestrator.py:28847),
  [orchestrator.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/integrations/internal_mcp/orchestrator.py:29840),
  [von_routes.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/server/routes/von_routes.py:7717)

- Episode self-improvement and proposal/promotion loop:
  [episode_evaluation_workflow.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/workflows/durable/episode_evaluation_workflow.py:318),
  [episode_self_improvement_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/episode_self_improvement_service.py:310),
  [episode_self_improvement_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/episode_self_improvement_service.py:638),
  [episode_self_improvement_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/episode_self_improvement_service.py:706),
  [episode_self_improvement_workflow.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/workflows/durable/episode_self_improvement_workflow.py:239),
  [workflow_studio_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/workflows/workflow_studio_service.py:869)

- Current critic/evaluator surfaces:
  [turn_execution_record_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/turn_execution_record_service.py:1300),
  [turn_execution_record_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/turn_execution_record_service.py:6399),
  [episode_evaluation_workflow.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/workflows/durable/episode_evaluation_workflow.py:228),
  [episode_critique_memory_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/episode_critique_memory_service.py:716),
  [episode_critique_memory_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/episode_critique_memory_service.py:1723),
  [episode_evaluation_workflow_vontology_service.py](/C:/Users/mwit860/Programming/Strong-AI-Lab/Von/src/backend/services/episode_evaluation_workflow_vontology_service.py:79)

These anchors matter because the remaining gaps are no longer mostly "missing
live-path repair" tasks. They are broader substrate-shaping tasks that should
start from the current represented surfaces rather than restating older absence
claims.

## Targeted Literature Check

The `JVNAUTOSCI-1989` and `JVNAUTOSCI-1962` passes together included a short
targeted literature pass on memory, evaluator design, and safe
self-improvement.

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
  `1964` now edges ahead of `1963`.
- [AgentDevel: Reframing Self-Evolving LLM Agents as Release Engineering](https://arxiv.org/abs/2601.04620)
  is directionally aligned with the current Von state after `1987`/`1994`/`1993`:
  keep a single canonical version line, require regression-aware promotion
  evidence, and treat self-improvement as an auditable release pipeline rather
  than an opaque in-agent recursion.

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

### P1 Next Tranche

The next substantive sequence should now be:

1. `JVNAUTOSCI-1964`
   Authority-backed critic/evaluator workflow expansion umbrella.

2. `JVNAUTOSCI-1963`
   Isolated self-improvement loops and benchmark worlds umbrella.

3. `JVNAUTOSCI-1995`
   Canonical enduring-memory manifest and query surface.

4. `JVNAUTOSCI-1996`
   Represented memory-promotion and revision workflow.

5. `JVNAUTOSCI-1997`
   Long-horizon enduring-memory evaluation and task-state reconstruction
   harness.

Why this is now the right order:

- `1962` is now done as architecture and decomposition work, so the next
  sharpest umbrella is `1964`.
- `1964` now comes before `1963` because the self-improvement loop is present
  but still reasons over too narrow an evaluator substrate. Richer machine-usable
  critic outputs for grounded helpfulness, calibration, abstention, and
  long-horizon success now look like the cleanest unlock for both safe promotion
  and later benchmark-world work.
- `1963` still matters, but its starting point is no longer "there is no loop".
  It should now extend the represented loop into isolated experiment worlds,
  baseline-vs-candidate comparison, rollback, and promotion gating while
  consuming the broader evaluator evidence designed under `1964`.
- `1995`/`1996`/`1997` are now cold-startable enduring-memory slices rather
  than abstract future work. They can proceed as the concrete continuation of
  the memory line once capacity returns to it, and `1996`/`1997` in particular
  directly benefit from the richer evaluator substrate targeted by `1964`.

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

After completion of any substantive P1 slice under `1964`, `1963`, `1995`,
`1996`, or `1997`:

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

This repeat review plus the `1962` architecture pass produced four
documentation conclusions:

- this dated status note needed a real refresh and now reflects the live
  post-`1993`/`1994`/`1989`/`1962` state;
- `docs/engineering/jvnautosci_1962_enduring_memory_architecture_2026-04-23.md`
  now carries the concrete enduring-memory architecture and first child-task
  decomposition;
- `docs/engineering/recent_architecture_progress_2026-04-20.md` still remains
  useful as a historical anti-drift note through the April 21 tranche, but its
  sequencing advice should no longer be used as the current programme-wide next
  step list; and
- `docs/engineering/Von_for_AgenticAI.md` did not require direct editing in this
  review because its long-horizon design claims remain directionally correct.
  The stale surfaces were the dated status and sequencing notes, not the
  underlying design intent.
