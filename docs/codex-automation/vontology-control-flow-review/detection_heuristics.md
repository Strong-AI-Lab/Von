# Detection Heuristics

Use these as prompts, not mechanical rules. Confirm against current code and
represented authority before filing Jira work.

## Strong Signals

- Workflow-purity deltas: update `JVNAUTOSCI-2080` before creating duplicates.
- Python `*_BLUEPRINTS`, `*_CONFIGS`, `*_FIELDS`, `*_ALIASES`, `*_PATTERNS`, or
  `*_POLICY` tables that contain durable domain, prompt, workflow, evidence, or
  feedback semantics.
- Python-owned user-visible recommendation, recovery, fallback, review, or
  delivery wording.
- Domain-specific evidence selection for matching/ranking/recommendation.
- Domain-specific HTTP routes or legacy `src/workflows` stubs that choose
  workflow ids or encode organisational processes.
- Prompt text appended to Vontology-rendered prompts in Python.
- Python-generated Vontology descriptions containing domain labels, capability
  wording, cost, maturity, or success-likelihood claims.
- Prompt-backed interpretation migrations need a downstream materialisation
  read: Python candidate schemas, field-to-predicate mappings, note text, and
  success criteria can remain hidden policy.
- Generic workflow-authoring surfaces with domain action ids such as
  `workflow_authoring.resolve_<domain>`, hard-coded type/predicate defaults,
  domain profile fields, or tests asserting those action ids.
- Renderer services that query Vontology metadata but branch on renderer IDs or
  domain concept families for labels, field ordering, expanded panels, or type
  exclusions.
- File-copy typing/routing services: MIME/extension evidence extraction can be
  support, but semantic categories that influence routing or KB facts are
  represented workflow/profile policy.
- Vontology-backed metadata/profile services that merge Python defaults when
  authority is missing or partial. The defect is often fail-open fallback, not
  absence of a service.
- Event-emission helpers that filter process events by hard-coded or env-var
  status/type allow-lists before persisted bindings or VWL conditions run.
- Publisher scripts that embed production workflow prompt text, discovery
  exemplars, routing metadata, transition/control definitions, required-effect
  contracts, or response templates.
- Extracting workflow/recovery control out of a monolith into a dedicated
  Python service is not enough if the new service still decides fallback mode,
  retry/handoff reason, or user-facing recovery wording.
- Required-tool ledgers are support surfaces, but operation classes and
  evidence/write semantics should come from represented tool metadata or
  workflow contracts. Prefix classifiers such as `search_`, `create_`, `get_`,
  and `workflow_` are a strong signal to update the tool-metadata authority
  track.
- Tests that monkeypatch Vontology metadata loading to `{}` and then assert
  integration-specific evidence, write, operation, or completion-gate semantics
  are strong evidence of hidden Python fallback authority.
- Domain-specific durable action services that normalise named profiles and map
  those profiles to required ontology types, field-to-predicate writes, or
  verification criteria are strong drift candidates even when the surrounding
  workflow graph is Vontology-backed.
- Runtime services that create integration-specific resource/profile vocabulary,
  predicates, user-authorisation/default relationships, or concept IDs from
  configured aliases are strong drift candidates. A represented workflow
  consuming those facts does not make Python the correct source of the facts.
- Durable modules that build `WorkflowDefinition(...)` graphs, register them as
  `source="built_in"`, and also own diagnosis, repair, or remediation wording
  are strong drift candidates. Even "maintenance" workflows should be
  Vontology-stored VWL; Python should expose only reusable actions and
  telemetry.
- Vontology-backed profile materialisers that create or repopulate canonical
  profile JSON from Python defaults/blueprints remain drift candidates even
  after the live consumer reads a represented profile. A hard-coded default
  profile id fallback is especially suspect when missing linkage should fail
  closed.
- Test or bootstrap helpers that publish Vontology workflow graphs from
  Python `WorkflowRegistration(source="built_in")` definitions are still drift
  candidates when the helper is the only reproducible source for the graph,
  transitions, or action mappings. A later runtime load from Vontology does not
  prove independent VWL authority if the Vontology graph was generated from the
  Python builder.
- Background workflow schedule bootstraps are authority surfaces when they set
  default workflow inputs that determine entity eligibility, target languages,
  confidence thresholds, mutation budgets, cadence, or reanalysis windows. Env
  overrides do not make those defaults represented policy.
- Replay/evaluation tooling becomes an authority surface when Python prompt
  banks, response-marker lists, required-tool lists, answer-quality booleans,
  promotion blockers, pass/partial/fail verdicts, or experiment observations
  feed prompt/model/workflow learning. Smoke-test launchers and evidence
  collectors can be support-only, but durable rubrics and case expectations
  should be represented.
