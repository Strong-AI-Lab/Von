# Thinking Card Live LLM Visibility Design

## Purpose

This note defines the user-facing contract for the active Thinking card during a live turn.

The central requirement is simple:

- When Von prepares an LLM request, the Thinking card should show that prepared input.
- When Von sends that request to a model, the Thinking card should show that the input has been sent.
- If the model call then waits, stalls, streams, falls back, or fails, the card should continue to explain that state in user-facing terms.

This is not primarily a developer-debugging requirement. It is a user-facing transparency requirement.

## Problem

Recent turns have shown a repeated failure mode:

- the system has enough information to know which model candidate is being used;
- the backend has already prepared bounded request telemetry;
- the live progress card still shows only heartbeat-style waiting text;
- stage diagnostics continue to say that LLM input/output was not recorded;
- the active "Copy diagnostics" payload is a locator rather than the useful live explanation the user expects.

That means the card is currently strongest exactly where it is least useful, and weakest exactly where the user most needs it.

## Product Intent

The Thinking card is the user's live answer-path explanation.

It should make Von feel like an intelligent collaborator whose current work is
legible, bounded, and interruptible. It should not feel like a spinner with
developer metadata attached.

The core user need is not "show me every internal event." It is:

- what is Von trying to accomplish for me now?
- what object, paper, person, meeting, conference, workflow, or artefact is it
  working on?
- what sources, memory, tools, workflows, or models is it using?
- what has already happened, what is happening now, and what will happen next?
- is Von waiting, stuck, falling back, asking for permission, or about to take
  an action that matters?
- can I trust the answer path enough to keep waiting, stop the turn, or inspect
  more detail?

The card should expose operational provenance and progress, not hidden
chain-of-thought. It should show bounded request previews, context summaries,
workflow/route choices, tool activity, evidence sources, model state, and
failure boundaries. It should not reveal secrets, raw unbounded prompts, or
private internal scratch reasoning.

## User Audiences And Modes

The card should support at least three presentation modes. These modes are not
just "small, medium, large" detail levels; they answer different user questions.

### Default Mode

Default mode is for a competent human user who wants to know whether Von is
doing useful work without learning Von internals.

This includes researchers using Von for paper tracking, literature triage,
conference planning, collaborator memory, diary work, task follow-up, and
general research-team support.

Default mode should show:

- the current user-facing objective;
- the main object or target being worked on;
- the selected route or workflow in plain language;
- whether Von is retrieving memory, searching externally, using tools, calling
  a model, finalising an answer, or waiting for permission;
- short evidence/source summaries when relevant;
- bounded prepared-input and context summaries when an LLM call is active;
- waiting, stall, fallback, and next-action status in plain language.

Default mode should not foreground:

- internal stage IDs;
- request IDs;
- raw JSON;
- provider-specific transport events;
- counters that are only meaningful to developers;
- "no recorded LLM input/output" unless that is the actual user-relevant
  problem.

### Expert Mode

Expert mode is for intelligent users who understand the domain and want more
inspectable provenance without dropping into developer debugging.

For an AI researcher, this may mean seeing the selected workflow, candidate
routes, model family, prompt programme identity, context sources, tool
affordances, and fallback rationale.

For a researcher tracking papers, this may mean seeing which represented papers,
authors, projects, arXiv records, Zotero-like records, or Vontology concepts
are being consulted, and whether a claim comes from represented memory,
retrieval, a live web/tool result, or an LLM synthesis.

For conference planning, this may mean seeing which deadlines, venue facts,
travel constraints, budgets, co-author preferences, or calendar artefacts are
being considered, and whether Von is merely drafting a plan or preparing an
action that would require approval.

Expert mode should show:

- selected workflow and candidate alternatives;
- context-source categories and important named objects;
- evidence/provenance summaries;
- tool plan and tool-result summaries;
- model and fallback rationale at a high level;
- confidence, uncertainty, and missing-information notes when available;
- what will require confirmation before any destructive or high-impact action.

Expert mode should still avoid unbounded raw payloads, secrets, and excessive
transport noise.

### Debugging Mode

Debugging mode is for developers, operators, and AI-system researchers
investigating failures.

It may show:

- exact stage IDs and runtime phase names;
- request IDs, sequence numbers, and timestamps;
- model/provider metadata and fallback counters;
- prompt/context lineage and bounded prompt previews;
- tool call IDs, diagnostic payloads, and schema validation results;
- liveness timers and stall classification internals;
- copy/export controls for a complete current diagnostic snapshot.

