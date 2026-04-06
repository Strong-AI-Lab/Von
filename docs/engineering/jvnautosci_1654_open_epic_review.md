# JVNAUTOSCI-1654 Open Epic Review

Initial review: 2026-04-03
Refreshed: 2026-04-07

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

## Portfolio status as of 2026-04-07

### Completed epics (closed since initial review)

| Key | Summary | Completed |
|-----|---------|-----------|
| JVNAUTOSCI-833 | Agentic Behaviours in Von | 2026-04-07 |
| JVNAUTOSCI-964 | Von Experimental Evaluation and Measurement | 2026-04-06 |
| JVNAUTOSCI-1553 | Paper Recommender | 2026-04-05 |
| JVNAUTOSCI-1586 | Workflow Studio | 2026-04-06 |

These completions shift the portfolio balance. The agentic-behaviour umbrella and the evaluation infrastructure are now closed. Paper recommendation has a completed vertical slice. Workflow Studio delivered independent workflow listing, visualisation, and AI-assisted editing. The question is now: what should follow?

### New epics (added since initial review)

| Key | Summary | Status | Impact on intent execution |
|-----|---------|--------|---------------------------|
| JVNAUTOSCI-1667 | Von Coding Agent VS Code Extension Plugin | To Do | Low — new interaction surface, not an intent-execution bottleneck |
| JVNAUTOSCI-1714 | Paper recommendation workflow extensions | To Do | Medium — extends the completed paper-recommender vertical slice |
| JVNAUTOSCI-1752 | Type-specific instance renderers for concept pages | To Do | High — KB-driven presentation that directly improves how users recognise, orient, and act on represented knowledge |

### Open epic inventory (27 epics, excluding SUPERSEDED-933)

**In Progress (4):**
- JVNAUTOSCI-144 — Document Representation and Handling
- JVNAUTOSCI-167 — Continuously Running, Self-Updating Von Prototype
- JVNAUTOSCI-537 — Von Frontend UI/UX & Interaction Improvements
- JVNAUTOSCI-932 — Memory and knowledge retention
- JVNAUTOSCI-936 — Representationally rich Vontology + inference engine

**To Do (19):**
- JVNAUTOSCI-535 — Data Model & Normalisation
- JVNAUTOSCI-536 — Observability & Performance
- JVNAUTOSCI-538 — Knowledge Discovery & Recommendation
- JVNAUTOSCI-539 — Access & Governance
- JVNAUTOSCI-541 — Onboarding & Lab Operations
- JVNAUTOSCI-710 — Project Codebase Refactoring for Public Release
- JVNAUTOSCI-739 — Codebase transition (private to Von)
- JVNAUTOSCI-766 — Vontology-based Tool Usage Heuristics
- JVNAUTOSCI-857 — Production hardening
- JVNAUTOSCI-866 — Beautiful, flexible UX
- JVNAUTOSCI-885 — Multimodal/sensory I/O enablement
- JVNAUTOSCI-934 — Project admin/coordination
- JVNAUTOSCI-977 — External communications channels (Gmail, Slack)
- JVNAUTOSCI-1116 — Architectural De-bloating & Modularisation
- JVNAUTOSCI-1117 — UI/UX Modernisation & Onboarding
- JVNAUTOSCI-1119 — System Reliability & Type Safety
- JVNAUTOSCI-1460 — External interaction surfaces (channels, voice, devices)
- JVNAUTOSCI-1667 — Von Coding Agent VS Code Extension
- JVNAUTOSCI-1714 — Paper recommendation workflow extensions
- JVNAUTOSCI-1752 — Type-specific instance renderers for concept pages

**Backlog (1):**
- JVNAUTOSCI-540 — Developer Platform & Infrastructure

## Recommended next tranche (refreshed)

With 833 and 964 now closed, the evaluation and agentic-behaviour foundations are in place. The priority shifts from "build the ability to measure" to "use measurement to drive KB substrate, recommendation quality, and hardening".

