# Agentic Architecture Status - 23 April 2026

## Purpose

This note records the current engineering status of the Von codebase against the
design intent described in `docs/engineering/Von_for_AgenticAI.md`, using a
bounded static review of the live code on 23 April 2026.

It is not a replacement for `Von_for_AgenticAI.md`. That document remains the
long-horizon design note. This document is the current engineering-status and
task-ordering companion that answers a narrower question:

- where the live code still falls materially short of the intended AGI-shaping
  architecture;
- which gaps are immediate engineering priorities versus medium-term platform
  work; and
- which Jira tasks now carry those gaps.

## Current Headline

The sharpest remaining problems are no longer the older write-policy,
buttonify, or turn-contract prose seams. Those were materially reduced by the
April 21 landings through `JVNAUTOSCI-1972`.

The current shortfall is now concentrated in five areas:

1. Python still owns too much live capability/control metadata and recovery
   logic.
2. Rumination still decides relation priority and auto-apply behaviour through
   code-side heuristics.
3. Self-improvement is still mostly remediation-routing rather than a closed
   represented promotion loop.
4. Explicit memory strata exist, but are still mostly adjacent tooling rather
   than the default main-turn cognitive substrate.
5. The next review must be repeated after the current tranche lands, because
   the highest-value remaining gaps are moving quickly.

## Current Code Reality

As measured on 23 April 2026:

- `src/backend/integrations/internal_mcp/orchestrator.py` is `37459` lines
- `src/backend/integrations/internal_mcp/catalogue.py` is `30083` lines
- `src/backend/server/routes/von_routes.py` is `14892` lines
- `src/backend/services/turn_execution_record_service.py` is `7671` lines
- `src/backend/server/utils_flask.py` is `3392` lines

Large files are not the whole problem, but they remain the strongest pressure
surface for hidden policy drift and inline patching.

## Priority Ordering

### P0 Immediate Architecture Work

This is the current highest-priority implementation tranche.

1. `JVNAUTOSCI-1974`
   Remove deterministic missing-tool-call retry payload synthesis and keep retry
   authority in structured contracts or authored retry workflows.

2. `JVNAUTOSCI-1975`
   Replace hard-coded dispatch surface-family inference with represented tool
   metadata in turn-contract preflight.

3. `JVNAUTOSCI-1973`
   Remove workflow-discovery English keyword/name fallback from routing
   eligibility and move residual matching onto represented discovery metadata.

4. `JVNAUTOSCI-1985`
   Replace remaining Python-owned tool capability/write/evidence authority with
   represented tool metadata across catalogue, registry, and turn records.

5. `JVNAUTOSCI-1986`
   Move rumination relation priority and auto-apply policy out of Python
   keyword/threshold heuristics and onto represented authority.

Why this is P0:

- these are still live code-side policy seams on the main cognition/control or
  long-horizon knowledge-growth paths;
- they are more immediate than broader platform ambitions because they still
  let Python decide behaviour that should be represented and revisable; and
- they are the highest-leverage precondition work before Von can safely push
  memory, self-improvement, and multi-agent ambitions much further.

### P1 Next Tranche

These should follow immediately after the P0 tranche, not be deferred
indefinitely behind unrelated feature work.

1. `JVNAUTOSCI-1987`
   Close the episode-evaluation self-improvement loop with represented
   improvement and promotion workflows instead of Python fallback suggestions
   and remediation-only routing.

2. `JVNAUTOSCI-1988`
   Integrate context-bundle, dossier, and policy-memory surfaces into the main
   conversation-turn path instead of leaving them as mostly adjacent tooling.

3. `JVNAUTOSCI-1989`
   Repeat the AGI-alignment architecture review after the current P0/P1 tranche
   and refresh status notes, task ordering, and stale issue wording.

4. `JVNAUTOSCI-1962`
   Enduring-memory and memory-promotion architecture umbrella.

5. `JVNAUTOSCI-1963`
   Isolated self-improvement loops and benchmark worlds umbrella.

6. `JVNAUTOSCI-1964`
   Authority-backed critic/evaluator workflow expansion umbrella.

Why this is P1:

- the memory and self-improvement gaps are central to the Von vision, but they
  are not best served by building on top of still-live P0 Python authority
  seams;
- the required substrates partly exist already, so the next need is not only
  design but mainline integration and promotion-loop closure; and
- `1989` is included here deliberately so the review cadence becomes explicit
  rather than optional.

### P2 Longer-Horizon Platform Work

These remain important, but they are not the sharpest next engineering moves on
the current code.

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

- these are still real gaps, but they are not blocked mainly by one missing
  code patch;
- most of them are programme-shaping architecture lines that should proceed
  after or alongside cleaner authority and memory substrates; and
- `1120` is worthwhile but not one of the sharpest remaining AGI-alignment
  failures in the current backend.

## Existing Tasks Not Duplicated

Some older tasks remain useful as umbrellas or adjacent work, but were not
reopened or duplicated here:

- `JVNAUTOSCI-1913` remains the main anti-drift / monolith-pressure umbrella.
- `JVNAUTOSCI-1960` remains the broader minimal-imposition agentic programme
  epic.
- `JVNAUTOSCI-1969` remains a worthwhile MCP contract/diagnostics audit, but it
  is not the same as removing the remaining live capability-authority seams.
- `JVNAUTOSCI-768` and `JVNAUTOSCI-770` still point in the represented tool
  metadata direction, but their wording is materially stale and should not be
  treated as the current implementation guide without reinterpretation.

## Required Review Cadence

This review must be repeated. It should not remain a one-off note.

After completion of any P0 or P1 task from this tranche:

1. rerun a bounded architecture scan against the then-current code;
2. re-measure the main monolith sizes and re-check the strongest remaining live
   Python authority seams;
3. update this note and any now-stale sequencing companion notes;
4. revise or close Jira tasks whose wording no longer matches the current code
   reality; and
5. only then decide the next highest-priority tranche.

The repeat-review requirement is tracked explicitly in `JVNAUTOSCI-1989`.

## Expected Doc Revisions After the Next Tranche

When the next review is run, the following documents should be checked and
updated if their ordering or claims have become stale:

- `docs/engineering/Von_for_AgenticAI.md`
- `docs/engineering/recent_architecture_progress_2026-04-20.md`
- this dated status note

The key rule is simple:

> do not keep steering work from old architecture notes once the live code has
> moved and the highest-value remaining gaps have changed.
