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

## 2026-04-30 Examples

- Representation profile blueprints: `JVNAUTOSCI-2193`.
- Paper recommendation policy: `JVNAUTOSCI-2194`.
- Onboarding launch/process policy: `JVNAUTOSCI-2195`.
- Deterministic workflow-description authoring: `JVNAUTOSCI-2196`.
- Buttonify prompt suffix: `JVNAUTOSCI-2197`.
- File-copy entity materialisation contract bindings: `JVNAUTOSCI-2216`.
- PhD-student workflow-authoring action contracts and handlers:
  `JVNAUTOSCI-2219`.