Debugging mode should be explicit. The default card should not become a debug
console merely because the debug data is available.

## Cold-Start Experience

The card must be useful before the user has learned Von's architecture and
before a turn has rich terminal diagnostics.

Cold-start usefulness means:

- the first visible state should explain that Von is preparing the turn, not
  merely "Thinking";
- if no server progress is visible yet, the card should distinguish client
  submission, waiting for live progress, and a real backend stall;
- as soon as the prompt is accepted, the card should present the best available
  intent/object summary, even if it later gets refined by workflow selection;
- if workflow discovery has not yet completed, the card should say what kind of
  route is being sought;
- if no workflow matches, the card should explain the fallback route rather
  than leaving the user with a failed discovery row;
- if an LLM request is prepared, the prepared input and context summary should
  become visible immediately in the appropriate mode;
- if an LLM request is sent, the card should preserve the same prepared-input
  preview and add model, sent time, waiting state, and next fallback status;
- if telemetry is missing, the card should say what is known and what is not
  known, rather than converting missing data into misleading stage text.

Cold-start implementation should be driven by a stable "progress view model"
for the card: a small, user-oriented representation of objective, object,
route, evidence/context, current activity, wait state, and available detail
modes. Raw telemetry can feed that model, but the card should not have to infer
the product meaning directly from low-level event rows.

## User-Facing Contract

The active Thinking card should answer these questions at every moment of a turn:

1. What is Von currently trying to do to answer my prompt?
2. Which workflow or route is it using?
3. If it is about to call an LLM, what input has it prepared?
4. If it has sent that input, when was it sent, to which model, and what is it waiting for?
5. If it changes model or strategy, why did it do that?
6. If it is using tools instead of, or before, another LLM call, what tool work is being attempted?
7. If it is stalled, what exact boundary is stalled?

## What A Useful Thinking Card Would Look Like

For a paper-tracking prompt such as "what papers of mine do you know about?", a
useful default card might say:

- "Looking for represented papers connected to you."
- "Route: answer from Vontology profile and publication memory."
- "Checking represented paper, author, and project relationships."
- "Prepared LLM request using represented paper summaries and recent
  conversation context."
- "Waiting for the model to turn the retrieved paper records into an answer."

Expert mode for the same turn might additionally show:

- named paper concepts or canonical identifiers being consulted;
- whether each item came from Vontology, retrieval, or a live external lookup;
- selected workflow and candidate alternatives;
- missing relation types or uncertain ownership links.

For a conference-planning prompt such as "help me plan what to do before the
ICML deadline", a useful default card might say:

- "Identifying the conference-planning objective and relevant deadlines."
- "Checking represented commitments, papers, and calendar-like context."
- "No booking, submission, or message will be sent without confirmation."
- "Preparing a planning response with open questions and next actions."

Expert mode might additionally show:

- deadline sources and confidence;
- which papers, collaborators, travel constraints, or tasks were considered;
- which proposed actions would be additive, reversible, or require approval.

For a prompt such as "tell me about the current user", a useful card would look something like this:

- "Understanding your prompt and gathering authenticated context."
- "Selected route: answer from authenticated user and organisation context."
- "Prepared LLM request for workflow dispatch."
- "Model: gemma4:26b."
- "Prompt prepared and sent 3 seconds ago."
- "Prepared input preview: answer the user's question using the current authenticated user context and organisation context; if the answer is uncertain, say what is missing."
- "Context included: current user context, current organisation context, recent conversation turns."
- "Waiting for the model to return its first output."

If the call remains silent for a while, the card should evolve into:

- "Still waiting for model output."
- "Prompt was sent 48 seconds ago."
- "No output has been received yet."
- "No tool calls have started."
- "If this continues, Von will try the next allowed model candidate."

If a fallback occurs, the card should say:

- "Primary model did not complete."
- "Trying fallback 2 of 3."
- "Fallback model: <model>."
- "Prepared input reused with the same context."

If a tool-calling stage is active, the card should say:

- "Planning tool calls needed to answer your prompt."
- "Prepared tool-calling LLM input sent."
- "Waiting for the model to decide which tool to call."

The card should remain answer-oriented. It should not reduce the experience to counters, opaque stage IDs, or transport-level heartbeat noise.

## Minimum Live Fields

The live card should have first-class fields for:

