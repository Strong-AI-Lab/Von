# JVNAUTOSCI-1654 Open Epic Review

Date: 2026-04-03

## Objective

Review the currently open epics through one practical lens:

- Which work is most likely to improve whether Von correctly understands and successfully carries out user intent end-to-end?

This note deliberately prioritises execution correctness, knowledge-grounded action, and visible completion over local subsystem elegance.

## Evaluation lens

An epic scores highly if it materially improves one or more of these:

1. Von chooses the right workflow/tool path for the user's intent.
2. Von completes the intended action rather than reporting a false success.
3. Von can retrieve, aggregate, and present the right knowledge without brittle code-side fallbacks.
4. We can measure failures directly and regress them reliably.
5. Behaviour remains KB-authoritative and workflow-first rather than drifting back into Python-side special cases.

## Recommended next tranche

### 1. Make intent completion measurable and hard to fake

Primary epics:

- `JVNAUTOSCI-833` Agentic Behaviours in Von
- `JVNAUTOSCI-964` Von Experimental Evaluation and Measurement
- `JVNAUTOSCI-536` Observability & Performance

Why this comes first:

- The main current risk is not missing capability in the abstract; it is incomplete or misrouted execution being surfaced as if the user's intent had been handled.
- Existing evaluation tasks under `JVNAUTOSCI-964` are still generic (`JVNAUTOSCI-965`, `JVNAUTOSCI-966`, `JVNAUTOSCI-967`) and do not yet form a strong operational gate on real intent completion.
- `JVNAUTOSCI-833` is the right umbrella, but its remaining open tasks are sparse and under-represent workflow repair, completion validation, and workflow generation/repair.

Recommended output of this tranche:

- A turn-level benchmark for user-intent success/failure using `turn_execution_*` and related critic/evaluation artefacts.
- Dashboards and regression views that separate:
  - successful completion
  - false success
  - unresolved follow-up needed
  - tool/workflow misrouting
- A small set of acceptance scenarios that are close to real user requests and run repeatedly.

### 2. Finish the KB substrate Von needs in order to act intelligently

Primary epics:

- `JVNAUTOSCI-932` Memory and knowledge retention
- `JVNAUTOSCI-936` Representationally rich Vontology + inference engine

Why this comes second:

- Reliable action selection needs durable context bundles, memory/event structure, and better query/aggregation surfaces.
- `JVNAUTOSCI-936` already has the most directly useful current task in progress: `JVNAUTOSCI-254` (`Vontology-first context bundles and reconstructed dossiers for concepts and workflows`).
- `JVNAUTOSCI-932` contains the work needed to stop relying on stale mixed representations and weak artefact handling.

Recommended output of this tranche:

- Finish context bundles/dossiers so workflows can operate on a bounded, reconstructed, provenance-bearing representation instead of scattered ad hoc lookups.
- Prioritise memory/event and artefact tasks that directly improve current behaviour:
  - `JVNAUTOSCI-972`
  - `JVNAUTOSCI-969`
  - `JVNAUTOSCI-970`
  - `JVNAUTOSCI-971`
  - `JVNAUTOSCI-973`
- Reinterpret or defer older low-leverage tasks under these epics unless they clearly unblock the current architecture.

### 3. Build one strong vertical slice for KB aggregation and recommendation

Primary epics:

- `JVNAUTOSCI-538` Knowledge Discovery & Recommendation
- `JVNAUTOSCI-144` Von - Document Representation and Handling
- `JVNAUTOSCI-1553` Paper Recommender

Why this is third rather than first:

- Recommendation quality will not be robust until the execution/evaluation substrate and KB query/aggregation substrate are stronger.
- Once those are in place, paper recommendation is a strong proving ground because it exercises:
  - document ingestion and representation
  - retrieval and aggregation
  - explanation and provenance
  - user feedback loops

Important portfolio note:

- `JVNAUTOSCI-1553` already contains `JVNAUTOSCI-1554`, which explicitly re-scopes paper recommendation as a workflow-first capability and supersedes legacy subtasks.
- That is the right direction.
- Treat `JVNAUTOSCI-1553` as a vertical slice inside the broader goals of `JVNAUTOSCI-538` and `JVNAUTOSCI-144`, not as an independent strategy epic.

