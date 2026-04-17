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
telemetry-consistent. For robustness, combine the triggering prompt with a
small nearby prompt family and, where useful, a sampled prompt from the
maintained live prompt bank.

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

## 4. Expectation-First Preflight

Before replaying any prompt, first form and report to the user your full
expectation for what a reasonable answer would likely need to achieve.

Do this before seeing the new live answer, not afterwards.

The expectation should not be a guessed exact wording. It should be a concrete
record of expectation against which the live answer can be judged, including:

1. What substantive content a good answer should cover.
2. Which knowledge surfaces probably need to contribute:
   - KB
   - web
   - Jira
   - arXiv
   - or a clearly justified subset
3. What kind of evidence, examples, or grounding would make the answer feel
   genuinely responsive rather than generic.
4. What uncertainty behaviour would be reasonable:
   - what the system may safely say with current evidence
   - what it should explicitly mark as uncertain
   - whether a follow-up question would be justified only after machine-side
     retrieval is exhausted
5. What would count as a user-disappointing answer:
   - empty refusal
   - generic caveat without meaningful retrieval
   - tool use that does not reach the required knowledge surface
   - confident claims without grounding

The point is to set the bar for the replayed answer, not to specify exactly
what the answer must contain. Von may have access to represented data,
retrieved evidence, or grounded connections that the coding agent does not yet
see directly. If the live answer is better than expected and is acceptable on
its own merits, accept it.

Example:

For `What papers of mine do you know about?`, the coding agent may not know in
advance whether represented papers actually exist. The expectation should not
claim a specific count. Instead, it should record something like:

- if represented papers exist, a good answer should surface them explicitly;
- a stronger answer would present them as concept-backed papers with titles,
  short summaries, references, or other grounded identifying details;
- if the answer claims there are no relevant results, that claim should later
  be checked against the relevant Vontology and RAG surfaces rather than simply
  trusted.

For prompts that implicitly require recent external evidence, the expectation
should name that explicitly. For example, a good answer to a
"recent open-source projects aligned with my KB themes" prompt should normally
include both:

- a grounded read of the relevant represented themes in the KB; and
- a current external lookup that verifies project recency and thematic fit.

Report this expectation briefly to the user before the replay. This prevents
post hoc grading drift and makes the acceptance standard falsifiable.

## 5. Query-Set Design

Do not validate only the triggering prompt. Use a small prompt family that
tests whether the system understands the underlying information request.

Include:

1. The exact triggering prompt.
2. An explicit named-entity variant.
3. A nearby relation-query variant that does not use the same noun family.
4. A non-user-relative variant that should exercise the same retrieval and
   routing basis.
5. When useful, one sampled prompt from the maintained live prompt bank that
   stresses the same or an adjacent capability class.

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

### 5.1 Random Prompt Sampling

Von already has a maintained live prompt bank and replay harness for this:

- `scripts/live_kb_tool_prompt_bank.json`
- `scripts/run_live_kb_tool_prompt_sampler.py`

Use them when you want a real `/von/generate` replay that samples a KB-sensitive
or tool-sensitive prompt rather than only replaying the original wording.
`scripts/run_live_kb_tool_prompt_sampler.py` is the maintained random test
chooser for this prompt bank.

The prompt bank now distinguishes several prompt complexity classes, and the
sampler can constrain random choice with `--complexity-class`:

- `direct_context_or_background`
  Easy questions that a capable direct-response LLM should usually answer from
  turn context, runtime context, or background knowledge alone.
- `vontology_grounded`
  Questions that should be answerable from that base plus represented
  Vontology/KB content.
- `tool_augmented`
  Questions that combine those abilities with live tool use such as web, Jira,
  or arXiv lookup. This class also includes local operational prompts such as
  Von task creation/listing/status change and direct-message creation/counting.

The sampler itself now points back to this document and prints a runtime note
that it should be used in conjunction with this method. Treat that reminder as
part of the tool's intended use, not as optional commentary.

Practical rules:

1. Do not let random sampling replace the exact triggering prompt. It is a
   robustness check, not the primary acceptance case.
2. Prefer a sampled prompt from the same capability family when the failure
   seems class-wide.