### 1. Finish the KB substrate Von needs to act intelligently

Primary epics:

- `JVNAUTOSCI-932` Memory and knowledge retention *(In Progress)*
- `JVNAUTOSCI-936` Representationally rich Vontology + inference engine *(In Progress)*

Why this is now first:

- With evaluation infrastructure delivered (964 Done), the main bottleneck is the quality and completeness of the context available to workflows at execution time.
- Context bundles/dossiers (JVNAUTOSCI-254) remain the most directly useful in-progress work. Finishing them means workflows operate on bounded, provenance-bearing representations instead of ad hoc lookups.
- Episodic memory and artefact tasks (972, 969, 970, 971, 973) directly improve whether Von retains the right information across multi-step interactions.

Recommended output:

- Context bundle/dossier substrate completed and integrated into workflow execution paths.
- Episodic/artefact memory tasks landed with real call-path validation.
- Older low-leverage tasks under these epics reinterpreted or deferred.

### 2. Extend the paper recommendation vertical slice into a full KB-aggregation proving ground

Primary epics:

- `JVNAUTOSCI-1714` Paper recommendation workflow extensions *(new, To Do)*
- `JVNAUTOSCI-538` Knowledge Discovery & Recommendation
- `JVNAUTOSCI-144` Document Representation and Handling *(In Progress)*

Why this moves up:

- Paper recommendation (1553) is now Done as a vertical slice. The follow-on epic (1714) extends it into researcher reading flows, explanation quality, and feedback loops.
- This is a natural proving ground for the KB substrate work above — it exercises retrieval, aggregation, explanation, provenance, and user feedback end-to-end.
- 538 provides the broader capability umbrella; 144 provides the document-representation backing.

Recommended output:

- Paper recommendation extended with workflow-first researcher reading flows.
- Explanation and provenance affordances surfaced alongside recommendations.
- Feedback loops that improve recommendation quality over time.
- Lessons from this slice generalised to other knowledge-discovery domains.

### 3. Observability and performance as a continuous discipline

Primary epic:

- `JVNAUTOSCI-536` Observability & Performance

Why this stays high:

- With 964 (evaluation) closed, 536 inherits the operational measurement responsibility. Intent-execution improvement requires ongoing measurement of where Von succeeds and fails.
- JVNAUTOSCI-1429 (dispatch latency/visibility) should be pulled forward as a concrete deliverable.
- Selector evaluation benchmarks should be established as an ongoing regression tool, not a one-off.

Recommended output:

- Dashboards and SLIs for intent-completion success/failure/false-success rates.
- Dispatch-latency and visibility fixes (1429).
- Selector evaluation benchmark running as repeatable regression.

### 4. Expose KB-driven presentation, not generic UI polish

Primary epics:

- `JVNAUTOSCI-1752` Type-specific instance renderers for concept pages *(new, To Do)*
- `JVNAUTOSCI-866` Beautiful, flexible UX for a knowledge-using and -creating AI system
- `JVNAUTOSCI-537` Von Frontend UI/UX & Interaction Improvements *(In Progress)*

Why this is fourth:

- UI work matters, but most current open UI tasks are polish/convenience oriented rather than directly improving whether intent is carried out correctly.
- The high-value UI work is the part that makes KB-driven, provenance-bearing, workflow-produced results understandable and actionable.
- Users need inspectable views of what Von knows, why it believes it, and what provenance supports the current output.
- `JVNAUTOSCI-1752` is the most concrete and immediately product-facing epic in this area. It defines type-specific summary renderers (papers, people, tasks, memories, rooms, events) driven by Vontology applicability metadata — not scattered frontend hard-coding. This is directly aligned with the intent-execution lens: users recognise, orient on, and act from what they see.

Recommended output:

