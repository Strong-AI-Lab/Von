# Prompt Programs and Model Routing Playbook

- **Kind:** Prompt and model-routing playbook
- **Lifecycle:** Active
- **Authority:** Normative within its stated prompt/model scope
- **Last reviewed:** 27 July 2026

## 1. When to read this

Read this document before planning or implementing work that touches:

- prompt content or prompt rendering
- model selection or routing
- prompt optimisation
- fine-tuning or adapter selection
- evaluation of prompt- or model-governed behaviour

## 2. Core doctrine

When a prompt is deliberately retained as durable, independently governed
operational policy, treat it as:

- first-class artefacts
- versioned and inspectable
- evaluated against downstream task metrics
- eligible for optimisation and rollback

Model selection is also policy. It should not be hidden in scattered conditionals or copied helper logic.

## 3. Prompt program rules

1. For a feature that deliberately selects Vontology prompt governance, store
   its authoritative prompt content in Vontology text relations. Direct or
   ephemeral prompts may remain in the smallest suitable code, configuration,
   or model context with their role made clear.
2. Give important prompt concepts stable identities and descriptions.
3. Maintain prompt lineage when revising or optimising prompts.
4. Keep evaluation notes with the prompt or linked task.
5. Do not silently override a Vontology-governed prompt in code.
6. Do not treat a prompt change as harmless copy-editing when it changes behaviour.

If a selector or classifier exists and its prompt needs independent durable
governance, apply the same discipline as to other governed prompts. This rule
does not require a selector, classifier, or separate response/recovery stage.

## 3A. Shared turn-context discipline

When a capability has demonstrated a need for more than one model stage,
canonical accumulated turn state and each stage's model-visible projection are
both part of the behaviour contract. Do not create selector, planner, critic,
or response stages merely to instantiate this pattern.

Maintain one shared canonical turn-state object where multiple consumers
actually need it. Give each selected stage the minimum sufficient projection
for its job. Stage prompts may add instructions or evidence; projections may
omit or summarise material when that improves relevance, safety, latency, or
cost. Material additions, omissions, and summaries should be inspectable and
evaluated in proportion to the claim.

Different projections are not authority drift by themselves. Undocumented or
unevaluated projection policy is. Keep stable projection mechanisms in code and
author adaptable semantic selection policy in represented profiles/prompts when
independent governance is needed.

Telemetry should expose:

- the canonical state/version used
- the effective stage projection summary
- material stage-local additions, omissions, compaction, or retrieval
- enough lineage to distinguish a prompt fragment from the full
  model-visible context

## 4. Prompt optimisation loop

For features worth systematic optimisation, define:

- target metric
- evaluation dataset or replay set
- baseline prompt version
- candidate-generation method
- promotion threshold
- regression checks
- rollback condition

Possible optimisation methods include manual revision, prompt-program search, textual-gradient style methods, or evolutionary prompt search. The method matters less than disciplined evaluation and rollback.

## 5. Experience-derived guidance

Prompt behaviour can also improve through durable guidance artefacts:

- reflections after failure
- context-conditional guidelines
- retrieved "dos and don'ts"
- domain playbooks

If the guidance is durable and reusable, represent it explicitly rather than leaving it in ad-hoc logs.

## 6. Model portfolio and routing

Maintain an explicit model registry or equivalent metadata covering:

- provider or local source
- cost
- latency
- context limits
- privacy or deployment constraints
- strengths and known weaknesses
- supported tool or modality affordances

Routing policies should be measurable and revisable. Prefer explicit routers or decision surfaces over feature-specific model choices scattered through code.

### 6A. Elapsed-time policy

Conversation-turn and workflow LLM duration thresholds are advisories. The
runtime records a crossing, keeps emitting progress, and retains a usable late
result. Legacy `timeout` configuration names are interpreted as advisory
durations and are not forwarded as provider request deadlines. Successful
durations may raise later advisories with conservative headroom.