- current intent summary;
- current object or target summary;
- selected workflow or route;
- current stage label in user-facing language;
- evidence/source summary;
- model candidate identity;
- fallback attempt position;
- LLM input lifecycle state: prepared, sent, first output received, completed, failed;
- bounded prompt preview shown at prepare time;
- bounded context summary shown at prepare time;
- sent timestamp;
- elapsed waiting time since send;
- stall classification;
- latest output preview once output begins;
- next action if fallback or recovery is triggered.

## Layered Presentation

The card should separate user-facing explanation, expert provenance, and
developer detail.

Default user-facing layer:

- plain-language intent;
- working object or target;
- prepared input preview;
- workflow/route explanation;
- evidence/source summary;
- waiting or fallback explanation;
- concise output preview.

Expert layer:

- selected workflow and candidate route summaries;
- context-source categories and named objects;
- tool plan and result summaries;
- provenance, uncertainty, and missing-information notes;
- model/fallback rationale at a high level.

Developer/debug layer:

- exact stage IDs;
- provider and model metadata;
- fallback counters;
- call IDs;
- bounded context lineage;
- tool and routing diagnostics.

The user-facing layer should be visible by default. Expert and debug layers can
remain collapsible or mode-gated.

## Event Contract

The live progress surface should expose explicit events for:

- LLM input prepared;
- LLM input sent;
- first output received;
- output streaming update;
- LLM call completed;
- LLM call failed;
- fallback candidate selected;
- fallback input prepared;
- fallback input sent;
- stall detected while waiting for output.

The prepared-input event should include a bounded request preview immediately.
The sent event should preserve that same preview and add model/provider/timestamp.
The card should not wait for post-hoc persistence before showing either state.

The runtime should also preserve enough structured data to build the progress
view model:

- user-facing objective summary;
- working object/target summary;
- route/workflow summary;
- evidence and context-source summary;
- current activity and wait-state summary;
- available expert/debug detail links;
- uncertainty, missing-information, and confirmation-requirement notes.

These fields may be produced by workflow, prompt, model, telemetry, or other
authoritative surfaces, but they should be carried as explicit progress state
rather than repeatedly reconstructed in frontend string heuristics.

## Copy / Export Contract

The active "Copy diagnostics" action should not silently downgrade to a thin locator when the user reasonably expects a live explanation.

If a locator is copied, it should be clearly labelled as a locator.
If the user asks for current diagnostics, the copy should include the current user-facing state and the current bounded prepared/sent LLM input when available.

## Non-Goals

This design does not require:

- exposing secrets or raw unsafe payloads;
- dumping full prompts with unlimited context;
- replacing the post-turn `LLM i` surface;
- collapsing workflow and tool telemetry into a single opaque blob;
- showing hidden chain-of-thought or private model scratch work;
- requiring ordinary users to understand workflow IDs or event schemas before
  they can decide whether Von is making progress.

## Acceptance Shape

The design should be considered met when:

- the instant an LLM request is prepared, the card can show a bounded prepared-input preview;
- the instant it is sent, the card shows that it was sent, to which model, and when;
- a hanging model call no longer appears indistinguishable from "no LLM input was ever sent";
- fallback attempts are legible to the user;
- tool-planning turns show prepared tool-calling input before the tool decision returns;
- the live copy/export surface is explicit about whether it is a locator or a current diagnostic snapshot;
- default mode is useful for paper-tracking, conference-planning, and ordinary
  research-support turns without opening diagnostics;
- expert mode exposes provenance, selected workflow, named objects, evidence
  sources, and uncertainty without becoming a raw debug dump;
- debugging mode still exposes the full bounded telemetry needed to diagnose
  stalls, routing failures, prompt/context loss, and copy/export problems;
- cold-start turns show a meaningful objective/object/wait state before terminal
  diagnostics exist;
- missing telemetry is represented as missing telemetry, not as misleading
  user-facing progress.

## Related Incidents

This contract is motivated by repeated failures, not a single isolated turn. Relevant evidence includes:

- request `ca0f7e20-93a8-43dc-a7c1-53fe33263a67`;
- request `382d7d0f-69e5-4a53-9d1c-528bc15bf10a`;
- request `88df98d5-834c-4a97-8c50-e2712bd7ffda`;
- the earlier pre-dispatch gap analysed in `JVNAUTOSCI-1429`;
- the later stage-telemetry-loss analysis recorded in `JVNAUTOSCI-1859` and `JVNAUTOSCI-1866`.

## Implementation Reminder

The key architectural point is that the live card should consume the same prepared request data that the orchestrator already has in hand before dispatch.

Do not wait for terminal persistence to make the active card useful.
