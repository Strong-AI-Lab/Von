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
- semantic file-copy typing and routing rules;
- minimal-imposition runtime profile policy, write-tool risk classes, and
  scenario/tool overrides;
- knowledge-acquisition profile policy, including predicate priorities,
  confidence thresholds, source adjustments, auto-apply rules, and question
  limits;
- event-binding and workflow-trigger policy;
- recommendation matching, feedback semantics, and delivery wording;
- domain-specific workflow launch policy;
- workflow-description semantics such as domain labels, capability wording,
  maturity/cost, and success-likelihood claims;
- production workflow-family prompt text, discovery exemplars, routing
  metadata, transition/control definitions, required-effect contracts, and
  response templates;
- selected-workflow recovery and handoff policy;
- required-tool operation/evidence/write semantics that affect completion-gate
  blockers or recovery choice;
- integration-specific tool metadata semantics, such as whether a Gmail read
  tool is search evidence or verification evidence.
- talk/presentation representation profiles, including required type sets,
  field-to-predicate bindings, speaker role semantics, and verification
  postconditions.

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
- generic file evidence extraction and typing-result persistence when semantic
  typing/routing policy comes from represented artefacts;
- generic event emission, event payload normalisation, persisted binding lookup,
  workflow instance submission, idempotency, and telemetry;
- generic knowledge-acquisition profile loading, validation, source telemetry,
  confidence arithmetic, and canonical relation mutation support;
- generic required-tool ledger construction, invocation counting, read-back
  evidence preservation, and telemetry when operation classes and policy values
  come from represented tool/workflow metadata;
- generic tool metadata loading, validation, normalisation, merge telemetry, and
  fail-closed diagnostics when represented metadata is absent;
- generic representation-profile executors that load represented talk/presentation
  field bindings and verification contracts, apply them through canonical
  Vontology services, and read back postconditions;
- generic HTTP/request plumbing that passes represented decisions through.

Seed bundles and workflow publisher scripts are migration/publication tooling,
not normal runtime authority after Vontology authority exists.
