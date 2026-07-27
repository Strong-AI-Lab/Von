# Real-Path Server Replay and Telemetry Loop

- **Kind:** Validation protocol and practical guidance
- **Lifecycle:** Active
- **Authority:** Canonical replay/telemetry protocol routed by `AGENTS.md`; use
  only the sections and validation depth justified by the claim
- **Created:** 2026-04-17
- **Last substantive content update:** 2026-07-25
- **Last reviewed:** 2026-07-25
- **Evidence boundary:** Each acceptance claim still requires its own dated
  exact-path evidence

## 1. Purpose

This note describes a repeatable engineering loop for fixing user-visible Von
behaviour on the real server path, rather than stopping at nearby unit tests or
synthetic harnesses.

Use it when the claim you need to validate is something like:

- "Von responds to this text input correctly"
- "the turn succeeds through an appropriate direct, model, tool, or workflow
  path"
- "the chosen path and answer are aligned"
- "telemetry matches what the user actually experienced"

The loop is deliberately iterative, but its depth is risk-tiered. Replay the
real prompt against the nearest faithful surface, inspect enough telemetry to
find the actual failure boundary, fix it, and replay until the behaviour and
evidence agree. Add prompt families, browser passes, full telemetry
reconciliation, negative controls, or release provenance only when the intended
claim needs them.

## 2. When to Use It

Use the full process for Tier 2 or Tier 3 claims under `AGENTS.md`, and use the
applicable subset for Tier 1. Typical triggers are:

- the bug is user-visible or answer-visible;
- the route, orchestrator, workflow, selector, or response path may be at
  fault;
- a unit test passes but the real system still behaves incorrectly;
- you need confidence that a behaviour works for the claimed general reason
  rather than only in a test harness;
- the failure might be caused by the wrong candidate set, wrong selector
  steering, wrong execution mode, or weak answer composition.

Do not treat `von_chat_run` or a direct helper call as sufficient acceptance
evidence for a `/von/generate` or browser claim. Prefer the nearest faithful
affected surface. Browser validation is required when rendering, live progress,
authentication/session behaviour, or another browser-only concern is part of
the claim; it is not an automatic second pass for every backend behaviour fix.

## 3. Core Principle

The job is not to make one answer look plausible.

The job is to make the real turn lifecycle respond correctly to the user's text
input through the smallest adequate path. That may be a direct function or tool
call, model judgement, a represented prompt/workflow, or a composition of
these. Durable semantic policy should not hide in case-specific Python rescue
logic, but replay evidence must not prescribe a workflow merely to count as
architecturally valid.

That means every replay should answer two questions:

1. Did the user-visible behaviour improve?
2. Does the persisted telemetry show the right reason why it improved?

If those disagree, the task is not done.

### 3.1 Architecture-neutral sentinel cases

Lower-complexity prompts are useful sentinels when they expose whether Von can
use available context and capabilities without case-specific rescue logic. A
passing path is credible when it:

- produces the useful answer or effect with relevant provenance;
- remains viable across neighbouring phrasings and, where material, languages;
- does not rely on prompt-bank memorisation, proper-noun forcing, broad
  irrelevant fan-out, or an English lexical intent table; and
- leaves materially different competent strategies available unless a concrete
  risk requires otherwise.

Do not require the simple case to traverse the same selector, workflow, or tool
sequence as a richer case. Ask whether the successful reason is general enough
for the claimed capability, not whether it follows the current architecture.

## 4. Expectation-First Preflight

Before a Tier 2/3 replay, or a Tier 1 case whose answer quality is subjective,
form a concise expectation for what a reasonable answer would need to achieve.
Record it in task notes or report it to the user when that helps collaboration;
do not interrupt the user with a ceremonial preflight for every replay.

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

Recording the expectation before seeing the answer prevents post hoc grading
drift and makes the acceptance standard falsifiable.

## 5. Query-Set Design