- Type-specific instance renderers for at least several core concept families (papers, people, tasks, events, episodic memories) with furled/unfurled views.
- Renderer selection driven by Vontology type/applicability, not frontend special-cases.
- KB-defined views for high-value concept/workflow/document contexts.
- Explanation/provenance affordances for aggregated results and recommendations.
- Minimal but strong interaction loops around accepting, editing, rejecting, and refining generated/aggregated outputs.

### 5. Harden the pathways that must fail closed

Primary epics:

- `JVNAUTOSCI-857` Production hardening
- `JVNAUTOSCI-539` Access & Governance

Why this should run in parallel where feasible:

- As Von becomes better at carrying out intent, the cost of unsafe or non-deterministic behaviour rises.
- Auth, namespace, rate-limit, and other fail-closed protections should advance in parallel with stronger execution.
- Triage 857 into reliability-critical fail-closed work versus later hardening, so it does not remain a large undifferentiated backlog.
- Key tasks to prioritise now:
  - JVNAUTOSCI-1438 — Add authentication to admin endpoints
  - JVNAUTOSCI-1437 — Implement rate limiting on Flask API endpoints
  - Permission/visibility work: JVNAUTOSCI-871, JVNAUTOSCI-638, JVNAUTOSCI-627

## Epic-by-epic disposition

### Advance now (highest priority)

- `JVNAUTOSCI-932` Memory and knowledge retention *(In Progress)*
  - High leverage because context continuity and artefact retention directly affect whether Von can complete multi-step intent reliably.

- `JVNAUTOSCI-936` Representationally rich Vontology + inference engine *(In Progress)*
  - High leverage because better query/aggregation/dossier surfaces feed both routing and execution.

- `JVNAUTOSCI-536` Observability & Performance
  - Necessary to see failure modes and latency bottlenecks in real call paths.
  - Now also inherits ongoing evaluation/measurement responsibility from the closed 964 epic.

- `JVNAUTOSCI-857` Production hardening
  - Advance the fail-closed and admin/auth safety tasks that protect current behaviour.

### Advance as a vertical slice (second priority)

- `JVNAUTOSCI-1714` Paper recommendation workflow extensions *(new)*
  - Follow-on from the completed paper-recommender slice. Extends into researcher reading flows and feedback loops.

- `JVNAUTOSCI-538` Knowledge Discovery & Recommendation
  - Broad capability umbrella. Paper recommendation is the first proving ground; generalise from there.

- `JVNAUTOSCI-144` Document Representation and Handling *(In Progress)*
  - Important as the document/paper representation substrate.
  - Many open tasks are old and need sceptical reinterpretation.

### Supporting epics; advance only where they unblock work above

- `JVNAUTOSCI-535` Data Model & Normalisation
  - Useful only insofar as it removes mixed/legacy representation pain for 932 and 936.

- `JVNAUTOSCI-539` Access & Governance
  - Important supporting safety epic; coordinate with 857 rather than driving an independent strategy.

- `JVNAUTOSCI-1116` Architectural De-bloating & Modularisation
  - Worth doing when a hotspot blocks progress, but not a primary priority axis.

- `JVNAUTOSCI-1119` System Reliability & Type Safety
  - Supports robustness. Advance as it intersects with active work.

- `JVNAUTOSCI-167` Continuously Running, Self-Updating Von Prototype *(In Progress)*
  - Background enrichment improves data quality over time. Advance where it intersects with 932/936.

### Advance as KB-driven presentation (third–fourth priority)

- `JVNAUTOSCI-1752` Type-specific instance renderers for concept pages *(new)*
  - The most concrete KB-driven presentation epic. Defines type-specific summary renderers backed by Vontology applicability metadata.
  - Directly improves user recognition, orientation, and actionability on concept pages.
  - Coordinates with 866 (vision) and 537 (execution) but is more focused and deliverable.
  - Subsumes the intent of 1750 and 1751, which are early local manifestations of the same idea.

### Defer for now

- `JVNAUTOSCI-537` Von Frontend UI/UX & Interaction Improvements
  - Defer the polish-heavy items; keep only the tasks that directly help users understand or steer KB/workflow outcomes.

