# Ownership Boundaries

Operational memory only. It does not override `AGENTS.md` or current code
evidence.

## Represented Authority

Vontology, VWL workflows, prompt concepts/text relations, represented profiles,
predicates, and KB assertions should own durable:

- workflow selection/control policy;
- prompt bodies and model-facing behavioural rules;
- profile applicability and field semantics;
- representation contracts and required evidence/tool policy;
- recommendation matching, feedback semantics, and delivery wording;
- domain-specific workflow launch policy;
- workflow-description semantics such as domain labels, capability wording,
  maturity/cost, and success-likelihood claims.

## Python Support

Python can remain the surface for:

- schema validation, serialisation, and structural output filtering;
- prompt/workflow/profile loading with fail-closed diagnostics;
- telemetry, provenance, persistence plumbing, and gateway/tool invocation;
- embedding/vector execution primitives;
- generic HTTP/request plumbing that passes represented decisions through.

Seed bundles are publication/migration artefacts, not normal runtime authority
after Vontology authority exists.