When the change claims to repair a general capability class, do not validate
only the triggering prompt. Use a small prompt family that tests the intended
generality. A narrowly scoped mechanical defect may need only the exact case
plus its targeted regression.

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

For lower-complexity prompts in the family, keep the same bar. They are useful
because they expose whether the general discovery, retrieval, workflow, and
answer-grounding architecture is healthy. They are not a separate invitation to
introduce an easier, narrower success path that the harder compositional turns
would never use.

### 5.1 Prompt-Variant Failure Replays

For replay-backed prompt improvement, keep the prompt hypothesis and promotion
policy represented. The live sampler can provide the generic execution surface:
model arms, prompt-variant arm metadata, prompt-variant selection telemetry, and
experiment-run observations.

Minimal local command shape:

```powershell
pdm run python scripts/run_live_kb_tool_prompt_sampler.py `
  --prompt-text "List my last six email messages." `
  --replay-case-id "mail-listing-failure" `
  --model "gemma4:26b" `
  --compare-model "gpt-5.4-mini" `
  --base-prompt-id "#V#mail_answer_prompt" `
  --prompt-variant-id "#V#gemma_mail_answer_prompt_v2" `
  --workflow-id "#V#general_mail_review_workflow" `
  --workflow-stage-id "turn_answer" `
  --experiment-run-id "#V#experiment_run_mail_prompt_variants"
```

This records whether the normal runtime selected the represented prompt variant;
it does not inject raw prompt text or promote a variant. A telemetry-inconsistent
arm can remain useful comparator evidence, but it is non-promotable until the
material response, observed effects, and user-visible answer agree. A
completion gate is relevant only when that path deliberately uses one.

For earlier fix planning on a poorly performing prompt, use
`scripts/replay_llm_exchange.py` when you only need to time or compare one LLM
exchange. It can replay a captured `request_id`, send an explicit draft prompt
with `--prompt` or `--prompt-file`, or preserve a captured exchange's context
while replacing only the prompt body with `--override-prompt-file`. That is a
good way to test whether a proposed prompt revision is promising before
materialising it as a represented prompt variant. Treat the result as
diagnostic planning evidence only: it does not run Von workflows, execute tools,
select represented prompt variants, or prove the user-visible turn is fixed.

### 5.2 Random Prompt Sampling

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
4. Before running a sampled prompt used as acceptance evidence, record the
   expectation-first preflight when Tier 2/3 or subjective judgement warrants
   it.
5. The sampler does not issue a semantic verdict. Its collection status says
   whether the run evidence was captured, not whether the answer was good.
   Compare the real answer and, where relevant, effects, model, timing, and
   telemetry against the user job, or use an explicit evaluator when the
   experiment genuinely needs one.
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

1. Identify the capabilities, evidence, and authority surfaces the actual path
   may use. Do not assume in advance that these must include a workflow,
   selector, critic, or represented prompt.
2. Run targeted automated checks for the files you already changed.
3. Start Von locally through the host-native isolated agent-test launcher:
   - macOS/Linux: `./run.sh restart -AgentTest -NoBrowser -HealthTimeoutSec 180`
   - Windows/PowerShell: `.\run.ps1 restart -AgentTest -HealthTimeoutSec 180`
   Maintained live replay/testing scripts default to
   `http://127.0.0.1:5010` and require `/health` to report
   `agent_test_instance=true`, so they do not accidentally hit the
   interactive/user-facing server on port 5000.
   If you intentionally use a different `-AgentTest -Port`, set
   `VON_AGENT_TEST_BASE_URL` or pass the matching `--base-url`.
   When the acceptance claim depends on the represented postcondition critic,
   start AgentTest with `VON_AGENT_TEST_REAL_POSTCONDITION_CRITIC=1`. The normal
   AgentTest shortcut intentionally skips that LLM subworkflow for speed and is
   not valid evidence for critic prompts, terminal outcome receipts, or
   represented recovery decisions.
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
7. If the claim specifically concerns a selected terminal-outcome receipt,
   inspect the projections on the surfaces that path actually uses and reconcile
   the fields material to the claim. Do not require five storage/projection
   surfaces or a critic receipt for an ordinary successful turn.
