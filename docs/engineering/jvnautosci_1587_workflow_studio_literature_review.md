# JVNAUTOSCI-1587 Workflow Studio Literature Review and Visual Design Brief

Date: 2026-03-27
Task: `JVNAUTOSCI-1587`
Parent epic: `JVNAUTOSCI-1586`
Status: Finalised design brief for implementation planning

## 1. Purpose

This note records a short literature-informed design brief for a new Workflow
Studio inside Von: an independent workflow-focused program/surface for
discovering, listing, inspecting, comparing, tracing, and eventually editing
authoritative VWL/Vontology workflows.

This is not a repeat of the completed renderer-aligned workflow visualisation
work (`JVNAUTOSCI-1148`). That earlier work added workflow-related rendering in
existing task-centric UI paths. The Workflow Studio is a separate workflow-first
surface with its own browse/search/navigation model and its own requirements
around overview, drill-down, operational overlays, and mixed-initiative editing.

## 2. Current Von Substrate

The current implementation already provides a strong read-side substrate:

- workflow listing and definition APIs in `src/backend/server/routes/workflows_routes.py`
- workflow summary assembly in `src/backend/workflows/workflow_listing_service.py`
- workflow management/introspection tool surfacing in
  `docs/engineering/workflow_mcp_capability_matrix.md`
- a lightweight existing workflow monitor in
  `src/frontend/web/von_interface/templates/chat_tab.html`
- a reusable diagram contract baseline in Appendix A of
  `docs/engineering/von_workflow_language_manual.md`

Session observations taken on 2026-03-27:

- `workflow_list_definitions(limit=5)` succeeded and reported `48` workflows.
- The same payload reported:
  - `48` registry workflows
  - `48` Vontology-discovered workflows
  - `48` graph-complete workflows
  - `0` authority-missing workflows
  - `30` `retrieval_ready` workflow descriptions
  - `18` `stub` descriptions
  - `16` under-specified workflow descriptions
- `workflow_mcp_health_check(include_introspection=true)` succeeded for:
  - `workflow_list_definitions`
  - `workflow_list_instances`
  - `workflow_list_event_bindings`
  - `workflow_list_schedules`
  - `settings_get_public`
  - `chat_introspect`

Recent session history also matters. On 2026-03-26, the same health check
showed a `workflow_list_instances` timeout while definition/binding/schedule
reads still worked. So the correct design conclusion is not "the operational
surface is broken", but "operational surface health varies enough that the
Workflow Studio must treat degraded and partial states as first-class UX".

This current state supports a staged plan:

1. build the studio on top of existing authoritative workflow data,
2. keep diagrams and operational summaries derived,
3. make partial availability explicit,
4. postpone write-capable editing until preview, validation, and authority
   contracts are designed.

## 3. Literature Review

### 3.1 Process-model size and decomposition

The strongest repeated result in the BPM literature is that size and control-flow
complexity harm comprehension. Mendling, Reijers, and van der Aalst's
seven-process-model-guidelines work argues for keeping models as small as
possible, using structured modelling, and decomposing once models become too
large or too branch-heavy. A useful working threshold from this line of work is
that models above roughly fifty elements should not be presented as the default
single view.

The earlier "What makes process models understandable?" study reinforces the
same direction: size, density, label quality, structuredness, and reader
experience all materially affect comprehension. The practical implication for Von
is that the Workflow Studio should not optimise for "show me the whole graph";
it should optimise for "help me answer my current workflow question with the
minimum necessary visual load".

### 3.2 Layout, attention, and flow consistency

Recent work strengthens the case that layout is not cosmetic. Burattin et al.
showed that flow consistency can be measured and treated as a real model-quality
signal. Figl et al. (2024) pushed this further with eye-tracking evidence:
consistent flow direction and visible control-flow patterns help users know where
to look next and make it easier to detect sequential, parallel, and exclusive
blocks.

This matters directly for the Workflow Studio. The diagram engine should not
allow arbitrary free-form layouts as the primary view. The default layout should
be stable, directional, and optimised for control-flow interpretation. Layout
determinism is part of comprehension and diffability.

### 3.3 Hierarchy helps, but hidden detail can hurt

There is no simple "collapsed sub-processes are always good" conclusion. The
literature is more nuanced.

- Hierarchy and modularisation help when the alternative is an overwhelming flat
  model.