### 4. Expose KB-driven presentation, not generic UI polish

Primary epics:

- `JVNAUTOSCI-866` Beautiful, flexible UX for a knowledge-using and -creating AI system
- `JVNAUTOSCI-537` Von Frontend UI / UX & Interaction Improvements

Why this is fourth:

- UI work matters, but most current open UI tasks are polish/convenience oriented rather than directly improving whether intent is carried out correctly.
- The high-value UI work is the part that makes KB-driven, provenance-bearing, workflow-produced results understandable and actionable.

Recommended output of this tranche:

- KB-defined views for high-value concept/workflow/document contexts.
- Explanation/provenance affordances for aggregated results and recommendations.
- Minimal but strong interaction loops around accepting, editing, rejecting, and refining generated/aggregated outputs.

### 5. Harden the pathways that must fail closed

Primary epics:

- `JVNAUTOSCI-857` Production hardening
- `JVNAUTOSCI-539` Access & Governance

Why this should run in parallel where feasible:

- As soon as Von becomes better at carrying out intent, the cost of unsafe or non-deterministic behaviour rises.
- Some of the open tasks here are directly relevant to current operational safety and should not wait:
  - `JVNAUTOSCI-1438` Add authentication to admin endpoints
  - `JVNAUTOSCI-1437` Implement rate limiting on Flask API endpoints
  - permission/visibility work such as `JVNAUTOSCI-871`, `JVNAUTOSCI-638`, `JVNAUTOSCI-627`

## Epic-by-epic disposition

### Advance now

- `JVNAUTOSCI-833` Agentic Behaviours in Von
  - Core umbrella for workflow-first execution, repair, planning, tool use, and Vontology extension.
  - Needs stronger child-task focus on completion correctness, workflow generation, and repair.

- `JVNAUTOSCI-964` Von Experimental Evaluation and Measurement
  - Should become the operational measurement epic for user-intent success, not just general experimentation documentation.

- `JVNAUTOSCI-932` Memory and knowledge retention
  - High leverage because context continuity and artefact retention directly affect whether Von can complete multi-step intent reliably.

- `JVNAUTOSCI-936` Representationally rich Vontology + inference engine
  - High leverage because better query/aggregation/dossier surfaces feed both routing and execution.

- `JVNAUTOSCI-536` Observability & Performance
  - Necessary to see failure modes and latency bottlenecks in real call paths.
  - Currently under-scoped relative to its importance.

- `JVNAUTOSCI-857` Production hardening
  - Advance the fail-closed and admin/auth safety tasks that protect current behaviour.

### Advance as a vertical slice after the foundations above

- `JVNAUTOSCI-538` Knowledge Discovery & Recommendation
  - Worth doing, but only after the execution and KB substrates are stronger.

- `JVNAUTOSCI-144` Document Representation and Handling
  - Important as part of the document/paper vertical slice.
  - Many open tasks are old and need sceptical reinterpretation.

- `JVNAUTOSCI-1553` Paper Recommender
  - Good proving ground, but should be subordinate to the broader recommendation/document foundations.

### Supporting epics; advance only where they unblock the work above

- `JVNAUTOSCI-535` Data Model & Normalization
  - Useful only insofar as it removes mixed/legacy representation pain for `JVNAUTOSCI-932` and `JVNAUTOSCI-936`.

- `JVNAUTOSCI-539` Access & Governance
  - Important supporting safety epic; coordinate with `JVNAUTOSCI-857` rather than driving an independent strategy.

- `JVNAUTOSCI-1116` Architectural De-bloating & Modularization
  - Worth doing when a hotspot blocks progress, but not as a primary prioritisation axis for user-intent success.

### Defer for now

- `JVNAUTOSCI-537` Von Frontend UI / UX & Interaction Improvements
  - Defer the polish-heavy items; keep only the tasks that directly help users understand or steer KB/workflow outcomes.

- `JVNAUTOSCI-866` Beautiful, flexible UX for a knowledge-using and -creating AI system
  - Keep as the long-term design north star, but do not treat generic beauty/flexibility as the next bottleneck.