8. Treat `orchestrator_result_ready` and equivalent answer-available events as
   evidence that an answer is available, not proof of every background effect.
   Verify the terminal status or world state needed by the claim; a receipt is
   required only when the selected contract makes it part of that claim.

Select the model or comparison arms from the capability slice, active profile,
or task hypothesis. Record requested and executed models when model choice is
material; this protocol does not impose a programme-specific default model.

For browser follow-up, also record the visible user-facing model/provider shown
in the UI when that surface exposes one. If the scripted replay and browser
surface appear to be using different models, say so explicitly rather than
letting later reviewers infer it. If the scripted replay and browser follow-up
use different authenticated users or organisations, do not treat model
differences as a parity defect until you have recorded that identity mismatch
explicitly.

Do not patch any surface from the symptom alone. Inspect the real turn and put
the repair on the smallest adequate authority or execution surface; Python is
neither automatically wrong nor automatically the right fix.

## 7. Preferred Replay Surfaces

Use the nearest faithful affected surface:

1. Browser chat UI when browser-only state, rendering, live progress, or
   authentication is part of the claim.
2. Real `/von/generate` for backend turn behaviour that does not require
   browser-only evidence.
3. A narrower canonical surface such as `von_chat_run` only when it is the
   affected surface or higher-fidelity execution is unsafe/unavailable, with
   the limitation recorded explicitly.

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
resolves history location, fetches persisted debug telemetry, and emits an
unscored collection record. It defaults to the `JVNAUTOSCI-2070` isolated agent-test
backend and will reject a non-agent-test server unless you pass
`--allow-non-agent-test-server` for an explicitly interactive-server check.
Collection success means the evidence was captured; assess the user outcome
separately.

The sampler also supports controlled multi-arm model comparison. Use repeated
`--compare-model` flags, and optionally `--include-active-model-arm`, when you
need the same selected prompt, seed, authenticated user/org context, and replay
method across multiple model arms. Each arm should run in its own fresh
authenticated conversation so one arm's answer history does not contaminate the
next arm's replay.

When the active Thinking card or related live progress surface is part of the
user experience, include that in acceptance review too. Use
`docs/engineering/thinking_card_live_llm_visibility_design.md` as the contract
for what the card should communicate.

Follow a successful scripted `/von/generate` pass with a browser replay when
the claim includes the live Thinking card or another browser-only surface. The
scripted pass remains sufficient for a backend-only Tier 1 claim when it is the
nearest faithful path.

## 8. One Replay Cycle

For each replayed prompt:

1. Form and record the expectation-first preflight when required by the selected
   validation tier or subjective answer rubric.
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
   A background task's terminal result and its chat-history projection can
   settle at slightly different times.  When they describe the same
   `request_id`, merge the richer terminal diagnostics with history-only
   fields instead of letting an earlier empty nested projection hide later
   selector, dispatch, tool, or completion evidence.  Hydrate debug blob
   references before treating a compact projection as evidence that a field
   was absent.
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
10. If the replay was a scripted `/von/generate` pass, follow it with a browser
   replay only when the claim includes the Thinking card, rendering,
   authentication/session state, or another browser-only concern.
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
15. Replay nearby prompt variants when the claimed fix is general rather than
   mechanical or case-bounded.
16. If the validation tier or hypothesis warrants it, replay one sampled prompt
   from the live prompt bank.

Do not over-generalise from one improved replay. Conversely, do not require a
prompt family for a purely mechanical defect whose regression and exact path
already establish the bounded claim.

## 9. What to Inspect in Telemetry

