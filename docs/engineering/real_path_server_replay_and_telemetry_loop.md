# Real-Path Server Replay and Telemetry Loop

**Status**: Living practical guidance  
**Date**: 2026-04-17

## 1. Purpose

This note describes a repeatable engineering loop for fixing user-visible Von
behaviour on the real server path, rather than stopping at nearby unit tests or
synthetic harnesses.

Use it when the claim you need to validate is something like:

- "Von responds to this text input correctly"
- "the turn is genuinely workflow-driven"
- "the selector, dispatch, and answer path are aligned"
- "telemetry matches what the user actually experienced"

The loop is deliberately iterative. You replay the real prompt against the real
server surface, inspect the exact turn telemetry, fix the real failure
boundary, and replay again until the behaviour is both user-correct and
telemetry-consistent.

## 2. When to Use It

Use this process when:

- the bug is user-visible or answer-visible;
- the route, orchestrator, workflow, selector, or response path may be at
  fault;
- a unit test passes but the real system still behaves incorrectly;
- you need confidence that a behaviour is workflow-driven rather than merely
  test-driven;
- the failure might be caused by the wrong candidate set, wrong selector
  steering, wrong execution mode, or weak answer composition.

Do not treat `von_chat_run` or a direct helper call as sufficient acceptance
evidence unless the user-facing bug is specifically in that surface. Prefer the
actual `/von/generate` path or the actual browser chat UI.

## 3. Core Principle

The job is not to make one answer look plausible.

The job is to make the real turn lifecycle respond correctly to the user's text
input, with decision policy carried by workflows, prompts, KB state, and
Vontology artefacts rather than by hidden Python rescue logic.

That means every replay should answer two questions:

1. Did the user-visible behaviour improve?
2. Does the persisted telemetry show the right reason why it improved?

If those disagree, the task is not done.

## 4. Query-Set Design

Do not validate only the triggering prompt. Use a small prompt family that
tests whether the system understands the underlying information request.

Include:

1. The exact triggering prompt.
2. An explicit named-entity variant.
3. A nearby relation-query variant that does not use the same noun family.
4. A non-user-relative variant that should exercise the same retrieval and
   routing basis.

Example family for entity-relative represented-fact lookup:

- `What papers of mine do you know about?`
- `What papers are associated with Michael Witbrock?`
- `What students of mine do you know about?`
- `Which workflows mention episode evaluation?`

The point is not these exact prompts. The point is to test the general
capability class:

- entity-relative KB lookup
- represented relation retrieval
- selector steering from intent, not surface token overlap
- answer generation grounded in retrieved evidence or explicit uncertainty

If the exact prompt passes but the neighbouring prompts fail, you probably
implemented a narrow local fix.

## 5. Setup

Before the first replay:

1. Identify the authoritative artefacts for the path you expect:
   - workflow concepts
   - prompt concepts
   - relevant KB concepts or predicates
   - any workflow publication/bootstrap surface that must already be current
2. Run targeted automated checks for the files you already changed.
3. Start Von locally on a known port.
4. If the issue depends on authenticated state, use the browser-test login path
   or another canonical authenticated route rather than faking user context.

Do not start by patching Python based only on the symptom. First confirm the
expected authority surfaces and then inspect the real turn.

## 6. Preferred Replay Surfaces

Use the nearest faithful surface in this order:

1. Browser chat UI on the local server.
2. Real `/von/generate` request against the local server.
3. Only if neither is safe or practical, a narrower canonical surface such as
   `von_chat_run`, with the limitation recorded explicitly.

For user-visible answer defects, browser replay is best because it exercises:

- the actual request shape
- real auth/session state
- response rendering
- narration/screen transforms
- any live thinking-card or progress telemetry

## 7. One Replay Cycle

For each replayed prompt:

1. Send the prompt on the real server path.
2. Record the user-visible answer exactly.
3. Capture the telemetry locator or the key identifiers:
   - `request_id`
   - `turn_id`
   - `history_location`
   - `chat_session_id`
   - namespace context
4. Fetch the persisted evidence:
   - `turn_execution_get_diagnostics`
   - `chat_history_get_debug_entry`
   - `conversation_telemetry_get_locator`
   - `workflow_get_execution_trace` for any referenced workflow traces
5. Read the turn in stage order rather than jumping straight to the answer.
6. Classify the failure boundary.
7. Apply the smallest durable fix at the right authority surface.
8. Replay the same prompt again.
9. Replay the nearby prompt variants.

Do not call the task fixed after one improved replay unless the bug was purely
mechanical and the neighbouring prompts prove the same capability.

## 8. What to Inspect in Telemetry

Inspect the turn in this order.

### 8.1 Prompt and context

Check:

- the actual user prompt
- effective namespace and auth state
- context size and whether the right conversation state was present
- whether stage-specific context additions or reductions were recorded

If the system is reasoning over the wrong context, do not patch downstream
selection first.

### 8.2 Expected-outcome or intent reasoning

