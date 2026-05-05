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
- After prompt-backed interpretation migrations, inspect the downstream
  materialisation contract separately: Python candidate schemas,
  field-to-predicate mappings, note text, and success criteria can remain
  hidden policy even when extraction/inference is prompt-owned.
- In generic workflow-authoring surfaces, domain action ids such as
  `workflow_authoring.resolve_<domain>`, hard-coded type/predicate defaults,
  domain profile fields, and tests asserting those action ids are strong
  represented-authority drift signals.
- User-visible renderer services that query Vontology metadata but then branch
  on renderer IDs or domain concept families to choose section labels, field
  ordering, expanded panels, or type exclusions are information-policy drift.
- File-copy typing/routing services need a split read: MIME/extension evidence
  extraction can be support logic, but semantic categories such as scholarly
  paper, CV, business card, meeting transcript, or email message are represented
  workflow/profile policy when they influence routing or persisted KB facts.
- Vontology profile services that fall back at runtime to canonical Python
  default decision policies, tool-risk classes, or profile blueprints are
  authority seams even when they also materialise those defaults into KB.
- Predicate-specific profile tables such as `*_PRIORITY_POLICY`,
  `*_AUTO_APPLY_POLICY`, source-adjustment maps, or confidence thresholds are
  strong drift signals when normal workflow execution can load, merge, or
  repair them from Python.
- Event-emission helpers that filter process events by hard-coded or env-var
  status/type allow-lists before persisted event bindings or VWL conditions run
  are workflow-control drift. Python should emit structured events and let
  represented binding/workflow policy decide relevance.
- Vontology-backed metadata services that merge Python defaults when authority
  is missing or partial should be checked against existing migration tasks
  before filing. The remaining defect is often fail-open fallback, not absence
  of a service.
- Standalone publisher scripts are drift candidates when they embed full
  production workflow families: prompt text, discovery exemplars, routing
  metadata, transition/control definitions, required-effect contracts, or
  response templates. Treat one-shot migration/export helpers separately, but
  file when the script is the recoverable workflow source or is advertised as a
  future workflow-family template.

## 2026-04-30 Examples

- Representation profile blueprints: `JVNAUTOSCI-2193`.
- Paper recommendation policy: `JVNAUTOSCI-2194`.
- Onboarding launch/process policy: `JVNAUTOSCI-2195`.
- Deterministic workflow-description authoring: `JVNAUTOSCI-2196`.
- Buttonify prompt suffix: `JVNAUTOSCI-2197`.
- File-copy entity materialisation contract bindings: `JVNAUTOSCI-2216`.
- PhD-student workflow-authoring action contracts and handlers:
  `JVNAUTOSCI-2219`.
- Concept-summary renderer panel policy: `JVNAUTOSCI-2234`.
- File-copy semantic typing and route hints: `JVNAUTOSCI-2235`.
- Represented-artefact repo-seed/startup authority: `JVNAUTOSCI-2260`.
- Knowledge-acquisition profile predicate priorities and thresholds:
  `JVNAUTOSCI-2262`.
- Task-status event trigger status filtering before represented event bindings:
  `JVNAUTOSCI-2264`.
- Python-authored KR materialisation workflow publisher:
  `JVNAUTOSCI-2271`.