- But collapsed sub-processes can hide information that is necessary for the
  user's task, forcing mentally expensive context-switching.
- Turetken et al. (2020) found that modularisation changes understandability in
  task-dependent ways, and that users often benefit when expanded detail can be
  consulted directly rather than inferred from collapsed placeholders alone.

The correct design move for Von is therefore not "always collapse" or "always
flatten". The studio should use overview-plus-detail and semantic zoom:

- default to a manageable overview,
- allow drill-in,
- preserve breadcrumbs and local context,
- provide lightweight inline previews for collapsed structures,
- avoid sending users to a completely separate context just to inspect one hidden
  branch.

### 3.4 Visual notation quality matters independently of workflow semantics

Moody's Physics of Notations remains the best concise design theory for the
visual side of the problem. The most relevant principles here are:

- semantic transparency
- perceptual discriminability
- complexity management
- cognitive integration
- dual coding
- graphic economy

For the Workflow Studio this means:

- different node/edge types should look meaningfully different
- colour should not be the only channel
- labels must carry semantic weight, not just IDs
- users should be able to integrate views without memorising unstable visual
  conventions
- the notation should remain visually economical instead of encoding too many
  categories at once

### 3.5 Interaction is part of comprehension

The literature on process visualisation and broader visual analytics points in
the same direction: for long, branching, or operationally rich workflows, a
static single canvas is not enough. Brath et al. (2024) describe practical
lessons from real-world journey analytics deployments that map well onto
workflow inspection:

- aggregation and grouping are necessary
- filtering and focus are necessary
- multiple coordinated views are better than one overloaded view
- operational overlays and narrative summaries help users interpret complex
  event-driven structures

This aligns with the existing Von substrate, where the real workflow story is
already split across topology, schedules, bindings, traces, and health signals.
The Workflow Studio should treat multi-view coordination as the baseline model,
not as a later enhancement.

### 3.6 LLM assistance should be mixed-initiative, not opaque

The recent LLM + visual analytics literature is useful here, but it does not
justify direct AI-owned editing.

LEVA (2024) and Li et al. (2024) suggest a better pattern:

- let the LLM help with explanation, onboarding, summarisation, search intent,
  and candidate action formulation
- keep the visual state explicit and inspectable
- keep the user's selection/context explicit
- make proposed operations reviewable before execution

In other words, the LLM should operate as interpreter, coordinator, and
suggestion engine around the workflow view, not as a silent direct mutator of
the authoritative workflow graph.

## 4. What the Literature Supports Strongly

These conclusions are strongly supported by multiple sources and should be
treated as design constraints rather than optional style choices.

### 4.1 The studio needs multiple coordinated views

One workflow diagram cannot carry all of these simultaneously without becoming
hard to read:

- topology
- control-flow logic
- dataflow/context mapping
- schedules
- event bindings
- durable runtime state
- execution traces
- task/Jira relationships
- health/parity/authority diagnostics

The Workflow Studio should therefore expose a coordinated view family rather than
one "master" diagram.

### 4.2 The default view should be cognitively light

The default view should answer "what is this workflow?" quickly. It should not
start by showing every context key, every trace event, and every operational
overlay.

Recommended default:

- workflow catalogue on the left
- topology view as the primary central view
- metadata/authority panel on the right
- optional operational overlays layered in rather than baked into the default

### 4.3 Layout must be stable and directional

For the default topology view:

- use a stable left-to-right or top-to-bottom flow direction
- minimise unnecessary directional reversals
- expose parallel/exclusive patterns clearly
- keep deterministic layout ordering for diffability and repeatability

### 4.4 Hierarchy must preserve context

The studio should support hierarchy, but not by hiding everything behind opaque
"plus" boxes. Users should be able to move between overview and detail while
preserving:

- location in the parent workflow
- breadcrumb context
- inbound/outbound transition meaning
- key metadata about the collapsed region

### 4.5 Search, filtering, and explanation are first-class

Users will ask different questions:

- Which workflows exist?
- Which ones are healthy?
- Which ones are background-triggered?
- Why did this workflow run?
- What data keys does this step write?
- What changed between two workflows?

Those are not all diagram problems. The studio must combine search, filtering,
and explanatory summaries with diagrammatic views.

## 5. What Remains Design Judgement

The literature does not settle these questions fully; they require local design
judgement.