- `JVNAUTOSCI-866` Beautiful, flexible UX
  - Keep as the long-term design north star, but do not treat generic beauty/flexibility as the next bottleneck.

- `JVNAUTOSCI-1117` UI/UX Modernisation & Onboarding
  - Broad overlap with 537 and 866.

- `JVNAUTOSCI-885` Multimodal/sensory I/O enablement
  - Broadens channels without fixing core intent completion.

- `JVNAUTOSCI-1460` External interaction surfaces (channels, voice, devices)
  - Same reason as 885; defer until core execution is stronger.

- `JVNAUTOSCI-977` External communications channels (Gmail, Slack)
  - New interaction surfaces, not an intent-execution bottleneck.

- `JVNAUTOSCI-541` Onboarding & Lab Operations
  - Useful, but not on the critical path.

- `JVNAUTOSCI-1667` Von Coding Agent VS Code Extension *(new)*
  - Interesting new direction, but a separate interaction surface. Does not address the core intent-execution problem.

- `JVNAUTOSCI-540` Developer Platform & Infrastructure *(Backlog)*
  - Enables faster development but not user-facing.

### Consolidate / reinterpret

- `JVNAUTOSCI-766` Vontology-based Tool Usage Heuristics System
  - The goal is valid, but the framing is now too separate from workflow-first execution.
  - With 833 closed, the routing/selector work that 766 describes should be reinterpreted under the active KB substrate epics (932, 936) or a successor agentic-behaviour epic.
  - Recommend: supersede or fold into 936 with an explicit KB-routing sub-scope.

- `JVNAUTOSCI-1117` UI/UX Modernisation & Onboarding
  - Likely better folded into 537 and 866.

### Retire or explicitly supersede after quick confirmation

- `JVNAUTOSCI-710` Project Codebase Refactoring for Public Release
  - Description says the epic is no longer used and points to 739.
  - Should not remain open.

- `JVNAUTOSCI-739` Codebase transition (private to Von)
  - The repo IS "Von" now. Review whether any open children still matter.
  - If not, close or supersede so it stops competing for attention.

- `JVNAUTOSCI-933` Project admin/coordination *(already SUPERSEDED)*
  - Confirm fully closed in Jira.

### Coordination only

- `JVNAUTOSCI-934` Project admin/coordination
  - Keep as the umbrella for planning/review/admin tasks like this one, not as a feature-priority destination.

## Most important overlaps to clean up

### 1. Routing / tool-selection overlap (766 → now orphaned from closed 833)

- `JVNAUTOSCI-766` heavily overlapped with the now-closed `JVNAUTOSCI-833`.
- The present architecture direction should fold this into KB-authored routing metadata under `JVNAUTOSCI-936` or create a focused successor.
- **Action:** Supersede 766 by explicitly capturing its useful scope under 936.

### 2. Recommendation overlap (538, 144, 1714)

- `JVNAUTOSCI-538`, `JVNAUTOSCI-144`, and `JVNAUTOSCI-1714` overlap.
- With 1553 (Paper Recommender) now Done, 1714 is the natural follow-on.
- Suggested framing:
  - 538 = the broad capability
  - 144 = document/paper representation substrate
  - 1714 = active vertical-slice extension (reading flows, feedback, explanation)

### 3. UX overlap (537, 866, 1117, 1752)

- Four UX/UI epics, now including 1752 (type-specific instance renderers).
- 1752 is the most concrete and KB-aligned of the four. It defines what needs to exist on concept pages — type-specific summary renderers backed by Vontology applicability.
- Suggested framing:
  - 1752 = the actionable KB-driven presentation epic (advance now)
  - 866 = long-term KB-defined UI vision (keep as north star)
  - 537 = concrete frontend execution work (advance where it serves 1752 or other active work)
  - 1117 = likely unnecessary as a separate open epic; fold into 537

### 4. Safety / governance overlap (857, 539)