3. Record the sampled prompt id, category, bank version, and any random seed so
   the run is reproducible.
4. Before running the sampled prompt, still do the expectation-first preflight
   and report that expectation to the user.
5. Treat the harness verdict such as `should_user_be_happy` as supporting
   evidence, not as the whole judgement. Compare the real answer and telemetry
   against your recorded expectation.
6. For operational prompts that are really asking Von to mutate or inspect its
   own task/message state, record whether actual task/message tool use happened.
   A generic "I can help with that" answer is not a pass for a create/update
   prompt.
7. When the capability spans multiple classes, work up the complexity over
   time:
   - start with `direct_context_or_background`;
   - then move to `vontology_grounded`;
   - then move to `tool_augmented`.
   A failure in an easier class should usually be addressed before you put much
   interpretive weight on a harder-class failure above it.

Useful cases:

- after fixing a prompt-specific failure, to test whether the underlying
  capability improved;
- when the failure looks like a broader KB-plus-tool retrieval weakness;
- when you want to confirm that an apparently hard failure is not actually
  downstream of a simpler direct-answer or KB-grounding weakness;
- when you want a fast regression sentinel that still exercises the real server
  path.
- when you want lightweight operational coverage for built-in Von surfaces such
  as task creation/list/search/status updates and message creation/counting,
  without jumping immediately to harder multi-surface research prompts.

## 6. Setup

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
5. If you are using a sampled prompt, record the prompt id, category, prompt
   bank version, and seed or selection method in your notes.
6. Record the relevant run-environment features for the replay, not just the
   prompt text. At minimum, capture:
   - base URL or server surface used;
   - authenticated user and organisation context;
   - the server-resolved active model/provider for that authenticated context,
     when the server exposes it;
   - requested model, if you explicitly overrode it;
   - actual model reported by Von telemetry;
   - local branch/commit identity for the checkout you ran from;
   - server-reported version/branch/commit when the server exposes them.

For the current `JVNAUTOSCI-1894` replay programme, the default scripted model
override should be Ollama `gemma4:26b` unless a task explicitly requires a
different model or a controlled A/B comparison. If you deliberately omit that
override, record the reason.

For browser follow-up, also record the visible user-facing model/provider shown
in the UI when that surface exposes one. If the scripted replay and browser
surface appear to be using different models, say so explicitly rather than
letting later reviewers infer it. If the scripted replay and browser follow-up
use different authenticated users or organisations, do not treat model
differences as a parity defect until you have recorded that identity mismatch
explicitly.

Do not start by patching Python based only on the symptom. First confirm the
expected authority surfaces and then inspect the real turn.

## 7. Preferred Replay Surfaces

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

For scripted real-route replay against `/von/generate`, prefer the existing live
sampler harness when it fits the task:

- `scripts/run_live_kb_tool_prompt_sampler.py`

It already creates a fresh authenticated conversation, captures the response,
resolves history location, fetches persisted debug telemetry, and emits a
conservative verdict. Keep in mind that its verdict complements rather than
replaces the expectation-first human judgement.

When the active Thinking card or related live progress surface is part of the
user experience, include that in acceptance review too. Use
`docs/engineering/thinking_card_live_llm_visibility_design.md` as the contract
for what the card should communicate.

In practice, a successful scripted `/von/generate` pass should usually be
followed by a browser replay of the same prompt when the normal user path
includes the live Thinking card. The scripted pass is still useful for speed
and exact telemetry capture, but it cannot tell you whether the live user-facing
progress surface was actually good.

## 8. One Replay Cycle

For each replayed prompt:

1. Form the expectation-first preflight for that prompt and report it briefly
   to the user.
2. Send the prompt on the real server path.
3. Record the user-visible answer exactly.
4. Capture the telemetry locator or the key identifiers:
   - `request_id`
   - `turn_id`
   - `history_location`
   - `chat_session_id`
   - namespace context
