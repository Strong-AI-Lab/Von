# JVNAUTOSCI-1657 Phase 2 Operationalisation

**Status**: Operationalisation note  
**Primary Jira tasks**: `JVNAUTOSCI-1661`, `JVNAUTOSCI-1657`  
**Date**: 2026-04-03

## 1. Purpose

This note turns Phase 2 of `JVNAUTOSCI-1657` into a concrete, non-duplicative KB-substrate task set.

The Phase 1 review concluded that the dominant remaining failure class is unresolved execution rather than hidden false success. The next tranche should therefore improve the substrate Von uses to carry context, memory, artefact provenance, and structured representation, while keeping the Phase 1 benchmark stack active in shadow-gate mode.

This is not a new backlog. It is a clarification of which existing tasks are the active Phase 2 spine, what each epic owns, and which work must remain gated.

## 2. Phase 2 Backbone

Phase 2 should be treated as two coordinated workstreams.

### 2.1 `JVNAUTOSCI-936` owns KB context/dossier substrate and representational normalisation

This epic should own the graph-first representational substrate that lets Von reconstruct bounded working context rather than stuffing prompts with heuristic long-range text.

Active Phase 2 tasks in this line:

- `JVNAUTOSCI-254` as the canonical context-bundles and reconstructed-dossiers task
- `JVNAUTOSCI-1563` for hybrid dossier/theory representation
- `JVNAUTOSCI-1564` for bounded workspace reconstruction contract and telemetry
- `JVNAUTOSCI-1565` for ablation and retained-case evaluation
- `JVNAUTOSCI-531` for unified relation/text-link normalisation

Role of this line:

- define the authoritative context-bundle and dossier artefact model
- make bounded reconstructed workspaces reusable across workflow families
- keep representation graph-first and promotion-aware
- reduce drift between concept/text relations, assertion state, theory-local state, and workflow-local report state

### 2.2 `JVNAUTOSCI-932` owns episodic memory, artefact provenance/privacy, and durable retention

This epic should own the memory and artefact substrate that stores durable evidence safely and makes it retrievable without breaking namespace, provenance, or privacy boundaries.

Active Phase 2 tasks in this line:

- `JVNAUTOSCI-972` as the episodic-memory event-model anchor
- `JVNAUTOSCI-969` for provenance and privacy/namespace gating
- `JVNAUTOSCI-970` for durable artefact storage abstraction and ingestion pipeline
- `JVNAUTOSCI-971` for artefact metadata extraction and ontology linking

Already-landed anchors that should be treated as part of the substrate, not separate directions:

- `JVNAUTOSCI-1607` for `#V#episode_critique_memory` artefacts and queryable projections
- `JVNAUTOSCI-1395` for generic remote URL artefact ingestion and `#V#computer_file_copy` registration

Role of this line:

- give episodic memory a first-class event structure
- make stored artefacts and memories provenance-aware and namespace-safe
- preserve durable file-copy and memory records as authoritative KB artefacts
- provide retrieval eligibility and auditability before memory injection becomes a default behaviour

## 3. Authoritative KB Artefacts for This Tranche

The key Phase 2 artefacts should be named explicitly and treated as authoritative KB surfaces rather than repo-local pseudo-structures.

### 3.1 Context bundles and dossiers

The context/dossier line should centre on the artefacts named in `JVNAUTOSCI-254`:

- `#V#context_bundle`
- `#V#workflow_context_bundle`
- `#V#concept_context_bundle`
- `#V#context_facet`
- `#V#context_dossier`
- `#V#workflow_report_revision`
- `#V#evidence_receipt`

These should carry:

- inheritable graph-first context
- bounded textual synthesis where text is genuinely primary
- explicit evidence receipts and revision history
- report-state and reconstruction semantics that can survive long-horizon work without collapsing into one large prompt transcript

### 3.2 Hybrid dossier and theory-local state

Phase 2 should treat hybrid dossier reconstruction as mixed textual and KR state, not as text-only memory.

The relevant authoritative artefacts and control surfaces are:

- `#V#testing_theory`
- theory-local assertions and promotion/rollback surfaces
- mixed dossier/report state described in `JVNAUTOSCI-1563`
- bounded workspace reconstruction and telemetry described in `JVNAUTOSCI-1564`

This is the right home for counterfactual branches, local hypotheses, promotable assertions, open questions, and bounded reconstructed workspace state.

### 3.3 Episodic memory and event structure

The episodic-memory line should use:

- Davidsonian event representations under `JVNAUTOSCI-972`
- `#V#episode_critique_memory` as the already-landed exemplar durable memory artefact
- event-linked projections that remain namespace-safe and provenance-aware

The event model should be the common substrate for interaction episodes, memory-worthy mutations, and later durable memory retrieval.

### 3.4 Artefact retention and provenance