Inspect only the surfaces the actual path used. The subsections below are a
diagnostic menu, not required controller stages; absence of a selector,
workflow, critic, or dedicated intent phase is not itself a defect.

### 9.1 Prompt and context

Check:

- the actual user prompt
- effective namespace and auth state
- context size and whether the right conversation state was present
- whether material call- or path-specific context changes were recorded

If the system is reasoning over the wrong context, do not patch a later surface
first.

### 9.2 Interpretation, when separately visible

Locate where the path interpreted what the user was asking. This may be within
one model call rather than a dedicated stage.

Check whether it recognised the request as, for example:

- KB lookup
- relation retrieval
- user-relative entity lookup
- direct answer versus workflow/tool path

If the system has an explicit intent or expected-outcome stage, verify that its
output materially affected downstream discovery and selector context. A stage
that exists only in telemetry but does not steer the rest of the turn is not
good enough.

### 9.3 Workflow discovery, if used

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

### 9.4 Selector decision, if used

Inspect:

- selected workflow
- selector verdict and source
- selector-stage prompt/context lineage
- whether the selector used the prepared intent/output from earlier stages

If the selector picked a plausible workflow from a bad candidate set, the fix
is usually upstream in discovery. If the candidate set was good and the
selection was still wrong, the fix is usually in selector context, prompt, or
policy metadata.

### 9.5 Dispatch and workflow execution, if used

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

### 9.6 Response transforms, if used

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
- which capability or route it is using, where that helps;
- what kind of information or tool work it is using to answer;
- what it is currently waiting on;
- whether fallback or tool-planning behaviour is explained in user-facing
  terms;
- whether it avoids collapsing into Von-internal code mechanics, stage IDs, or
  implementation trivia as the primary story.

If the answer is acceptable but the live card is dominated by code-mechanics
detail or fails to communicate the reasoning and information sources in
user-facing language, that is still a user-visible deficiency worth recording.

### 9.9 Decision attribution evidence

Some current diagnostics expose historical `decision_attribution` fields for
discovery, selection, dispatch, model choice, recovery, and acceptance, plus an
`architecture_integrity_score` based on represented attribution. Treat these as
compatibility telemetry, not a model of the decisions every turn must contain
and not a quality score for new architecture.

Use attribution to locate where a material decision was actually made and to
find hidden case-specific policy. A correct direct or Python-supported path is
not an architectural failure merely because it is not represented. Judge
whether the chosen surface is the smallest adequate one, whether durable policy
is inspectable when it needs to be, and whether the complete user job improves.
Do not optimise the represented fraction or add stages so this legacy score
rises.

### 9.10 Telemetry projection quality

Check whether persisted diagnostics preserve the material decisions, evidence,
effects, and lineage the claim needs.

If the path behaved correctly but telemetry collapsed material events into
misleading buckets, that is a telemetry bug, not a reason to add controller
stages. Fix it separately and say so.

### 9.11 Tool-result lineage and evaluator evidence

A provider-native tool-result message is valid only when the same provider
conversation contains the matching tool-call record and the call identifier,
ordering, and tool identity remain unambiguous. A fresh result without that
lineage must not be presented to the provider as though it were a continuation
of an earlier call. Project it instead as bounded, redacted, explicitly
untrusted context evidence with its source and available lineage preserved. If
the provider rejects a purported continuation, expose a typed protocol failure;
do not silently switch to a representation that changes the evidence's
authority or meaning.

Tool names, invocation counts, and success flags establish that a path was
attempted; they do not establish what the tool found or whether the final answer
follows from it. An evaluator that judges grounding therefore needs a bounded
semantic projection of the relevant observation, including stable identifiers,
status, result cardinality, the small set of fields needed for the judgement,
explicit missing or redacted fields, typed errors, and provenance back to the
observation. Use the smallest stable projection that repeated evaluation
actually needs; representation in Vontology is optional. Keep payloads bounded
and secret-safe, and treat their content as data rather than instructions. If a
claim requires a projection that is absent, record an evidence gap instead of
inferring success from a tool name or guessing the missing payload.

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