5. Record the relevant environment features for this run:
   - base URL;
   - authenticated user/org;
   - server-resolved active model/provider for that authenticated context, when
     the server exposes it;
   - requested model, if any;
   - actual telemetry model;
   - browser-visible model/provider, if a browser pass is part of the run;
   - local branch/commit for the checkout you ran from;
   - server-reported version/branch/commit when the server exposes them.
   For the current replay programme, the default scripted requested model
   should normally be Ollama `gemma4:26b`, and the browser pass should be
   switched to the same visible model where practical before you judge parity.
6. Fetch the persisted evidence:
   - `turn_execution_get_diagnostics`
   - `chat_history_get_debug_entry`
   - `conversation_telemetry_get_locator`
   - `workflow_get_execution_trace` for any referenced workflow traces
7. Compare the answer against the recorded expectation before deciding whether
   it was satisfactory, while allowing a clearly better grounded answer to pass
   on its own merits.
8. If the answer says there were no results, no represented facts, or no
   relevant content, perform a post-step verification against the nearest
   relevant authority surfaces:
   - Vontology concepts and relations
   - RAG/KB search results
   - indexed sessions or file copies where relevant
   - any other expected represented source for that prompt class
   For operational task/message prompts, this can include the relevant Von task
   or message state rather than only KB search.
9. If the replay route already exposed a live Thinking card or equivalent
   progress panel, review that user-facing surface as well.
10. If the replay was a scripted `/von/generate` pass and it succeeded against
   the recorded bar, follow it with a browser replay of the same prompt when
   the normal user path includes the live Thinking card.
11. Also consider a browser replay for interesting failures, especially:
   - near-miss answers that were close to acceptable;
   - failures involving waiting, fallback, or tool-planning behaviour the user
     would have seen live;
   - failures where the card might reveal useful reasoning/transparency gaps
     even though the final answer was poor.
12. Read the turn in stage order rather than jumping straight to the answer.
13. Classify the failure boundary.
14. Apply the smallest durable fix at the right authority surface.
15. Replay the same prompt again.
15. Replay the nearby prompt variants.
16. If useful, replay one sampled prompt from the live prompt bank.

Do not call the task fixed after one improved replay unless the bug was purely
mechanical and the neighbouring prompts prove the same capability.

## 9. What to Inspect in Telemetry

Inspect the turn in this order.

### 9.1 Prompt and context

Check:

- the actual user prompt
- effective namespace and auth state
- context size and whether the right conversation state was present
- whether stage-specific context additions or reductions were recorded

If the system is reasoning over the wrong context, do not patch downstream
selection first.

### 9.2 Expected-outcome or intent reasoning

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

### 9.3 Workflow discovery

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

### 9.4 Selector decision

Inspect:

- selected workflow
- selector verdict and source
- selector-stage prompt/context lineage
- whether the selector used the prepared intent/output from earlier stages

If the selector picked a plausible workflow from a bad candidate set, the fix
is usually upstream in discovery. If the candidate set was good and the
selection was still wrong, the fix is usually in selector context, prompt, or
policy metadata.

### 9.5 Dispatch and execution

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

### 9.6 Response composition and transforms

Inspect:

- the raw answer path
- narration or presenter transforms
- screen/spoken backfill stages
- whether the user-facing answer was built from retrieved facts or from
  execution bookkeeping
- whether the answer cleared the recorded bar rather than merely looking
  superficially plausible

This catches the common failure mode where routing/execution mostly worked but
the answer still undershoots, overstates, or hides the result.

### 9.7 False-empty verification

When the answer claims there are no relevant results, no represented papers, no
matching tasks, no KB content, or similar, do not treat that as self-proving.

Do a bounded post-check against the relevant authority surfaces.

Typical examples:

- For represented-paper questions, check whether relevant paper concepts,
  authorship links, names, summaries, or file-copy-backed scholarly artefacts
  exist.
- For KB or RAG questions, check whether semantically relevant indexed content
  exists even if the answer said none was found.
- For Jira-linked questions, check whether Jira results existed but were not
  surfaced.

If the post-check finds relevant content, the turn is not a valid "no results"
success. That is usually an answer failure, execution failure, retrieval
failure, or thinking-panel transparency failure, depending on where the content
was lost.

### 9.8 Thinking panel review

When the turn exposes a live Thinking card, progress card, or equivalent user
surface, inspect it as part of user-visible acceptance rather than treating it
as optional decoration.