Look for the stage that should interpret what the user is actually asking for.

Check whether it recognised the request as, for example:

- KB lookup
- relation retrieval
- user-relative entity lookup
- direct answer versus workflow/tool path

If the system has an explicit intent or expected-outcome stage, verify that its
output materially affected downstream discovery and selector context. A stage
that exists only in telemetry but does not steer the rest of the turn is not
good enough.

### 8.3 Workflow discovery

Inspect:

- `workflow_discovery.query`
- `workflow_discovery.discovery_query_input`
- candidate list
- match sources
- relevance and exclusion reasons

Ask:

- Was the candidate set even capable of satisfying the user request?
- Did discovery search for the underlying intent, or only for lexical overlap
  with a specialised workflow name?

### 8.4 Selector decision

Inspect:

- selected workflow
- selector verdict and source
- selector-stage prompt/context lineage
- whether the selector used the prepared intent/output from earlier stages

If the selector picked a plausible workflow from a bad candidate set, the fix
is usually upstream in discovery. If the candidate set was good and the
selection was still wrong, the fix is usually in selector context, prompt, or
policy metadata.

### 8.5 Dispatch and execution

Inspect:

- selected execution mode
- workflow handoff status
- `failure_codes`
- zero-tool-execution flags
- tool invocation counts
- workflow trace references

Typical questions:

- Was the selected path actually runnable?
- Did dispatch misclassify a tool pipeline as a custom workflow?
- Did the workflow start but do no useful work?
- Did represented side-effects happen but fail to surface in the answer?

### 8.6 Response composition and transforms

Inspect:

- the raw answer path
- narration or presenter transforms
- screen/spoken backfill stages
- whether the user-facing answer was built from retrieved facts or from
  execution bookkeeping

This catches the common failure mode where routing/execution mostly worked but
the answer still undershoots, overstates, or hides the result.

### 8.7 Telemetry projection quality

Check whether the persisted diagnostics preserve the stages and lineage you
need.

If the workflow behaved correctly but the telemetry collapsed the authored
stages into generic buckets, that is a telemetry bug, not a routing bug. Fix it
separately and say so.

## 9. Failure Classification

Use a simple classification before editing anything:

1. `Intent failure`
   The system did not recognise the underlying information request.
2. `Discovery failure`
   The candidate set did not include the right workflow/tool family.
3. `Selector failure`
   The candidate set was usable but the selector chose badly.
4. `Dispatch failure`
   The right path was chosen but the execution mode or handoff was wrong.
5. `Execution failure`
   The workflow/tool path ran but did not complete the required effect.
6. `Answer failure`
   The system did useful work but the user-facing answer did not reflect it.
7. `Telemetry failure`
   The turn may have behaved correctly, but the persisted explanation is wrong
   or incomplete.

This prevents one symptom from turning into three unrelated code patches.

## 10. Fix Discipline

Apply fixes in this order:

1. Prompt/workflow/KB/Vontology authority surface.
2. Missing reusable support surface in Python.
3. Telemetry or validation support.

Avoid:

- code-side prompt bodies for durable policy
- domain-specific forcing rules in core orchestration
- lexical tables that pretend to be semantic routing
- closing a task because a harness passes while the real route still fails

If you find yourself forcing one proper-noun workflow or one noun family to win
selection, step back. The system probably needs a more general information-flow
fix.

## 11. Re-Run Criteria

After each fix:

1. Replay the exact triggering prompt.
2. Replay the nearby prompt family.
3. Re-check the telemetry for the new run.
4. Confirm that the explanation for success moved in the expected stage.

Good signs:

- discovery query and candidate set now reflect the underlying request
- selector context shows the intended steering input
- selected workflow family is plausible and runnable
- execution does real work or explicitly reports grounded uncertainty
- the answer is responsive to the user's text rather than a generic fallback
- telemetry and answer tell the same story

Bad signs:

- the same weak answer with a different justification
- success only on the exact original wording
- zero-tool or zero-workflow behaviour with no grounded explanation
- stage telemetry still missing the steering step you thought you fixed

## 12. Closure Standard

Do not call the behaviour fixed until all of the following hold:

1. The exact real-path replay is correct.
2. At least a small neighbouring prompt family also behaves coherently.
3. The answer is grounded in represented state, workflow results, or explicit
   uncertainty.
4. The persisted telemetry shows the intended workflow-driven reason for the
   behaviour.
5. Targeted regression tests cover the structural failure, not just the final
   string output.

For user-visible issues, record both:

- the user-visible acceptance evidence
- the corresponding telemetry evidence

Those two together are the closure story.

## 13. Practical Reminder

When a task has gone through several rounds of disappointment, increase the
burden of proof:

- prefer the exact server path over helper surfaces
- prefer multiple nearby prompts over one prompt
- prefer telemetry-backed diagnosis over intuition
- prefer "still not fixed" over premature closure

That is slower than a narrow patch, but it is much faster than reopening the
same behaviour repeatedly.