## 11. Fix discipline

Locate the actual failure boundary, then choose the smallest adequate repair
under `AGENTS.md`. The correct authority may be a direct tool/function, prompt,
workflow, Vontology artefact, retrieval policy, support primitive, or user-facing
composition. Do not prefer a represented layer merely because the symptom is
user-visible, and do not put durable adaptable policy into Python merely because
the symptom appeared there.

Do not add a compulsory stage or restriction unless comparative end-to-end
evidence shows that it improves the complete user job. A missing workflow is a
defect only when a reusable workflow is actually the smallest adequate
authority surface.

Replay the exact case and a nearby family when the claim is general. Inspect the
actual path and final world state. Avoid prompt-local forcing, lexical tables,
case-specific production branches, and closure from a synthetic harness alone.

## 12. Re-Run Criteria

After each fix, apply the items required by the chosen validation tier:

1. Replay the exact triggering prompt.
2. Replay the nearby prompt family when generality is claimed.
3. Replay one sampled live-bank prompt when sampling is part of the hypothesis
   or Tier 2/3 campaign.
4. Follow a scripted pass with a browser replay when the affected claim includes
   a browser-only surface.
5. For interesting failures, consider the same browser follow-up when the live
   card may reveal user-facing transparency gaps.
6. Re-check the telemetry for the new run.
7. Confirm that the actual reason and path for success changed as intended.
8. Confirm that the live answer cleared the recorded bar, or exceeded it on its
   own grounded merits, rather than merely improving relative to a bad
   baseline.
9. For a Tier 2/3 Jira-backed acceptance campaign, prepare a closure record
   proportionate to the claim:
   - the exact replayed prompt text;
   - the exact user-visible answer, or a faithful quoted excerpt if the full
     answer is too long to quote comfortably in the task;
   - the key telemetry identifiers for the accepted run;
   - a short synopsis of what the live Thinking card showed, or would have
     shown if the browser pass exposed it;
   - a short note on why that Thinking-card content would or would not have
     helped a user understand the answering process.
   If no browser Thinking card or equivalent user-facing progress surface was
   available, say so explicitly rather than silently omitting that part of the
   record.

Good signs:

- execution does useful work or explicitly reports grounded uncertainty;
- the answer is responsive to the user's request rather than a generic
  fallback;
- neighbouring phrasings and materially different competent paths can succeed;
- false-empty claims do not survive post-check against relevant evidence;
- any affected progress UI explains reasoning and information use in
  user-facing terms; and
- telemetry, observed effects, and answer tell the same story.

Bad signs:

- the same weak answer with a different justification
- success only on the exact original wording
- no grounded explanation for the answer or observed effect
- a "no results" answer that contradicts actual represented content
- a Thinking card that mainly explains Von code mechanics rather than the
  answering process
- telemetry still omits a material decision or effect on the path actually used

## 13. Closure Standard

Do not call the behaviour fixed until all applicable items for the selected
validation tier hold:

1. The exact real-path replay is correct.
2. A neighbouring prompt family behaves coherently when the change claims a
   general capability repair.
3. Where random sampling is part of the acceptance campaign, the sampled replay
   also behaves coherently for its capability class.
4. The answer clears the expectation-first bar for the prompt, or exceeds it on
   its own grounded merits.
5. Any "no results" or "nothing represented" claim survives post-check against
   the relevant authority surfaces.
6. The answer is grounded in the relevant evidence and observed effects, with
   explicit uncertainty where needed.
7. Where the affected claim includes the live Thinking card or another
   browser-only surface, a successful scripted pass has been followed by a
   browser replay unless a limitation is recorded.
8. Where a live Thinking card was part of the affected claim, it also meets a
   user-facing usefulness standard consistent with
   `thinking_card_live_llm_visibility_design.md`.