### 5.1 Whether the studio lives inside the main Von shell or as a more isolated app

The literature supports independence of task context, but it does not dictate
whether the program should be:

- a separate route in the main web app,
- a more isolated expert-mode page,
- or a dedicated workspace shell.

My current recommendation is a separate route inside the existing web app so it
can reuse auth, navigation, and styling infrastructure without being trapped
inside chat-specific UI assumptions.

### 5.2 Which diagram library to use

The literature supports stable directional layouts and semantic differentiation,
not a specific library. The decision between SVG-based custom rendering,
Graphviz-derived layout, ELK, or a React flow canvas should be made in
`JVNAUTOSCI-1588` and `JVNAUTOSCI-1590` against Von-specific needs:

- deterministic layout
- large-graph performance
- semantic zoom
- overlays
- diffability
- edit preview support

### 5.3 Whether topology or task-oriented narrative should dominate the first render

The evidence favours cognitively light defaults, but the exact first render
could be:

- workflow summary card + simplified topology
- topology first
- or task-oriented "why/when/how" summary first

My current recommendation is topology-first with a short generated/curated
summary panel beside it.

## 6. Workflow Studio Design Brief for Von

### 6.1 Authoritative vs derived artefacts

The Workflow Studio must keep the following distinction rigid:

- authoritative:
  - Vontology workflow concepts and relations
  - workflow prompt/routing/publication metadata
  - generic workflow-authoring/runtime surfaces
- derived:
  - diagram payloads
  - layout coordinates
  - overlay summaries
  - filtered catalogue projections
  - comparison summaries

The studio must never make repo-side workflow files the effective source of
truth.

### 6.2 Recommended initial task taxonomy

The studio should explicitly support these user tasks:

- discover: find workflows by name, role, executability, quality, trigger type
- inspect: understand one workflow's purpose and structure
- compare: compare two workflows or two variants
- trace: connect a workflow definition to schedules, bindings, runs, and traces
- debug: find why a workflow is missing, unhealthy, incomplete, or stale
- edit-preview: formulate and preview a change before any mutation happens

### 6.3 Recommended initial view family

Phase-1 read-only view family:

- Catalogue view
  - searchable workflow list
  - badges for description quality, authority, background launch, health
- Topology view
  - steps and transitions only
  - stable directional layout
- Metadata view
  - workflow description, source, definition identity, authority/parity signals
- Decision view
  - conditional transitions, priority order, failure branches
- Dataflow view
  - context keys, tool param mappings, output-field writes
- Operational view
  - schedules, event bindings, recent runs, health diagnostics

Phase-2 additions:

- comparison view
- execution trace playback
- route-map / typed-subworkflow view
- task/Jira linkage view

### 6.4 Recommended default page layout

- left rail: catalogue, search, filters
- centre: selected diagram view
- right rail: details, summary, diagnostics, related artefacts
- top bar: view selector, scope filters, compare mode, assistant entrypoint

This layout matches the literature's support for overview+detail and coordinated
views, while keeping the diagram central.

### 6.5 Recommended treatment of hierarchy

- default to one workflow level at a time
- show collapsed sub-workflows as drillable nodes
- expose a lightweight preview on hover/select
- keep breadcrumbs and parent-context strips visible
- offer an "expand locally" mode for small subgraphs rather than forcing full
  navigation away

### 6.6 Recommended treatment of operational overlays

Operational overlays should be optional layers on top of a stable structural
view, not replacements for it.

Examples:

- schedule badges and cadence hints
- event-binding counts and warnings
- recent run status strip
- trace availability
- health-check or parity warnings

If an overlay is unavailable:

- keep the structural view usable
- show the missing overlay state explicitly
- preserve the last-known timestamp if available
- avoid empty panels that look like "no data exists"

### 6.7 Recommended role of the assistant

The assistant should help users:

- find the right workflow
- explain what a workflow or step does
- summarise a large workflow in plain language
- answer questions about branches, dependencies, and written context keys
- propose edits in explicit previewable form

The assistant should not:

- silently edit the workflow graph
- become the primary source of truth about workflow structure
- hide the concrete proposed mutation behind prose only
- bypass validation or authority checks

## 7. Do / Do Not List

### Do

