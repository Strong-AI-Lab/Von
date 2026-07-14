# Prompt Programs and Model Routing Playbook

## 1. When to read this

Read this document before planning or implementing work that touches:

- prompt content or prompt rendering
- model selection or routing
- prompt optimisation
- fine-tuning or adapter selection
- evaluation of prompt- or model-governed behaviour

## 2. Core doctrine

Von prompt programs are operational policy. They should be treated as:

- first-class artefacts
- versioned and inspectable
- evaluated against downstream task metrics
- eligible for optimisation and rollback

Model selection is also policy. It should not be hidden in scattered conditionals or copied helper logic.

## 3. Prompt program rules

1. Store authoritative prompt content in Vontology text relations.
2. Give important prompt concepts stable identities and descriptions.
3. Maintain prompt lineage when revising or optimising prompts.
4. Keep evaluation notes with the prompt or linked task.
5. Do not silently override a Vontology-governed prompt in code.
6. Do not treat a prompt change as harmless copy-editing when it changes behaviour.

Selector/classifier prompts are not second-class helper prompts. If they steer
semantic routing or stage behaviour, they are prompt-programme authority
surfaces and should be governed, versioned, and published with the same
discipline as response, narration, and recovery prompts.

## 3A. Shared turn-context discipline

For multi-stage turns, the accumulated turn context is itself part of the
behaviour contract.

Default to one shared turn-context object reused across selector, planner,
tool-use, and response stages. Stage prompts may add instructions or evidence,
but should not silently replace the underlying context unless a narrower
context is explicitly required, justified, and evaluated.

If code quietly gives different stages materially different contexts, Python
has become a hidden policy layer even when the prompt text still lives in
Vontology.

Telemetry should expose:

- the effective stage context summary
- any stage-local additions or reductions
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
2. escalate when confidence, validation, or complexity signals require it
3. use stronger models for difficult synthesis, ambiguous planning, or hard reasoning
4. use narrow fine-tunes when the task is stable and frequent enough to justify them

Do not hard-code this into many helpers. Centralise it.

## 7A. Replay-Backed Model Certification

Cheap/local model use should be certified per workflow stage, prompt profile,
and replay set. A passing single turn is evidence, not authority to update the
runtime policy.

For `JVNAUTOSCI-1894`-style replay work, the live sampler emits
model-portfolio evidence alongside the normal response and telemetry summary:

- selector evidence: selected workflow, raw selector response capture,
  structured-output validity, selected model-policy candidate, and fallback
  metadata;
- answer evidence: final answer length/usefulness, missing required evidence,
  empty-success suspect calls, completion-gate status, critic status, tool
  count, and timing rows;
- certification decision: whether the evidence is sufficient to promote a
  model-stage certification.

The certification decision is deliberately conservative. The sampler must not
mutate workflow model policy by itself. Promotion requires represented evidence
with replay provenance, enough distinct cases, passing stage evidence, and a
separate Vontology/model-policy mutation path. A strong critic workflow or
human review should decide whether to promote, expire, or reject the
certification.

When a model-specific prompt variant appears useful, represent it as a prompt
concept with lineage and replay evidence. Do not hide the variant in Python
branches keyed by provider or model name.

Model- or model-family-specific prompt variants should be linked from the base
prompt concept through represented Vontology metadata. Runtime support may
select variants by exact model, then model family or capability profile, but
the base prompt remains the default. If no matching variant exists, execution
must continue with the base prompt and record the fallback in telemetry rather
than treating the missing variant as a turn failure.

## 8. Workflow routing guardrails

When workflow discovery, continuation, and selector preparation interact, keep
the following policy constraints intact:

1. Preserve discovery-side routing eligibility and exclusion reasons into
   selector preparation. Do not overwrite them with later executability or
   registry defaults.
2. Apply routing-profile policy to normal discovered candidates as well as to
   custom override candidates. An authoring workflow is still an authoring
   workflow when found through ordinary discovery.
3. Treat explicit user divergence from an active workflow continuation as
   authoritative. If the user says the current workflow is wrong, asks for
   manual inspection, or explicitly forbids continuing it, do not keep forcing
   the stale workflow context into the next routing turn.
4. Require explicit authoring intent before routing into workflow-creation or
   workflow-authoring flows. Casual mention of “workflow”, or discussion of what
   workflow might eventually be needed, is not sufficient.
5. Default to the same accumulated turn context across selector-adjacent stages
   and later answer-generation stages. Stage-local prompt layers may add to
   that context, but silent phase-specific thinning is an architectural smell
   that requires explicit justification and telemetry.
6. Do not let workflow execution bookkeeping become the user-facing answer.
   Completion reports, dispatch summaries, and renderer diagnostics are
   supporting surfaces unless the user explicitly asked for an operational
   view.
7. Treat a failed specialised workflow as a routing event, not automatically
   as the end of the turn. When a specialised route fails before meaningful
   tool progress or verified durable effects, the default next step should be
   bounded compositional recovery from accumulated turn context, such as the
   general tool workflow or an authorised direct tool batch, before
   workflow-gap escalation or user-facing surrender. Telemetry should preserve
   both the failed specialised attempt and the recovery handoff.
8. Do not repair simple routing regressions with English-specific lexical
   rescue logic. Lower-complexity replay cases should succeed for the same
   discovery, selector, workflow, and tool-planning reasons that richer
   compositional turns would succeed, and that success path should remain valid
   in any language rather than only for English phrasings.

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
- explicit workflow policy

## 10. Acceptance checklist

Before closing prompt- or routing-related work, verify:

- the authoritative prompt lives in Vontology
- selector/classifier prompts are under the same authority and publishing
  discipline as other prompt artefacts
- prompt or router changes were evaluated on a relevant downstream metric
- the previous baseline remains reproducible
- model and prompt differences are visible in telemetry or task notes
- any stage-specific context additions or reductions are visible in telemetry
  and justified in task notes when non-obvious
- user-facing evaluation cases verify that execution summaries do not displace
  answers
- routing and retrieval improvements do not depend on English-only semantic
  code, and language-agnostic capability claims were checked against at least
  some non-English or otherwise non-canonical phrasings where relevant
- rollback is straightforward
