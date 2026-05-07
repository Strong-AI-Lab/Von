# Confirmed Non-Issues And Watch Items

This avoids duplicate filing. Re-check if code or live evidence changes.

## Already Covered

- Broad tool metadata/write/evidence authority: `JVNAUTOSCI-1985`.
- Workflow-purity prompt-source/repo-seed drift: `JVNAUTOSCI-2080`.
- Broad buttonify prose-extraction heuristics: completed `JVNAUTOSCI-1971`.
- Regex-heavy person/company/meeting file-copy inference: completed
  `JVNAUTOSCI-1952`; downstream materialisation contracts are tracked by
  `JVNAUTOSCI-2216`.
- Current paper recommender residual fallback is covered by `JVNAUTOSCI-2194`,
  `JVNAUTOSCI-1679`, and `JVNAUTOSCI-2199` unless fresh live evidence shows a
  current fail-open user impact.
- arXiv/file-copy launch-input projection is covered by `JVNAUTOSCI-1873`,
  `JVNAUTOSCI-1874`, `JVNAUTOSCI-2212`, and `JVNAUTOSCI-2251` unless a fresh,
  narrower live failure appears.

## Watch, Not Filed

- `workflow_selection_policy_service.py`,
  `workflow_selection_experience.py`, and selector
  `routing_policy_fragments`: learned-selection support/evidence formatting.
  File only if Python token features, formatter rules, or recovery wording
  override represented routing or prompt authority.
- `renderer_applicability_service.py`: renderer aliases; file only with strong
  evidence of durable semantic/display-profile authority.
- `workflow_override_policy_service.py` and `workflow_continuation_service.py`:
  file only if they override represented workflow/routing policy.
- `tool_result_hint_actions.py`: generic action support for Vontology-authored
  signal/follow-up hints. File only if integration-specific hint text or
  selection policy appears in Python.
- `workflow_mcp_tool_actions.py`: generic static MCP invocation support with
  namespace/write guardrails. File only if tool choice or domain process policy
  is added there rather than represented workflow metadata.
- `conversation_turn_stage_model.py`: telemetry/stage labelling support. File
  only if stage specs prune context, alter user-facing semantics, or override
  workflow authority.
- `scripts/publish_email_resource_link_extraction_workflow.py`,
  `scripts/repair_arxiv_email_paper_workflow_consistency.py`, and
  `scripts/repair_jvnautosci_2272_zhan_gmail_arxiv_ingestion_workflow.py`:
  one-shot Vontology-authoring repair/publication helpers. File only if they
  become startup/runtime workflow authority, recurring workflow-family
  publishers, templates, or request-path authority.
- `src/backend/security/role_resolver.py` and organisation-role fallback:
  historical Phase-1 RBAC stub surface covered by `JVNAUTOSCI-787` and future
  Vontology-driven role inheritance in `JVNAUTOSCI-704`.
- `src/backend/integrations/internal_mcp/arxiv_proxy_mcp.py`: low-level arXiv
  adapter/cache support. File only if user-facing paper policy or fallback
  sequencing moves from represented wrapper workflows into the adapter.
- `prompt_metadata_resolution.py`, `llm_step_executor.py`,
  `model_execution_budget_policy.py`, and model-selection workflow support:
  current evidence is Vontology-authored model/prompt metadata support. File if
  capability semantics, prompt wording, or model-choice policy become Python.
- `concept_predicate_metadata_service.py`: update `JVNAUTOSCI-1957` unless a
  separate predicate-policy surface appears.
- `workflow_event_integration_service.py` autotrigger/depth guards: file only
  with fresh evidence not covered by `JVNAUTOSCI-1605` or later event-trigger
  follow-ups.
- `file_copy_upload_classification_workflow.py` and
  `file_copy_upload_handler_workflow.py`: route-map handling remains a
  non-issue while candidate routes and dispatch metadata are loaded from
  Vontology, especially `#V#hasWorkflowTypedSubworkflowRouteMapJson`.
- `annotation_extraction_service.py`: annotation prompt instructions are loaded
  through Vontology relations and protected by workflow-purity checks.

Do not file solely because a file is large or contains domain nouns. File when
Python owns durable decision policy, evidence choice, prompt/workflow content,
user-visible copy, or KB/profile semantics.
