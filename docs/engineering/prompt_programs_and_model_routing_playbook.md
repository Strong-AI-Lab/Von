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

## 7. Default routing pattern

A reasonable default pattern is:

1. try the cheapest adequate model for routine bounded work
2. escalate when confidence, validation, or complexity signals require it
3. use stronger models for difficult synthesis, ambiguous planning, or hard reasoning
4. use narrow fine-tunes when the task is stable and frequent enough to justify them

Do not hard-code this into many helpers. Centralise it.

## 8. Fine-tuning guidance

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

## 9. Acceptance checklist

Before closing prompt- or routing-related work, verify:

- the authoritative prompt lives in Vontology
- prompt or router changes were evaluated on a relevant downstream metric
- the previous baseline remains reproducible
- model and prompt differences are visible in telemetry or task notes
- rollback is straightforward
