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
- representation candidate schemas, field-to-predicate bindings, note relation
  policy, and materialisation success/postcondition criteria;
- workflow-authoring representation profiles, domain field schemas,
  type/predicate bindings, candidate-resolution policy, and generated workflow
  postconditions;
- concept-summary renderer profiles, including field choice, section grouping,
  labels, panel ordering, empty states, and concept-family type exclusions;
- semantic file-copy typing and routing rules, including type candidates,
  route-hint taxonomy, thresholds or prompt-classifier policy, and downstream
  workflow applicability;
- minimal-imposition runtime profile policy, including decision rules,
  write-tool risk classes, and scenario/tool overrides;
- knowledge-acquisition profile policy, including predicate priorities,
  confidence thresholds, source adjustments, auto-apply rules, and question
  limits for relation-completion workflows;
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
- generic materialisation executors that apply represented field bindings and
  relation specs through canonical Vontology services;
- generic entity-representation primitives that execute represented profile
  specs without hard-coded domain action ids or predicate defaults;
- generic concept-summary rendering executors that apply represented panel
  specs and fetch requested Vontology fields;
- generic file evidence extraction and typing-result persistence when the
  semantic typing/routing policy comes from represented artefacts;
- generic knowledge-acquisition profile loading, validation, source telemetry,
  confidence arithmetic, and canonical relation mutation support when the
  policy values come from represented profile artefacts;
- generic HTTP/request plumbing that passes represented decisions through.

Seed bundles are publication/migration artefacts, not normal runtime authority
after Vontology authority exists.
