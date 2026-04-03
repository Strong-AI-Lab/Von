# JVNAUTOSCI-1657 Phase 2 Review

**Status**: Review/update note  
**Primary Jira tasks**: `JVNAUTOSCI-1662`, `JVNAUTOSCI-1657`  
**Date**: 2026-04-03

## 1. Purpose

This note reviews the actual implementation state of the Phase 2 KB-substrate tranche under `JVNAUTOSCI-1657`.

`JVNAUTOSCI-1661` operationalised the tranche well. The remaining question for `JVNAUTOSCI-1662` is different: whether enough of that substrate has actually landed to justify moving into the document/recommendation vertical slice and the KB-driven presentation tranche.

The short answer is:

- meaningful substrate anchors exist
- the Phase 2 task set is still only partly implemented
- broad vertical-slice expansion should remain gated

## 2. Evidence Snapshot

### 2.1 Phase 2 task state

The active Phase 2 spine is still largely unimplemented:

- `JVNAUTOSCI-254`: `In Progress`
- `JVNAUTOSCI-1563`: `To Do`
- `JVNAUTOSCI-1564`: `To Do`
- `JVNAUTOSCI-1565`: `To Do`
- `JVNAUTOSCI-531`: `To Do`
- `JVNAUTOSCI-972`: `To Do`
- `JVNAUTOSCI-969`: `To Do`
- `JVNAUTOSCI-970`: `To Do`
- `JVNAUTOSCI-971`: `To Do`

Later-gated tasks remain unstarted, as expected:

- `JVNAUTOSCI-968`: `To Do`
- `JVNAUTOSCI-995`: `To Do`

Two anchor tasks are already landed:

- `JVNAUTOSCI-1607`: `Done`
- `JVNAUTOSCI-1395`: `Done`

### 2.2 Authoritative KB artefact state

The intended dossier/context artefacts named in the Phase 2 operationalisation note are not yet present as canonical concepts:

- `#V#context_bundle`: absent
- `#V#workflow_context_bundle`: absent
- `#V#concept_context_bundle`: absent
- `#V#context_dossier`: absent
- `#V#workflow_report_revision`: absent
- `#V#evidence_receipt`: absent

Some important substrate artefacts do exist:

- `#V#testing_theory`: present
- `#V#episode_critique_memory`: present
- `#V#computer_file_copy`: present

## 3. What Has Actually Landed

### 3.1 Durable artefact/file-copy seam

`JVNAUTOSCI-1395` has already given Von a meaningful durable artefact seam around `#V#computer_file_copy`.

That means:

- remote URL artefacts can already be persisted
- file-copy registration is already a real KB path
- some document-oriented flows can build on a genuine substrate rather than a hypothetical one

### 3.2 Episodic-memory exemplar

`JVNAUTOSCI-1607` has already given Von a meaningful memory artefact seam around `#V#episode_critique_memory`.

That means:

- the system already has one real pattern for durable episodic-memory-style artefacts
- Phase 2 is not starting from zero on memory representation
- later event-model work has a concrete exemplar to generalise from

### 3.3 Testing-theory substrate

`#V#testing_theory` already exists, and the repo contains testing-workflow contract surfaces for theory-local state.

That means:

- hybrid dossier/theory work is not wholly speculative
- there is already a canonical place for some local, promotable, bounded KR state

## 4. What Has Not Yet Landed

### 4.1 Dossier/context-bundle substrate is not materially available yet

The central Phase 2 context-bundle and reconstructed-dossier line is not yet landed in a way that later phases can safely rely on.

The most important evidence is:

- `JVNAUTOSCI-254` is still only `In Progress`
- `JVNAUTOSCI-1563` and `JVNAUTOSCI-1564` are still `To Do`
- the named bundle/dossier concepts do not yet exist in Vontology

So the current state is: the intended design direction is clear, but the authoritative dossier substrate is not yet real enough to underwrite later-phase implementation.

### 4.2 Provenance/privacy and event-model substrate are not landed yet

The Phase 2 note correctly treated `JVNAUTOSCI-969` and `JVNAUTOSCI-972` as central anchors. Both remain `To Do`.

That means the tranche still lacks:

- explicit provenance/privacy/namespace gating for stored memories and artefacts
- the general episodic-memory event model intended to support broader retrieval and reasoning

This is a substantive blocker for treating retrieval-time memory use as trustworthy by default.

