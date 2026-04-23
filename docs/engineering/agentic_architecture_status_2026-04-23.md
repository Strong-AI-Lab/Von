# Agentic Architecture Status - 23 April 2026

## Purpose

This note records the current engineering status of the Von codebase against the
design intent described in `docs/engineering/Von_for_AgenticAI.md`, using a
bounded static review of the live code on 23 April 2026, refreshed later the
same day after the landings through `JVNAUTOSCI-1988` and `JVNAUTOSCI-1987`.

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
buttonify, turn-contract prose, or broad tool-metadata seams. Those were
materially reduced first by the April 21 landings through `JVNAUTOSCI-1972`,
and then again by the 23 April landings of `JVNAUTOSCI-1974`,
`JVNAUTOSCI-1975`, `JVNAUTOSCI-1985`, `JVNAUTOSCI-1973`, and
`JVNAUTOSCI-1986`, `JVNAUTOSCI-1988`, and `JVNAUTOSCI-1987`.

The current shortfall is now concentrated in four areas:

1. Broader enduring-memory and memory-promotion architecture still remains more
   programme direction than integrated operational substrate.
2. The episode-evaluation loop now has represented proposal and promotion
   workflows, but broader isolated self-improvement and benchmark-world support
   is still only partial rather than a general experimentation substrate.
3. Authority-backed critic/evaluator expansion is still narrower than the
   design target for self-correction and policy learning.
4. The next review still needs to be repeated after the tranche lands, because
   the highest-value remaining gaps have shifted materially several times in
   one day.

## Current Code Reality

As measured on 23 April 2026 after the `JVNAUTOSCI-1987` landing:

- `src/backend/integrations/internal_mcp/orchestrator.py` is `35095` lines
- `src/backend/integrations/internal_mcp/catalogue.py` is `27480` lines
- `src/backend/server/routes/von_routes.py` is `13351` lines
- `src/backend/services/turn_execution_record_service.py` is `6916` lines
- `src/backend/server/utils_flask.py` is `3036` lines

Large files are not the whole problem, but they remain the strongest pressure
surface for hidden policy drift and inline patching.

## Priority Ordering

### Current Tranche Status

The following immediate architecture tasks from the earlier same-day review are
now landed:

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
   live routing path; discovery now stays on the capability index plus the
   existing semantic/Vontology surfaces, and registry-backed executability
   checks reuse the live registry instead of rebuilding a separate read-only
   registry on the hot path.

5. `JVNAUTOSCI-1986`
   Rumination relation candidate ranking and auto-apply semantics now resolve
   from the linked knowledge-acquisition profile, and invalid/missing profile
   payloads fail closed instead of falling back to English keyword/class
   heuristics in Python.

6. `JVNAUTOSCI-1988`
   Main-turn execution now consumes represented turn-memory substrate directly:
   context-bundle/dossier/workspace state is injected into shared turn context,
   selected-workflow policy-memory is surfaced on the direct-response and tool
   paths, request-level memory attachments fail closed when unavailable, and
   the added context is visible in lineage/aux telemetry rather than hidden
   stage-local prompt shaping.

7. `JVNAUTOSCI-1987`
   Episode evaluation now launches represented workflow-improvement proposal
   workflows from critique memory, stores candidate revisions as reviewable
   workflow-authoring proposals with proposal context, launches represented
   promotion evaluation workflows, records explicit promotion
   recommendations back onto proposal lifecycle plus critique memory, and no
   longer relies on Python fallback suggestion synthesis on the live path.

### P0 Remaining Architecture Work

The previously identified immediate P0 tranche is now landed, and the
previously identified `1987` P1 close-out is also landed. The next sharpest
work is no longer one more missing live-path repair; it is the bounded
repeat-review plus the broader architecture umbrellas that still remain.

### P1 Next Tranche

These should follow immediately after the P0 tranche, not be deferred
indefinitely behind unrelated feature work.

1. `JVNAUTOSCI-1989`
   Repeat the AGI-alignment architecture review after the current P0/P1 tranche
   and refresh status notes, task ordering, and stale issue wording.

2. `JVNAUTOSCI-1962`
   Enduring-memory and memory-promotion architecture umbrella.

3. `JVNAUTOSCI-1963`
   Isolated self-improvement loops and benchmark worlds umbrella.

4. `JVNAUTOSCI-1964`
   Authority-backed critic/evaluator workflow expansion umbrella.

Why this is P1:

- the represented self-improvement loop is now materially closed enough that
  the next rational move is to repeat the review rather than continue steering
  from an already stale pre-`1987` ordering note;
- `1962` is now the sharpest substantive architecture umbrella because memory
  substrate and promotion architecture still remain more fragmented than the
  design note intends; and
- `1963` and `1964` still matter, but they now sit downstream of a clean
  post-`1987` reassessment rather than behind another missing narrow repair.

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

This same-day refresh now incorporates the landings of `1974`, `1975`, `1985`,
`1973`, `1986`, `1988`, and `1987`, but it does not replace the fuller
repeat-review task tracked in `JVNAUTOSCI-1989`.

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
