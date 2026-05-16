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
- mail/profile resource authority, including resource/profile vocabulary,
  authorised/default profile predicates, runtime-alias facts, and user-to-profile
  relationships that workflows use before calling mail tools.
- maintenance/introspection workflow graphs and policy, including diagnosis
  causes, repair selection, prompt guardrail wording, Jira remediation wording,
  transition/control definitions, and tool-family/domain semantics.
- episode self-improvement profiles, including profile concept/link authority,
  launch budget, priority ordering, target-surface eligibility, dedupe identity,
  benchmark evidence budget, and profile-description policy.
- Jira task import workflow graphs, including step topology, transition
  reasons, action contracts, launch inputs, lifecycle metadata, and any
  schedule/event binding policy.
- Background KB-enrichment policy, including target language sets, candidate
  eligibility thresholds, confidence gates, mutation budgets, schedule cadence,
  reanalysis windows, and alias/normalisation choices that affect which facts
  are written.
- Replay/evaluation authority, including replay case prompt banks, expected
  tool/knowledge-surface requirements, answer-quality rubrics, failure-marker
  semantics, prompt/model promotion blockers, policy-update criteria,
  pass/partial/fail verdict definitions, and experiment observation meaning.
- Episode evaluator authority, including evaluator axes, improvement categories,
  target surfaces, priority meanings, benchmark proxy definitions, audit bucket
  ordering, recommendation wording, and promotion-impact interpretation.
- Source-ingestion profile authority, including source-family concept ids,
  source-system labels, adapter/profile bindings, document and file-copy type
  ids, predicate vocabulary, and per-environment profile facts that determine
  how imported information-bearing objects are represented.
- Benchmark/rubric authority, including benchmark suites/cases, expected
  outcomes, coverage tags, expected authority sources, execution modes, strategy
  labels, retention priorities, signal categories, and pass/fail rubric meaning.
- Required-tool obligation semantics, including operation class, evidence role,
  target-bearing input/output fields, target-closure requirements, and read-back
  policy for required tools.

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
  evidence preservation, target extraction from represented field specs, and
  telemetry when operation classes and policy values come from represented
  tool/workflow metadata;
- generic tool metadata loading, validation, normalisation, merge telemetry, and
  fail-closed diagnostics when represented metadata is absent;
- generic representation-profile executors that load represented talk/presentation
  field bindings and verification contracts, apply them through canonical
  Vontology services, and read back postconditions;
- generic resource-profile materialisers that validate represented profile specs,
  safely expose non-secret runtime aliases, and fail closed when the represented
  resource/profile authority is absent;
- generic maintenance/introspection support actions that gather evidence,
  invoke bounded MCP tools, apply explicitly represented text-relation or Jira
  operations, validate outputs, and emit telemetry;
- generic episode self-improvement profile loading, validation, normalisation,
  application, and fail-closed diagnostics when represented profile authority is
  absent or invalid;
- generic Jira task import action support that coerces workflow context into
  `JiraTaskMigrationOptions`, invokes the shared migration runner, returns
  structured outputs, and emits diagnostics;
- generic background schedule persistence and action support that applies
  represented enrichment policy, validates numeric/list bounds, invokes prompts,
  writes through canonical Vontology services, and emits fallback diagnostics;
- generic replay/evaluation launchers, telemetry collectors, bounded evidence
  projections, structural consistency checks, and canonical experiment writes
  when case expectations, rubrics, verdicts, and promotion policy come from
  represented authority;
- generic episode-critic evidence collection, critique-memory persistence,
  payload validation, projection storage, and benchmark aggregation when axes,
  proxy definitions, recommendation/rubric policy, and improvement semantics
  come from represented evaluator artefacts;
- generic source-ingestion adapters, local file discovery, hashing/idempotency,
  upload/write mechanics, profile loading/validation, and telemetry when source
  profile/type/predicate authority comes from Vontology;
- generic benchmark loaders/reporters, telemetry joins, arithmetic, validation,
  and serialisation when benchmark suites, cases, labels, and rubrics come from
  represented authority;
- generic HTTP/request plumbing that passes represented decisions through.

Seed bundles and workflow publisher scripts are migration/publication tooling,
not normal runtime authority after Vontology authority exists.
