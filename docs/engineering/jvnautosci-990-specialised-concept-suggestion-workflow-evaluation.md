# JVNAUTOSCI-990: Specialised Concept-Suggestion Workflow Evaluation

## Summary
- Implemented a dedicated Stage-5 workflow: `#V#concept_suggestion_preflight_workflow`.
- The stage runs only when earlier preflight stages are sparse (missing type and/or predicate suggestions).
- Rollout is controlled by `VON_MCP_SPECIALISED_PREFLIGHT_MODE`:
  - `off` (default): disabled.
  - `shadow`: evaluate and emit telemetry without applying suggestions.
  - `active`: apply specialised suggestions to final preflight candidates.

## Inputs, Outputs, and Guardrails
- Inputs:
  - Current user prompt.
  - Baseline type and predicate suggestions from earlier stages.
  - Candidate pools from topic, annotation, RAG, salient predicates, and session memory.
- Outputs:
  - `specialised_type_suggestions`
  - `specialised_predicate_suggestions`
  - evaluation telemetry (mode, invoked, projected gain, recommendation).
- Guardrails:
  - Accept only well-formed `#V#...` concept IDs.
  - Strict de-duplication.
  - Bounded output (`_SPECIALISED_PREFLIGHT_MAX_SUGGESTIONS`).
  - Fail-safe: if workflow errors, baseline suggestions remain authoritative.

## Small Evaluation Set
- Scenario A (shadow mode, sparse baseline):
  - Workflow invoked and produced bounded type/predicate suggestions.
  - Final candidate set remained unchanged.
  - Recommendation emitted as `go_active_trial` when projected gain was positive.
- Scenario B (active mode, sparse baseline):
  - Workflow invoked and merged specialised suggestions into final candidates.
  - Provenance path `specialised_preflight_workflow` recorded in telemetry.
  - Recommendation emitted as `go_adopted`.
- Scenario C (baseline sufficient):
  - Stage remains skipped by design (`specialised_preflight_needed=false`).

## Go/No-Go Recommendation
- Recommendation: **Go**, with staged rollout.
- Rollout sequence:
  1. Enable `shadow` mode and monitor projected gain + error rates.
  2. Promote to `active` when shadow telemetry remains positive and stable.

## Integration and Fallback Plan
- Integration:
  - Built-in workflow definition + action registration.
  - Canonical workflow publication in authority service.
  - Preflight telemetry extended with baseline vs specialised counts and recommendation.
- Fallback:
  - Default `off` keeps existing behaviour unchanged.
  - Any specialised workflow failure degrades cleanly to baseline preflight outputs.