- Evaluator and critique-memory normalisers can be support-only when they
  validate represented payloads, but Python alias/default tables for evaluator
  axes, improvement categories, target surfaces, priority values,
  false-positive/false-negative proxies, recommendation text, or audit-bucket
  ordering are evaluator-policy evidence. Check `JVNAUTOSCI-2024` before
  creating a new task.
- Source-ingestion services are drift candidates when they define durable
  ontology source profiles in Python: source-family concept ids, per-environment
  document/file-copy types, predicate ids, profile names, source-system labels,
  and default profile catalogues. Adapter discovery and idempotent writes can be
  support-only, but represented profile authority should live in Vontology.
- Benchmark services are drift candidates when they define outcome labels,
  expected routes, coverage tags, expected authority sources, execution modes,
  strategy names, retention priorities, or signal/rubric categories in Python or
  repo seed bundles. Metric arithmetic can remain Python support, but the
  benchmark case/rubric authority should be represented.
- Required-tool obligation ledgers are support surfaces, but Python prefix
  classifiers and payload-key lists that decide operation class, evidence role,
  target identity, or target-closure requirements are drift candidates. Keep the
  counting/telemetry in Python and move semantic bindings to represented tool or
  workflow metadata.
- Source-to-representation convergence schedule bootstraps are authority
  surfaces when Python/env defaults set actor namespace, source profile, query,
  cadence, max results, launch prompt, or workflow id. Those values determine
  what organisational evidence is processed and should come from represented
  schedule/profile authority.
- Maintenance/audit workflows remain drift candidates when repo seed profiles
  define benchmark-like cases, expected workflows, thresholds, evaluation
  dimensions, or suggestion policy, especially if the completed task promised
  that repo fixtures are non-authoritative. Python can run probes and compute
  metrics, but the case/rubric/profile authority should be represented.
- Generic synthesiser/context-prep actions are still drift candidates when they
  assemble LLM-visible system-message framing from Python string literals.
  Resolving Vontology-authored hint bodies is not enough if the wrapper labels,
  active-request framing, or tests pin the model-visible template in code.

## Recent Examples

- `JVNAUTOSCI-2271` - Python-authored KR materialisation workflow publisher.
- `JVNAUTOSCI-2275` - selected-workflow recovery handoff policy.
- `JVNAUTOSCI-2284` - Gmail read-tool evidence-role defaults.
- `JVNAUTOSCI-2291` - talk/presentation representation profile and verification
  policy in Python.
- `JVNAUTOSCI-2296` - mail-profile resource vocabulary, predicates, alias
  concepts, and user/default-profile facts authored by Python materialisation.
- `JVNAUTOSCI-2302` - workflow-introspection maintenance workflow graph,
  diagnosis/repair policy, prompt patch text, and Jira remediation wording in
  Python.
- `JVNAUTOSCI-2311` - episode self-improvement profile concept ids, canonical
  launch/benchmark policy payloads, workflow links, and fail-open default
  profile resolution in Python.
- `JVNAUTOSCI-2315` - Jira incremental import workflow graph and transition
  policy published from a Python `source="built_in"` registration rather than
  independently authored Vontology-stored VWL.
- `JVNAUTOSCI-2316` - multilingual concept-enrichment target languages,
  candidate thresholds, confidence gate, mutation budget, and managed schedule
  defaults in Python/env defaults rather than represented workflow/profile
  policy.
- `JVNAUTOSCI-2324` - replay sampler/evaluation prompt bank, response-marker
  rubric, user-happiness scoring, promotion blockers, pass/partial/fail
  verdicts, and experiment observation policy in Python rather than represented
  replay-evaluation authority.
- `JVNAUTOSCI-2024` - episode evaluator criteria, benchmark proxy signals,
  recommendations, and critic policy should be authority-backed evaluator
  artefacts rather than Python proxy tables.
- `JVNAUTOSCI-2326` - AI coding-session ingestion source profiles and ontology
  type/predicate catalogues in Python.
- `JVNAUTOSCI-2327` - selector/context benchmark rubrics and coverage/source
  categories in Python/repo seed bundles.
- `JVNAUTOSCI-2329` - required-tool operation classes and target-closure
  semantics inferred from Python tool-name/payload-key heuristics rather than
  represented tool/workflow metadata.
- `JVNAUTOSCI-2340` - email-source representation convergence workflow/schedule
  authority still seeded from repo/Python, including Zhan Gmail/arXiv defaults.
- `JVNAUTOSCI-2341` - representation-routing audit profile/prompt/workflow
  authority, cases, thresholds, and test workflow graph still in repo/Python
  surfaces.
- `JVNAUTOSCI-2350` - synthesiser-context-prep active-request/tool-hint framing
  strings and exact-message tests live in Python rather than represented
  prompt/context-template authority.