- `JVNAUTOSCI-1117` UI/UX Modernization & Onboarding
  - Broad overlap with `JVNAUTOSCI-537` and `JVNAUTOSCI-866`.

- `JVNAUTOSCI-885` Multimodal / sensory input-output enablement
  - Broadens channels without fixing core intent completion.

- `JVNAUTOSCI-1460` External interaction surfaces and gateway runtime
  - Same reason as `JVNAUTOSCI-885`; defer until core execution is stronger.

- `JVNAUTOSCI-541` Onboarding & Lab Operations
  - Useful, but not on the critical path for the behaviour in question.

### Consolidate / reinterpret

- `JVNAUTOSCI-766` Vontology-based Tool Usage Heuristics System
  - The goal is still valid, but the framing is now too separate from workflow-first execution.
  - Reinterpret as KB-authored routing/workflow metadata under `JVNAUTOSCI-833`, not as a separate cached heuristics subsystem.

- `JVNAUTOSCI-1117` UI/UX Modernization & Onboarding
  - Likely better folded into `JVNAUTOSCI-537` and `JVNAUTOSCI-866`.

- `JVNAUTOSCI-1553` Paper Recommender
  - Keep only as a vertical slice epic if it helps coordination; otherwise its scope belongs under recommendation/document foundations.

### Retire or explicitly supersede after quick confirmation

- `JVNAUTOSCI-710` Project Codebase Refactoring and Documentation for Public Release
  - The description explicitly says the epic is no longer used and points to `JVNAUTOSCI-739`.
  - It should not remain open in its current state.

- `JVNAUTOSCI-739` Codebase transition - private repo to Von
  - Review whether any open children still matter.
  - If not, close or supersede it so it stops competing for attention.

### Coordination only

- `JVNAUTOSCI-934` Project admin/coordination
  - Keep as the umbrella for planning/review/admin tasks like this one, not as a feature-priority destination.

## Most important overlaps to clean up

### 1. Execution / routing overlap

- `JVNAUTOSCI-833` and `JVNAUTOSCI-766` overlap heavily.
- The present architecture direction should favour workflow metadata, selector metadata, executable-completeness checks, and KB-authored routing surfaces under `JVNAUTOSCI-833`.

### 2. Recommendation overlap

- `JVNAUTOSCI-538`, `JVNAUTOSCI-144`, and `JVNAUTOSCI-1553` overlap.
- Suggested framing:
  - `JVNAUTOSCI-538` = the broad capability
  - `JVNAUTOSCI-144` = document/paper representation substrate
  - `JVNAUTOSCI-1553` = one vertical slice or milestone, not a separate strategy

### 3. UX overlap

- `JVNAUTOSCI-537`, `JVNAUTOSCI-866`, and `JVNAUTOSCI-1117` overlap.
- Suggested framing:
  - `JVNAUTOSCI-866` = long-term KB-defined UI vision
  - `JVNAUTOSCI-537` = concrete frontend execution work
  - `JVNAUTOSCI-1117` = likely unnecessary as a separate open epic

### 4. Safety / governance overlap

- `JVNAUTOSCI-857` and `JVNAUTOSCI-539` are closely related.
- Keep both only if one stays focused on operational hardening and the other on permission/governance semantics. Otherwise, use links and comments to prevent duplicate work.

## KB-authoritative surfaces to prioritise

If the goal is better user-intent execution, the following should be treated as first-class authored surfaces:

- workflow concepts, launch contracts, and executable-completeness metadata
- routing/selection metadata and repair/recovery workflow definitions
- prompt concepts for workflow generation, workflow repair, and explanation/provenance shaping
- turn execution records, critic bundles, experiment specs, and experiment runs
- context bundle / dossier representations for concepts, workflows, documents, and artefacts
- view definitions and renderer applicability metadata for KB-driven presentation
- document/paper concepts plus file-copy and provenance relations for recommendation/explanation workflows

Supporting code surfaces should remain limited to:

- runtime enforcement
- validation
- execution plumbing
- telemetry collection
- deterministic rendering / safety envelopes

## Concrete suggested next tasks

These are the work items I would tackle next, in order, whether by reinterpreting existing tasks or by creating fresh ones if the current task inventory does not fit cleanly.