- keep the first screen simple
- keep diagrams derived from authoritative workflow data
- use stable directional layouts
- make hierarchy drillable but contextual
- separate structure from operational overlays
- show authority/health/degraded status explicitly
- use the assistant for explanation and edit proposal generation
- require preview and validation before any write

### Do not

- start with one giant "everything" graph
- rely on colour alone for semantics
- collapse important detail without local preview or breadcrumb context
- mix layout artefacts with source-of-truth workflow data
- make operational overlay failure look like workflow absence
- let the assistant mutate authoritative workflow state opaquely
- encode workflow-authority logic in repo-side files for the editor path

## 8. Initial Evaluation Rubric

Later implementation tasks should measure at least:

- discovery efficiency
  - can users find the intended workflow quickly?
- comprehension
  - can users identify purpose, start step, major branches, and key outputs?
- navigational stability
  - can users move between overview and detail without losing context?
- operational debuggability
  - can users tell whether a problem is structural, operational, or authority-related?
- trust and provenance
  - can users see what is authoritative vs derived?
- editing safety
  - can users understand proposed edits before they are applied?

## 9. Immediate Implications for Follow-on Tasks

### `JVNAUTOSCI-1588`

Must define:

- the independent route/program boundary
- the authority contract
- the diagram-family contract
- degraded overlay semantics

### `JVNAUTOSCI-1589`

Should implement generic read-model transforms for:

- topology
- decision logic
- dataflow
- operational overlays

It should not hard-code one studio-only special-case payload if the same read
surfaces are reusable elsewhere.

### `JVNAUTOSCI-1590`

Should build the catalogue + topology + metadata baseline first, not jump
straight to editing or full trace playback.

### `JVNAUTOSCI-1591`

Should treat operational overlays as optional but well-signposted layers.
Recent session variability in health checks is enough to justify this even when
the current check is green.

### `JVNAUTOSCI-1592` and `JVNAUTOSCI-1593`

Should use a mixed-initiative pattern:

- user selects context
- assistant proposes bounded change
- studio shows explicit patch/preview
- validation runs
- authoritative apply happens only after explicit confirmation

## 10. References

1. Jan Mendling, Hajo A. Reijers, Wil M. P. van der Aalst. "Seven process
   modeling guidelines (7PMG)." 2010.
   URL: https://www.vdaalst.com/publications/p574.pdf
2. Jan Mendling, Hajo A. Reijers, Jorge Cardoso. "What makes process models
   understandable?" 2007.
   URL: https://jorge-cardoso.github.io/rd/Papers/CP-2007-040-BPM-What-makes-process-models-understandable.pdf
3. Daniel L. Moody. "The Physics of Notations: Toward a Scientific Basis for
   Constructing Visual Notations in Software Engineering." 2009.
   Overview/tutorial URL: https://business.uq.edu.au/sites/default/files/events/files/daniel_moody_paper.pdf
4. Andrea Burattin et al. "Detection and Quantification of Flow Consistency in
   Business Process Models." 2016.
   URL: https://arxiv.org/abs/1602.02992
5. Kathrin Figl et al. "Guiding attention in flow-based conceptual models
   through consistent flow and pattern visibility." 2024.
   URL: https://www.sciencedirect.com/science/article/pii/S0167923624001258
6. Oktay Turetken et al. "The Influence of Using Collapsed Sub-processes and
   Groups on the Understandability of Business Process Models." 2020.
   URL: https://link.springer.com/article/10.1007/s12599-019-00577-4
7. Hajo A. Reijers et al. "Assessing the Impact of Hierarchy on Model
   Understandability." 2011.
   URL: https://hreijers.win.tue.nl/H.A.%20Reijers%20Bestanden/eessmod_2011.pdf
8. Richard Brath et al. "Visual Journey Analytics: lessons learned from
   real-world implementations." 2024.
   URL: https://diglib.eg.org/bitstream/handle/10.2312/vipra20241105/05_vipra20241105.pdf
9. Jian Zhao et al. "LEVA: Using Large Language Models to Enhance Visual
   Analytics." 2024 workshop paper.
   URL: https://arxiv.org/abs/2403.05816
10. Tong Li et al. "A Preliminary Roadmap for LLMs as Assistants in Exploring,
    Analyzing, and Visualizing Knowledge Graphs." 2024.
    URL: https://ieeevis.b-cdn.net/vis_2024/pdfs/w-nlviz-1021.pdf