- Closely related. Keep both only if 857 stays focused on operational hardening and 539 on permission/governance semantics. Otherwise, use links and comments to prevent duplicate work.

### 5. Interaction surface overlap (885, 1460, 977, 1667)

- Four epics that expand how users interact with Von (multimodal, external channels, VS Code, devices).
- None address the core intent-execution problem. All should be deferred until the execution substrate is strong.
- Caution against fragmenting effort across multiple new surfaces before the core is reliable.

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

## Concrete suggested next tasks (refreshed)

These are the work items to tackle next, in order, whether by reinterpreting existing tasks or by creating fresh ones.

1. **Context bundles/dossiers plus episodic/artefact completion**
   - Epic home: `JVNAUTOSCI-936` and `JVNAUTOSCI-932`
   - Finish the bounded, provenance-rich context substrate Von needs before acting.
   - This was tranche 2 in the initial review; promoted to first now that 833/964 are closed.

2. **Paper recommendation extension and researcher reading flows**
   - Epic home: `JVNAUTOSCI-1714` / `JVNAUTOSCI-538` / `JVNAUTOSCI-144`
   - Build on the completed 1553 vertical slice with workflow-first reading flows, explanation, and feedback.

3. **Observability SLIs and selector regression benchmarks**
   - Epic home: `JVNAUTOSCI-536`
   - Establish intent-completion dashboards, dispatch-latency fixes (1429), and repeatable selector benchmarks.

4. **KB-driven presentation and type-specific instance renderers**
   - Epic home: `JVNAUTOSCI-1752` / `JVNAUTOSCI-866` / `JVNAUTOSCI-537`
   - Build type-specific summary renderers for concept pages (papers, people, tasks, events, rooms) driven by Vontology applicability.
   - Build inspectable views of what Von knows and why it believes it.

5. **Admin/auth/namespace hardening**
   - Epic home: `JVNAUTOSCI-857` with `JVNAUTOSCI-539`
   - Prioritise fail-closed pathways and remove silent privileged gaps.

## Merge guidance from JVNAUTOSCI-1655

JVNAUTOSCI-1655 surfaced several useful concrete refinements. The strongest ideas have been integrated into this refreshed review:

### Integrated into this review

- Selector evaluation benchmark — captured as an explicit deliverable in tranche 3.
- JVNAUTOSCI-1429 dispatch-latency — pulled forward in tranche 3.
- KB aggregation and presentation gap — stated directly in tranche 4.
- Workflow generation from intent — noted as a gap; 833 is now closed so a successor home is needed.
- JVNAUTOSCI-857 triage — reflected in the hardening recommendations under tranche 5.

### Points rejected or reframed

- Do not keep JVNAUTOSCI-766 as a separate top-priority track. Reinterpret under 936.
- Do not over-separate measurement (536) from evaluation (now 964 is closed, 536 holds both).
- Do not pull memory-injection tasks ahead of dossier/context substrate work.
- Do not treat production hardening as mostly late work. Auth and fail-closed protections advance in parallel.

## Short conclusion

With the agentic-behaviour (833), evaluation (964), paper-recommender (1553), and workflow-studio (1586) epics now closed, Von has its foundational execution, measurement, and authoring infrastructure in place.

The next portfolio centre of gravity should be:

1. **KB substrate completion** — context bundles, episodic memory, dossiers (932, 936)
2. **Recommendation/document vertical extension** — build on paper-recommender with reading flows, explanation, feedback (1714, 538, 144)
3. **Observability as continuous discipline** — intent-completion SLIs, selector benchmarks, dispatch latency (536)
4. **KB-driven presentation** — type-specific instance renderers and inspectable, provenance-bearing views (1752, 866, 537)
5. **Fail-closed hardening** — auth, namespace, rate-limit protections (857, 539)

The biggest planning mistake to avoid is fragmenting effort across new interaction surfaces (VS Code extension, multimodal, external channels) or generic UX modernisation before Von can reliably complete, verify, and explain the intents it already supports.
