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
- selected workflow or route;
- current stage label in user-facing language;
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

## Two-Layer Presentation

The card should separate user-facing explanation from developer detail.

User-facing layer:

- plain-language intent;
- prepared input preview;
- workflow/route explanation;
- waiting or fallback explanation;
- concise output preview.

Developer layer:

- exact stage IDs;
- provider and model metadata;
- fallback counters;
- call IDs;
- bounded context lineage;
- tool and routing diagnostics.

The user-facing layer should be visible by default. The developer layer can remain collapsible.

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

## Copy / Export Contract

The active "Copy diagnostics" action should not silently downgrade to a thin locator when the user reasonably expects a live explanation.

If a locator is copied, it should be clearly labelled as a locator.
If the user asks for current diagnostics, the copy should include the current user-facing state and the current bounded prepared/sent LLM input when available.

## Non-Goals

This design does not require:

- exposing secrets or raw unsafe payloads;
- dumping full prompts with unlimited context;
- replacing the post-turn `LLM i` surface;
- collapsing workflow and tool telemetry into a single opaque blob.

## Acceptance Shape

The design should be considered met when:

- the instant an LLM request is prepared, the card can show a bounded prepared-input preview;
- the instant it is sent, the card shows that it was sent, to which model, and when;
- a hanging model call no longer appears indistinguishable from "no LLM input was ever sent";
- fallback attempts are legible to the user;
- tool-planning turns show prepared tool-calling input before the tool decision returns;
- the live copy/export surface is explicit about whether it is a locator or a current diagnostic snapshot.

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