The same rule applies to turn-support work that can still yield useful state:
workflow model-policy and registry setup, represented-memory context
resolution, and post-download file-copy registration continue after their
advisory crossings. Their result telemetry records the crossing rather than
substituting a synthetic timeout result.

It also applies below orchestration. Semantic-query embedding is not forced
through the former fixed eight-second request deadline, and derived
relationship-extent index reads and synchronisation do not acquire 1-, 5-, or
10-second PyMongo deadlines. Those paths retain slow results, record advisory
crossings, and raise later advisories from observed successful duration with
headroom. A bounded extent page may still cap the number of rows inspected;
its elapsed-time value is an advisory and cannot end the scan by itself.

Observation windows over durable external or workflow effects may return
control at an advisory crossing when a stable task or instance identifier is
available. The result must remain pending rather than failed and expose the
canonical identifier plus a polling affordance. Jira bulk-move observation and
durable workflow terminal observation follow this rule.

The read-only stdio `von_chat_run` wrapper also treats its legacy
`timeout_seconds` input as an advisory (with `advisory_seconds` preferred). It
uses a bounded worker pool but waits for and returns a usable late turn result.

Legacy model-provider `timeout_seconds` and `request_timeout_seconds` inputs
are likewise duration advisories, not provider cancellation settings. The
OpenAI, Gemini, and Ollama adapters remove them from model parameters, do not
pass them to native transport timeout controls, and retain a completed result
after logging an advisory crossing. Conversation-turn supervision adapts its
next advisory from observed successful durations with capability-specific
headroom.

Workflow-discovery thresholds follow the same rule: crossing the advisory is
telemetry, not permission to skip semantic or Vontology fallback substrates.
A cold capability index may be observed for a short interval before those
alternatives run, but the interval does not cancel the index build or terminate
discovery.

The canonical conversation transcript is also not replayed without bound into
every stateless provider round. The ordinary-turn model projection keeps the
recent transcript window, while the separately supplied represented
conversation situation and bounded observations carry older material
objectives, referents, commitments, and exact observations. The full transcript
remains durable and readable; this projection is a model-context boundary, not
history deletion.

Compatibility payloads may still contain `budget_exhausted` and legacy
`timeout_*` names. Consumers must use `elapsed_time_enforcement`,
`advisory_budget_exceeded`, and `hard_timeout_exceeded` to distinguish a slow
successful search from a real resource failure. Advisory crossings are latency
learning signals; they are not routing failures, zero-result cache blockers, or
negative workflow-selection rewards by themselves.

Legacy represented dynamic-tool `dynamic_timeout_sec` values also configure
an advisory. They do not create a hard boundary that the target capability did
not independently declare and justify.

Explicit user cancellation, provider failure, connection/resource protection,
and durable authority or lease expiry remain real boundaries. A hard model-call
cutoff requires one of those named constraints; elapsed time alone is not
evidence that a call has stopped making useful progress.

For structured tool calls, model capability is not enough on its own: the
provider connection, deployment, model, and API surface form one transport
capability. Represent surface-specific support on the model API profile. An
explicit profile may mark structured tool calling as `required`, `supported`,
or `unsupported`; absence means unknown and must preserve the client's
conservative existing surface. In particular, do not infer Responses support
from an OpenAI-like model name or endpoint URL.

Tool-call continuation is part of that profile. Record whether provider state
may be used or whether Von must replay ordered provider items statelessly, and
record the response-storage policy. Runtime code should project schemas,
parameters, call IDs, results, and telemetry for the chosen surface. It should
not silently drop tools, repeat a known-incompatible surface, or turn a typed
transport mismatch into a text-only planner call.

## 6A. Model configuration scopes

Keep model configuration scopes explicit and independently observable:

- a browser chat preference selects the model requested by that browser; editing
  or probing another model must not silently change the scoped primary or model
  pool
- a user- or organisation-scoped primary is persisted server state and is always
  eligible within that scope
- the enabled model pool expresses routing eligibility, while the represented
  model-selection workflow remains authoritative for the actual choice
- shared server defaults and singleton RAG/index services are separate from
  browser chat preferences and should not silently inherit them