1. Intent-completion benchmark and false-success dashboard
   - Epic home: `JVNAUTOSCI-964` linked to `JVNAUTOSCI-833` and `JVNAUTOSCI-536`
   - Measure real end-to-end intent success, not just tool counts.

2. Workflow generation and repair surface
   - Epic home: `JVNAUTOSCI-833`
   - Add KB-authored workflow templates/metadata plus executable validation and repair pathways.

3. Context bundles/dossiers plus episodic/artefact completion
   - Epic home: `JVNAUTOSCI-936` and `JVNAUTOSCI-932`
   - Finish the bounded, provenance-rich context substrate Von needs before acting.

4. Paper recommendation vertical slice
   - Epic home: `JVNAUTOSCI-538` / `JVNAUTOSCI-144` / `JVNAUTOSCI-1553`
   - Use it as a proving ground for retrieval, aggregation, explanation, and user feedback.

5. Admin/auth/namespace hardening
   - Epic home: `JVNAUTOSCI-857` with `JVNAUTOSCI-539`
   - Prioritise fail-closed pathways and remove silent privileged gaps.

## Merge guidance from `JVNAUTOSCI-1655`

`JVNAUTOSCI-1655` surfaced several useful concrete refinements. For a later unified planning item, I would keep this note (`JVNAUTOSCI-1654`) as the backbone and selectively import the strongest operational ideas from `JVNAUTOSCI-1655`.

### Points to import into the later unified plan

- Make a `selector evaluation benchmark` an explicit named deliverable inside the top-priority execution-correctness tranche.
- Pull `JVNAUTOSCI-1429` forward explicitly as a concrete dispatch-latency and visibility problem, not just a generic observability concern.
- State the `KB aggregation and presentation gap` more directly: users need inspectable views of what Von knows, why it believes it, and what provenance supports the current output.
- Make `workflow generation from intent` an explicit planning gap under `JVNAUTOSCI-833`, even if it does not become a separate epic.
- Triage `JVNAUTOSCI-857` into reliability-critical fail-closed work versus later hardening work, so it does not remain a large undifferentiated backlog.

### Points to reject or reframe when merging

- Do not keep `JVNAUTOSCI-766` as a separate top-priority track. Reinterpret its useful content under `JVNAUTOSCI-833` as KB-authored routing, selector, and workflow metadata work.
- Do not treat `JVNAUTOSCI-964` and `JVNAUTOSCI-536` as largely separate concerns. For this purpose, measurement and observability should be planned as one coupled workstream.
- Do not pull memory-injection style tasks ahead of dossier/context/provenance substrate work. The substrate should come first, otherwise the likely result is heuristic context stuffing rather than durable improvement.
- Do not treat production hardening as mostly late strategic work. Auth, namespace, rate-limit, and other fail-closed protections should advance in parallel with stronger execution capabilities.
- Do not create a separate new epic for workflow generation unless `JVNAUTOSCI-833` genuinely cannot hold that scope cleanly.

### Practical merge rule

If `JVNAUTOSCI-1654` and `JVNAUTOSCI-1655` are later merged into one unified planning item, the right synthesis is:

1. Keep `JVNAUTOSCI-1654` as the main prioritisation and portfolio-cleanup structure.
2. Import from `JVNAUTOSCI-1655` the selector benchmark, explicit dispatch-gap work, explicit KB-inspectability gap, workflow-generation-from-intent gap, and `JVNAUTOSCI-857` triage.
3. Exclude or reframe the parts of `JVNAUTOSCI-1655` that would split routing work away from `JVNAUTOSCI-833`, over-separate measurement from observability, or prioritise heuristic memory injection ahead of the substrate.

## Short conclusion

If the criterion is "make Von more likely to carry out user intent successfully", the next portfolio centre of gravity should be:

1. measurable execution correctness
2. KB query/aggregation/context substrate
3. one strong recommendation/document vertical slice
4. explanation/provenance-driven presentation
5. fail-closed hardening

The biggest planning mistake to avoid is spending the next tranche on generic UX modernisation, multimodal expansion, or architectural tidying before the system can reliably complete and verify the intents it already nominally supports.
