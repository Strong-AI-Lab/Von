# Confirmed Non-Issues And Watch Items

This avoids duplicate filing. Re-check if code or live evidence changes.

## Already Covered

- Broad tool metadata/write/evidence authority: `JVNAUTOSCI-1985`.
- Workflow-purity prompt-source/repo-seed drift: `JVNAUTOSCI-2080`.
- Broad buttonify prose-extraction heuristics: completed `JVNAUTOSCI-1971`.
- Regex-heavy person/company/meeting file-copy inference: completed
  `JVNAUTOSCI-1952`; current issue is downstream materialisation contracts,
  now tracked by `JVNAUTOSCI-2216`.

## Inspected, Not Filed On 2026-04-30

- `paper_recommendation_ranking_service.py`: appeared to be a compatibility
  wrapper; filed real policy drift against materialisation/Vontology/delivery.
- `workflow_selection_policy_service.py` and
  `workflow_selection_experience.py`: learned-selection support; file only if
  token features directly override represented routing authority.
- `renderer_applicability_service.py`: renderer aliases; file only with stronger
  evidence of durable semantic/display-profile authority.
- `workflow_override_policy_service.py` and `workflow_continuation_service.py`:
  watch areas; file only if they override represented workflow/routing policy.
- `workflow_launch_input_contracts.py` arXiv ID extraction: covered by
  `JVNAUTOSCI-2212` as deterministic launch-input support. File only if the
  extractor registry grows into phrase-specific or domain launch policy.

## Inspected, Not Filed On 2026-05-01

- `tool_result_hint_actions.py`: generic action support for Vontology-authored
  signal/follow-up hints. File only if integration-specific hint text or
  selection policy appears in Python.
- `workflow_mcp_tool_actions.py`: generic static MCP invocation support with
  namespace and write guardrails. File only if tool choice or domain process
  policy is added there rather than represented workflow metadata.

## Inspected, Not Filed On 2026-05-02

- `conversation_turn_stage_model.py`: current evidence points to stage
  labelling/telemetry support. File only if stage specs start pruning context,
  changing user-facing response semantics, or overriding workflow authority.
- `renderer_applicability_vontology_service.py`: renderer-profile seed
  blueprints are watch-listed, but the stronger live issue is
  `JVNAUTOSCI-2234` for concept-summary panel construction after applicability.
- `scripts/publish_email_resource_link_extraction_workflow.py`: one-shot
  publication script for a Vontology workflow. File only if it becomes a normal
  runtime workflow source or recurring Python workflow authoring pathway.

## Inspected, Not Filed On 2026-05-04

- `scripts/repair_arxiv_email_paper_workflow_consistency.py`: one-shot
  Vontology-authoring repair/audit for the `JVNAUTOSCI-2251` arXiv email
  workflow alignment. File only if it becomes startup/runtime workflow
  authority or a recurring Python workflow-authoring path.
- `src/backend/security/role_resolver.py` plus the organisation-role fallback
  in `src/backend/server/routes/von_routes.py`: old Phase-1 RBAC stub surface,
  already covered historically by `JVNAUTOSCI-787` and future Vontology-driven
  role inheritance in `JVNAUTOSCI-704`. File only if new workflow/KB policy
  expands or depends on those hard-coded mappings after represented membership
  authority exists.
- `src/backend/integrations/internal_mcp/arxiv_proxy_mcp.py`: source-specific
  low-level arXiv MCP adapter and cache/settlement support. File only if
  arXiv fallback/recovery sequencing or user-facing paper policy moves from the
  wrapper workflow back into this adapter.

## Inspected, Not Filed On 2026-05-05

- `src/backend/workflows/prompt_metadata_resolution.py` and
  `src/backend/workflows/llm_step_executor.py` model-family prompt variants:
  current evidence is generic support for Vontology-authored prompt variant
  concepts and telemetry. File only if model-family/capability semantics or
  prompt wording become Python-authored rather than represented metadata.
- `src/backend/services/concept_predicate_metadata_service.py` structural
  predicate fallback/defaults: do not file a duplicate; update
  `JVNAUTOSCI-1957` unless new evidence shows a separate predicate-policy
  surface.
- `src/backend/services/workflow_event_integration_service.py` episode
  evaluation autotrigger/depth guards: existing actor/critic/event-trigger work
  already covers this family historically. File only with fresh evidence that a
  specific trigger policy cannot be expressed by persisted bindings/VWL and is
  not covered by `JVNAUTOSCI-1605` or later follow-ups.

## Inspected, Not Filed On 2026-05-06

- `src/backend/workflows/model_execution_budget_policy.py`: current evidence is
  Vontology text-relation loading, bounded coercion, and telemetry for model
  execution budget hints. File only if cost/locality/latency semantics or model
  preference policy become Python-authored rather than represented model
  metadata.
- `src/backend/workflows/durable/model_selection_workflow.py` and
  `src/backend/services/workflow_model_selection_workflow_vontology_service.py`:
  watch as a small deterministic subworkflow bootstrap. Current evidence points
  to generic candidate-pool/policy-resolution support for represented workflow
  model policy. File if the Python authoring spec grows prompts, stage policy,
  workflow-specific model choices, or user-facing model-selection semantics.
- Paper recommendation policy authority after `JVNAUTOSCI-2194`: represented
  policy loading is materially improved. The remaining embedding-only fallback
  and prompt-unavailable path is not a new duplicate today because
  `JVNAUTOSCI-2194`, `JVNAUTOSCI-1679`, and `JVNAUTOSCI-2199` already cover
  the broader recommender authority/fail-closed family. Re-file only with fresh
  live evidence that the current fallback sends or materialises recommendations
  contrary to represented prompt/workflow policy.

## Inspected, Not Filed On 2026-05-07

- `scripts/repair_jvnautosci_2272_zhan_gmail_arxiv_ingestion_workflow.py`:
  one-shot Vontology-authoring repair for a specific Gmail/arXiv workflow
  family. It contains domain workflow descriptions, retry policies, launch
  contracts, and Gmail follow-up hint rewriting, but it is not startup/runtime
  authority. File only if this pattern becomes a recurring workflow-family
  publisher, template, or request-path authority.
- `_build_recovery_retry_launch_inputs` in
  `src/backend/workflows/durable/turn_execution_actions.py`: still a watch item
  for arXiv/file-copy launch-input projection, but current evidence is already
  covered by the multi-target/recovery and arXiv launch-input task family
  (`JVNAUTOSCI-1873`, `JVNAUTOSCI-1874`, `JVNAUTOSCI-2251`). File only with a
  fresh, narrower live failure not covered there.

Do not file solely because a file is large or contains domain nouns. File when
Python owns durable decision policy, evidence choice, prompt/workflow content,
user-visible copy, or KB/profile semantics.
