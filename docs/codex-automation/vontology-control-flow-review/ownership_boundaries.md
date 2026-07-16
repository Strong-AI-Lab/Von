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
- Source-to-representation convergence schedule/profile authority, including
  source account/profile, actor namespace, source query, cadence, item budgets,
  launch prompt, convergence gates, and target workflow selection.
- Maintenance audit authority, including audited task classes, expected
  workflows, case prompts, thresholds, evaluation dimensions, suggestion policy,
  and self-improvement handoff criteria.
- Model-visible context-framing templates for synthesiser or stage-local LLM
  context, including active-request labels, section headings, and hint-wrapper
  wording. Python may bind values into represented templates but should not own
  the durable wording the model sees.
- Durable conversation-turn discovery and selector-preparation policy,
  including fallback applicability, candidate ranking, support-workflow
  demotion, first-match selection, and selector prompt/fallback wording.
- Presenter/debug progress and evidence-summary wording, including nested
  workflow read-back labels, blocker descriptions, domain target labels, and
  model-visible section headings marked as authoritative.
- Surfaceable concept-handle evidence roles and labels, including which result
  fields count as created/materialised/represented handles, artefact-type
  labels, source-specific provenance fields, and response/presenter wording for
  those handles.
- Turn-contract dispatch recovery authority, including whether required
  evidence should keep the selected workflow, override to the tool pipeline,
  recover to a concrete workflow-execute target, fail closed, or request
  clarification.
- Operational diagnostics workflow authority, including maintenance workflow
  graph topology, transitions, tool-invocation inputs, context mappings,
  schedule/launch policy, and completion/failure routes.
- Selector fallback and recovery authority, including when a failed/default
  selector result may be recovered to a specialised workflow, which candidate
  is eligible, and what reasoning or recovery explanation is exposed.
- Model-policy graph authority, including workflow-stage aliases, primary and
  fallback model defaults, fallback-hop limits, inheritance, compatibility
  modes, local-only policy semantics, and any defaults used when represented
  model-policy data is incomplete.
- Operational certification and learning-release policy, including trial
  counts, pass-window expectations, certification gate requirements, release
  eligibility verdicts, promotion/rejection/rollback judgement, and release
  transition gate sets.
- Prior-turn evidence and obligation carry-forward policy, including whether
  context is discourse-only, expected-outcome evidence, routing evidence,
  tool-planning evidence, answer evidence, continuation/resume/verification, or
  suppressed for the current turn.
- Representation required-effects authority, including domain profile selection,
  required tool sets, payload/read-back fields, postcondition labels, failure
  code meanings, and completion-blocking policy for paper/person/company/meeting
  or future representation domains.

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
- generic schedule/profile loaders and schedule persistence support when actor,
  source, cadence, budgets, and launch defaults come from represented authority;
- generic maintenance-audit evidence collection, workflow discovery probes,
  inventory reads, metric arithmetic, payload validation, and episode-memory
  persistence when audit cases, thresholds, and suggestion policy come from
  represented authority;
- generic synthesiser/context-prep plumbing that resolves represented tool
  hints, extracts active-turn values from shared context, binds those values
  into represented templates, validates the resulting messages, and records
  context-lineage telemetry;
- generic durable turn-discovery plumbing that passes through existing discovery
  payloads, invokes canonical discovery services/actions, validates structured
  discovery results, and emits fail-closed diagnostics when discovery authority
  is unavailable;
- generic presenter/debug evidence projection support that traverses bounded
  payloads, redacts/validates fields, applies represented evidence-view or
  presenter contracts, and records telemetry without authoring domain wording;
- generic concept-handle projection support that loads represented
  evidence-view/presenter metadata, validates concept IDs, bounds payload size,
  preserves source paths/contract IDs, and emits telemetry without Python-owned
  paper/file-copy/arXiv labels or field-name policy;
- generic prompt/template rendering services that resolve Vontology prompt
  concepts, validate represented template schemas, bind workflow/runtime values,
  record source concept metadata, and fail closed when represented model-visible
  wording is missing;
- generic LLM-call foreground prompt plumbing that passes the raw user request
  to a Vontology-authored stage prompt without adding selector policy,
  examples, fallback copy, or domain hints;
- generic model-call diagnostics that report the concrete effective
  client/model/provider/host already selected by represented settings or runtime
  configuration;
- generic dispatch-preflight plumbing that loads represented policy, validates
  required-tool/evidence metadata, applies represented route-state decisions,
  records stable reason codes, and fails closed when policy authority is absent;
- generic diagnostic tool wrappers, result redaction, report compaction,
  finalise actions, telemetry, and bounded MCP invocation support when the
  diagnostic workflow graph/control policy comes from represented VWL;
- generic selector fast-path/recovery evaluators that validate represented
  policy metadata, candidate coverage, and authority provenance, then apply the
  represented decision or fail closed without authoring fallback policy;
- generic model-policy graph loaders that resolve represented stage/policy
  metadata, validate and normalise types, preserve source concept provenance,
  and fail closed or report incomplete authority instead of supplying hidden
  stage/default policy;
- generic operational-certification support that parses represented benchmark
  and release contracts, computes requested aggregates, validates matcher/budget
  shapes, digests evidence, checks live Vontology/source/read-back provenance,
  enforces actor scope and authenticated approval, persists release state, and
  exposes typed blockers/recovery affordances;
- generic prior-turn context plumbing that carries structured represented
  carry-forward directives, preserves source-turn lineage, suppresses fields
  exactly as directed, records telemetry, and fails closed when the represented
  directive is missing or invalid;
- generic required-effects plumbing that materialises represented effect
  contracts, validates tool invocation payloads against represented
  `required_payload_fields`, preserves typed blockers, and fails closed when
  representation contract/profile authority is unavailable rather than adding
  new domain defaults;
- generic HTTP/request plumbing that passes represented decisions through.
- read-only operational diagnostics and Jira triage tooling that computes
  bounded engineering metrics, redacts evidence, and prepares human-review
  tasks, provided it does not become recurring workflow policy, user-facing Von
  answer semantics, or prompt/model/workflow learning authority.

Seed bundles and workflow publisher scripts are migration/publication tooling,
not normal runtime authority after Vontology authority exists.