Persist changes through concern-specific, explicit actions. Telemetry and UI
status should distinguish configured, requested, and executed models, including
represented alternates, overrides, fallbacks, and recovered failures. A health
probe that disagrees with a successful live execution should be shown as such,
not collapsed into an unexplained fatal model badge.

## 7. Default routing pattern

A reasonable default pattern is:

1. try the cheapest adequate model for routine bounded work
2. if a miss plausibly reflects model capability, run a bounded comparison
   with a stronger suitable model before encoding semantic recovery in code
3. if an adequate route is too slow or expensive, compare a faster or cheaper
   model and retain the least costly model that preserves the required outcome
4. use stronger models for difficult synthesis, ambiguous planning, or hard
   reasoning, and narrow fine-tunes when a stable frequent task justifies them

Do not hard-code this into many helpers. Centralise it.

Model escalation is not a generic retry. Use available evidence to distinguish
model weakness from tool, authority, transport, context, or harness failure,
and collect only the comparison evidence needed under §7A. A stronger model
that finds the right path but cannot finish inside the latency or cost envelope
is evidence, not a production success; a faster model that produces an adequate
answer may be the better route.

## 7A. Proportionate replay evidence

Use replay evidence before materially changing a shared model policy. Match the
campaign to the claim: a narrow low-risk routing preference may need only a
small representative comparison and easy rollback; a broad release,
high-consequence capability, or research claim may need repeated cases,
holdouts, provenance, and human review.

Report the final user outcome, useful-action and refusal rates, cost, latency,
recovery, and the actual model/tool path. Stage-specific evidence is relevant
only for stages the candidate really uses. No separate certification artefact,
critic workflow, or Vontology mutation choreography is universal.

Promotion must stay within delegated authority and remain observable and
reversible. A passing single turn is bounded evidence, not proof of generality;
equally, the absence of a full certification campaign is not a reason to block
an ordinary reversible experiment.

When a model-specific prompt variant is worth retaining independently, represent
it with lineage and replay evidence rather than hiding it in provider-name
branches. The base prompt remains a usable default when no matching variant
exists.

## 8. Routing and recovery guidance

When a turn uses discovery, continuation, a selector, a direct tool path, or
some combination, preserve user intent and safe solution opportunity:

1. Treat explicit user divergence from an active workflow as authoritative; do
   not force stale continuation context into the next turn.
2. Require actual authoring intent before entering workflow-creation flows. A
   casual mention of a workflow is not sufficient.
3. Do not let execution bookkeeping become the user-facing answer unless the
   user asked for an operational view.
4. Treat a failed specialised route as evidence, not automatically as the end
   of the turn. Try an authorised bounded alternative such as direct tools,
   another workflow, model-led composition, or a manual path before surrender.
5. Do not repair routing regressions with English-specific lexical rescue logic
   or case-specific route forcing. Evaluate the user outcome across varied
   phrasings and allow materially different competent strategies to pass.

## 9. Fine-tuning guidance

Fine-tuning is attractive when:

- the task is stable
- enough data exists
- the desired behaviour is not just prompt phrasing
- the deployment trade-offs are acceptable

Fine-tuning is not a substitute for:

- good prompts
- good retrieval/context
- validators
- clear behavioural authority where durable governance is material

## 10. Acceptance prompts

Use only the items material to the claim:

- a prompt that needs independent durable governance lives in its selected
  authority surface, normally Vontology for Vontology-governed production
  features
- any selector/classifier that actually steers semantic behaviour is governed
  and evaluated proportionately; no selector stage was added merely to satisfy
  this checklist
- prompt or router changes were evaluated on a relevant downstream metric
- the previous baseline remains reproducible
- model and prompt differences are visible in telemetry or task notes
- any material context additions or reductions are visible when they affect the
  claim
- user-facing evaluation cases verify that execution summaries do not displace
  answers
- routing and retrieval improvements do not depend on English-only semantic
  code, and language-agnostic capability claims were checked against at least
  some non-English or otherwise non-canonical phrasings where relevant
- rollback is straightforward