The artefact line should use:

- `#V#computer_file_copy` as the canonical durable file-copy artefact
- provenance/privacy/namespace controls from `JVNAUTOSCI-969`
- durable artefact storage and ingestion from `JVNAUTOSCI-970`
- metadata extraction and ontology linkage from `JVNAUTOSCI-971`

This line already has a partial operational seam through `JVNAUTOSCI-1395`, but that should be treated as a landed foundation rather than a substitute for the remaining provenance and event-model work.

### 3.5 Representational normalisation

`JVNAUTOSCI-531` is the clearest Phase 2 normalisation task.

It should define the canonical direction for:

- unified relation/text-link representation
- graph-first handling of concept-to-concept and concept-to-text assertions
- future n-ary or reified relation patterns needed for event, provenance, and qualifier-rich memory structures

Phase 2 work should align with that direction rather than create new ad hoc storage forms.

## 4. Explicit Gating for Retrieval-Time Memory Injection

`JVNAUTOSCI-968` and `JVNAUTOSCI-995` should remain gated.

They should not be treated as the leading edge of Phase 2. They should start only once the following substrate conditions are materially in place:

1. `JVNAUTOSCI-254`, `JVNAUTOSCI-1563`, and `JVNAUTOSCI-1564` have made bounded context-bundle and dossier reconstruction explicit and reusable.
2. `JVNAUTOSCI-969` has made provenance, privacy, and namespace gating explicit for stored memories and artefacts.
3. `JVNAUTOSCI-972` has established the episodic-memory event-model anchor.
4. `JVNAUTOSCI-531` has at least fixed the canonical representational direction for relation/text-link normalisation so retrieval reads do not target a drifting storage model.

Interpretation of the gate:

- retrieval-time context injection is not blocked on the entire Phase 2 tranche being complete
- it is blocked on the dossier/provenance/event-model substrate being trustworthy enough that injected memory has a stable representation, safe retrieval boundary, and clear audit trail

## 5. Clean Task Set and Ownership Map

The clean Phase 2 task set should be read as follows.

### 5.1 Primary active tasks

- `JVNAUTOSCI-254`, `JVNAUTOSCI-1563`, `JVNAUTOSCI-1564`, `JVNAUTOSCI-1565`
- `JVNAUTOSCI-531`
- `JVNAUTOSCI-972`
- `JVNAUTOSCI-969`
- `JVNAUTOSCI-970`
- `JVNAUTOSCI-971`

### 5.2 Supporting already-landed anchors

- `JVNAUTOSCI-1607`
- `JVNAUTOSCI-1395`

### 5.3 Explicitly gated later tasks

- `JVNAUTOSCI-968`
- `JVNAUTOSCI-995`

This is sufficient to represent Phase 2 as a clean task set in Jira without creating duplicate planning tickets.

## 6. Recommended Sequencing Inside Phase 2

The recommended execution order is:

1. Treat `JVNAUTOSCI-254` plus `JVNAUTOSCI-1563` and `JVNAUTOSCI-1564` as the main context/dossier substrate line.
2. Run `JVNAUTOSCI-969` and `JVNAUTOSCI-972` in parallel as the memory-safety and event-model anchors.
3. Keep `JVNAUTOSCI-531` active as the representation-normalisation direction-setting task, but do not block all Phase 2 work on full migration completion.
4. Use `JVNAUTOSCI-970` and `JVNAUTOSCI-971` to generalise the artefact substrate beyond the currently landed file-copy seam.
5. Run `JVNAUTOSCI-1565` once enough substrate exists to compare simpler context strategies against reconstructed-dossier approaches.
6. Start `JVNAUTOSCI-968` and `JVNAUTOSCI-995` only after the gate in Section 4 is judged satisfied.

## 7. What This Clarifies

This operationalisation clarifies five things.

1. Phase 2 is not “do memory injection next”. It is “finish the substrate that makes memory injection safe and useful”.
2. `JVNAUTOSCI-254` and its subtasks are not side work. They are the core context-bundle/dossier spine for this tranche.
3. `JVNAUTOSCI-1607` and `JVNAUTOSCI-1395` are meaningful landed foundations, but they do not remove the need for `JVNAUTOSCI-969`, `JVNAUTOSCI-970`, `JVNAUTOSCI-971`, and `JVNAUTOSCI-972`.
4. `JVNAUTOSCI-531` matters because the substrate should not stabilise on top of fragmented relation/text-link semantics.
5. The Phase 1 benchmark stack remains active during all of this in shadow-gate mode.

## 8. Conclusion

`JVNAUTOSCI-1661` should be considered fully implemented once Jira reflects this task set, this ownership split, and this gating rule.

Phase 2 can now proceed as a coherent KB-substrate programme rather than as a mix of memory ideas, context-window hacks, and partially overlapping storage tasks.