### 4.3 Representational direction is not yet fixed enough

`JVNAUTOSCI-531` remains `To Do`.

That matters because the later phases should not build broad new behaviour on top of relation/text-link semantics that are still expected to move.

### 4.4 Retrieval-time memory injection remains correctly gated

Because the dossier, provenance, and event-model substrate has not yet landed, the earlier gating decision remains correct:

- `JVNAUTOSCI-968` should stay gated
- `JVNAUTOSCI-995` should stay gated

This review does not weaken that gate.

## 5. Readiness Verdicts

### 5.1 Broad document/recommendation vertical-slice implementation

**Verdict: not ready**

The vertical slice should not yet be expanded as if Phase 2 were complete. The missing dossier/context-bundle, provenance/privacy, and event-model substrate would make the slice too likely to grow around provisional or partial representations.

### 5.2 Narrow vertical-slice planning and re-scoping work

**Verdict: ready now**

Planning and re-scoping work for the vertical slice can proceed now, especially:

- `JVNAUTOSCI-1554` to re-scope the paper recommender as a workflow-first capability
- bounded planning inside `JVNAUTOSCI-538`, `JVNAUTOSCI-144`, and `JVNAUTOSCI-1553`

This kind of work does not require Phase 2 to be complete, and it will reduce later ambiguity.

### 5.3 Narrow implementation that reuses already-landed artefact seams

**Verdict: only with care**

A narrow implementation that reuses already-landed seams such as `#V#computer_file_copy` may be justified in limited cases, but it should not be treated as evidence that the broader Phase 2 gate is satisfied.

In practice, this means:

- existing document/file-copy flows can continue to be hardened
- they should not be misread as a substitute for `254 / 1563 / 1564 / 969 / 972 / 531`

### 5.4 KB-driven presentation tranche

**Verdict: not ready**

KB-driven presentation depends heavily on stable dossier/context/provenance structures. Because those are not yet landed, the presentation tranche should remain behind the same substrate gate rather than advancing as if the KB views were already authoritative.

## 6. Updated Entry Criteria for Later Phases

`JVNAUTOSCI-1657` should be read with the following explicit entry criteria.

### 6.1 Before broad Phase 3 implementation

The following should be materially true before broad document/recommendation implementation starts:

1. `JVNAUTOSCI-254` has landed a real context-bundle/dossier substrate rather than just a task description and intended artefact list.
2. `JVNAUTOSCI-1563` and `JVNAUTOSCI-1564` have landed, or their essential hybrid-dossier and bounded-reconstruction capabilities have been delivered through a justified narrower path.
3. `JVNAUTOSCI-969` has landed explicit provenance/privacy/namespace gating for stored artefacts and memories.
4. `JVNAUTOSCI-972` has landed the event-model anchor, or Phase 2 has otherwise established an equally clear general event representation.
5. `JVNAUTOSCI-531` has at least fixed the canonical representational direction strongly enough that later work is not building on a drifting storage model.
6. `JVNAUTOSCI-1554` has re-scoped the paper/document vertical slice in workflow-first terms.

### 6.2 What is not required before planning can continue

The following should not be treated as blockers for planning/re-scoping work:

- `JVNAUTOSCI-1565`
- `JVNAUTOSCI-973`
- `JVNAUTOSCI-1276`

These remain valuable, but they are not the minimum gate for saying whether Phase 3 planning may continue.

## 7. Recommended Update to the Unified Plan

After this review, `JVNAUTOSCI-1657` should be interpreted as follows:

1. Phase 2 remains the current active implementation phase.
2. The document/recommendation tranche is still the right next proving ground, but only after the Phase 2 substrate gate is materially satisfied.
3. The immediate later-phase work that can proceed now is planning/re-scoping work, especially `JVNAUTOSCI-1554`, rather than broad implementation.
4. The memory-injection tasks `968` and `995` remain later tasks, not readiness indicators.
5. The KB-driven presentation tranche should remain gated on the same dossier/provenance stability that gates the broader vertical slice.

## 8. Conclusion

`JVNAUTOSCI-1662` should be considered complete once `JVNAUTOSCI-1657` explicitly reflects this review outcome:

- Phase 2 has good foundations but is not yet complete enough for broad vertical-slice expansion
- vertical-slice planning may proceed now
- broad vertical-slice implementation and KB-driven presentation should remain gated
- the gating conditions are explicit and evidence-backed rather than implicit