9. The persisted telemetry shows the actual successful reason and path,
   whether direct function/tool, model-led composition, or workflow.
10. Targeted regression tests cover the structural failure, not just the final
   string output.

### 13.1 Tier 3 non-circular candidate and release evidence

This subsection applies to strong authority-release, certification, candidate
promotion, or exact-provenance claims. It is not a closure requirement for
ordinary Tier 1 behaviour fixes.

A final operational campaign cannot also be the only pre-activation test for a
candidate intended to repair that campaign. Requiring the final gate to pass
before the candidate can be tried creates circular evidence; activating first
and grading afterwards removes the safety boundary. Use a separate represented
candidate-safety suite before promotion, with the exact triggering or baseline
case, nearby cases, perturbation or minefield cases, and explicit regression and
scope checks. That suite authorises only the bounded candidate decision. After
promotion, rerun the final campaign and verify through telemetry that the
runtime consumed the exact promoted release; the candidate suite is not a
substitute for final acceptance.

Release evidence is also actor-scoped. Bind candidate evaluations, approvals,
promotion or rollback receipts, and active-release read-backs to the effective
namespace, user, organisation, affected artefact, candidate identity, and
release digest as applicable. Never silently copy, infer, or share one actor's
ledger state with another actor, even when they are in the same cohort or are
testing identical bytes. Aggregate cohort results only after the independently
scoped ledgers and read-backs are complete. Any genuinely shared release policy
must explicitly represent its audience and approval authority rather than
emerging from ledger reuse.

Candidate provenance must bind the semantic payload, not merely name a
represented proposer. Persist the exact proposal workflow output in the Turn
Execution Record, including bounded failure-packet, actor-scope, affected-
artefact, workflow-definition, and prompt-revision lineage. At registration,
read that trace back and require the proposed payload and its digest to occur
unchanged in the recorded output. A caller-supplied payload paired with a valid
workflow ID or current definition hash is not represented authorship; accepting
it would allow hidden code or an API caller to author policy while borrowing a
represented authority label.

An awaited durable `workflow_execute` result may be labelled exact only when
the completed checkpoint is bound outside workflow-controlled context to the
specific live worker claim that wrote it. Require a capability-bearing worker
claim, an opaque claim token, lock-fenced checkpoint and terminal writes, and a
manager-side digest over the authority output, prompt lineage, and actually
loaded definition identity. Strip those reserved fields from launch inputs.
On resume, reject exactness permanently if the preceding checkpoint is
unattested, the producing worker claim changed, or the loaded definition
changed; do not let an A-to-B-to-A sequence collapse back to an apparently pure
A lineage. Until producer-specific lineage is represented, exact projection is
limited to a definition containing exactly one self-contained `llm.action` with
explicit `tool_mode: none`, and the trace must show exactly one actual producer
invocation. Fail exactness for zero or multiple potential producers, retry,
idempotency or cyclic execution surfaces, an unbound child workflow, a dynamic
tool or MCP surface, or an arbitrary registry action. A represented mapping may
project the sole LLM result into the authority-output field, but it must not
author or overwrite prompt or execution lineage; reassert prompt diagnostics
from the actual producer record. If checkpoint bounding redacts, truncates, or
omits any authority-lineage field, make both the persisted attestation and
execution trace visibly ineligible. The workflow may still complete for its
ordinary user-facing purpose: these checks govern the strong provenance claim,
not represented recovery or general execution.

For user-visible issues, record both:

- the user-visible acceptance evidence
- the corresponding telemetry evidence

If a Jira-backed campaign is actually being closed under current decision
authority, record only the evidence material to its claim: for example the
prompt, answer/effect, and stable run locator. Do not require a closure comment,
browser pass, Thinking-card synopsis, or full telemetry packet merely because
the task has a tier or Jira key.

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