Use `docs/engineering/thinking_card_live_llm_visibility_design.md` as the
review contract.

The review question is not "would a developer find this mechanically useful?".
The review question is:

- would a user interested in the reasoning, methods, and information used to
  answer the prompt find this card useful and reassuring?

Check in particular whether the card shows:

- what Von is trying to do for the user;
- which route or workflow family it is using;
- what kind of information or tool work it is using to answer;
- what it is currently waiting on;
- whether fallback or tool-planning behaviour is explained in user-facing
  terms;
- whether it avoids collapsing into Von-internal code mechanics, stage IDs, or
  implementation trivia as the primary story.

If the answer is acceptable but the live card is dominated by code-mechanics
detail or fails to communicate the reasoning and information sources in
user-facing language, that is still a user-visible deficiency worth recording.

### 9.9 Telemetry projection quality

Check whether the persisted diagnostics preserve the stages and lineage you
need.

If the workflow behaved correctly but the telemetry collapsed the authored
stages into generic buckets, that is a telemetry bug, not a routing bug. Fix it
separately and say so.

## 10. Failure Classification

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

## 11. Fix Discipline

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

## 12. Re-Run Criteria

After each fix:

1. Replay the exact triggering prompt.
2. Replay the nearby prompt family.
3. If the technique is in scope, replay one sampled live-bank prompt.
4. If a scripted `/von/generate` replay succeeded and the user path normally
   includes the live Thinking card, follow it with a browser replay of the same
   prompt.
5. For interesting failures, consider the same browser follow-up when the live
   card may reveal user-facing transparency gaps.
6. Re-check the telemetry for the new run.
7. Confirm that the explanation for success moved in the expected stage.
8. Confirm that the live answer cleared the recorded bar, or exceeded it on its
   own grounded merits, rather than merely improving relative to a bad
   baseline.

Good signs:

- discovery query and candidate set now reflect the underlying request
- selector context shows the intended steering input
- selected workflow family is plausible and runnable
- execution does real work or explicitly reports grounded uncertainty
- the answer is responsive to the user's text rather than a generic fallback
- false-empty claims do not survive post-check against authority surfaces
- the Thinking card explains reasoning and information use in user-facing terms
- telemetry and answer tell the same story

Bad signs:

- the same weak answer with a different justification
- success only on the exact original wording
- zero-tool or zero-workflow behaviour with no grounded explanation
- a "no results" answer that contradicts actual represented content
- a Thinking card that mainly explains Von code mechanics rather than the
  answering process
- stage telemetry still missing the steering step you thought you fixed

## 13. Closure Standard

Do not call the behaviour fixed until all of the following hold:

1. The exact real-path replay is correct.
2. At least a small neighbouring prompt family also behaves coherently.
3. Where random sampling was used, the sampled replay also behaves coherently
   for its capability class.
4. The answer clears the expectation-first bar for the prompt, or exceeds it on
   its own grounded merits.
5. Any "no results" or "nothing represented" claim survives post-check against
   the relevant authority surfaces.
6. The answer is grounded in represented state, workflow results, or explicit
   uncertainty.
7. Where the normal user path includes the live Thinking card, a successful
   scripted `/von/generate` pass has been followed by a browser replay of the
   same prompt unless there is a clearly recorded reason not to do so.
8. Where a live Thinking card was part of the user experience, it also meets a
   user-facing usefulness standard consistent with
   `thinking_card_live_llm_visibility_design.md`.
9. The persisted telemetry shows the intended workflow-driven reason for the
   behaviour.
10. Targeted regression tests cover the structural failure, not just the final
   string output.

For user-visible issues, record both:

- the user-visible acceptance evidence
- the corresponding telemetry evidence

Those two together are the closure story.

## 14. Practical Reminder

When a task has gone through several rounds of disappointment, increase the
burden of proof:

- prefer the exact server path over helper surfaces
- prefer multiple nearby prompts over one prompt
- prefer explicit recorded expectations over retrospective generosity
- prefer telemetry-backed diagnosis over intuition
- prefer "still not fixed" over premature closure

That is slower than a narrow patch, but it is much faster than reopening the
same behaviour repeatedly.
