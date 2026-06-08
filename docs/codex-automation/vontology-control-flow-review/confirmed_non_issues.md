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
- `tool_evidence_contract_vontology_service.py` and
  `gmail_tool_evidence_contract_vontology_service.py`: current evidence is
  graph-KR materialisation support under `JVNAUTOSCI-2287`/`JVNAUTOSCI-2288`,
  with remaining runtime fallback authority already tracked by `JVNAUTOSCI-2284`
  and broader projection work by `JVNAUTOSCI-2286`. File only if these services
  become startup/runtime authority that repopulates missing Gmail semantics
  instead of a deletable materialisation helper.
- `tool_evidence_projection_service.py`: generic runtime projection executor
  that reads represented tool/evidence-view/field contracts. File only if the
  priority ordering, field selection, or redaction semantics become
  integration-specific Python policy rather than represented metadata.
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
- `experiment_run_service.py` meeting-invitation scenario template: already
  covered by `JVNAUTOSCI-1579` unless fresh runtime evidence shows the
  remaining Python template is still production workflow or prompt authority
  rather than test/experiment scaffolding.
- `#V#general_mail_review_workflow` resolver step: the workflow-side profile
  resolution is represented; the newly filed concern is the Python mail-profile
  resource vocabulary/materialiser tracked by `JVNAUTOSCI-2296`.
- `catalogue.py` Gmail profile concept-to-runtime-alias bridge: treat as
  low-level integration plumbing while it only exposes represented aliases to
  Gmail tools. Update `JVNAUTOSCI-2296` instead of filing a duplicate if the
  bridge starts authoring mail-profile vocabulary/facts or silently falls back
  to unrepresented profile policy.
- Recent mail-review repo-seed prompt/control edits are covered by the existing
  repo-seed/workflow-purity track (`JVNAUTOSCI-2080`, `JVNAUTOSCI-2260`) unless
  they become a distinct runtime authority path not represented in Vontology.
- 2026-05-12: commits `72d8e7c1..0a438ac8` expanded
  `#V#general_mail_review_workflow` and `#V#gmail_message_detail_fetch_workflow`
  seed content, including profile lookup, Gmail list/detail fan-out, and
  grounded rendering. This reinforced `JVNAUTOSCI-2260`; no duplicate was filed.
  The surrounding Python catalogue/runtime edits looked support-only unless they
  start authoring mail facts or unrepresented profile policy.
- 2026-05-13: commit `35e196af` changed durable terminal-output persistence and
  Gmail zero-result payload handling. `build_completed_workflow_outputs()`
  bounds persisted outputs and avoids raw context duplication; the Gmail change
  ensures `messages=[]` is exposed for empty lists. No new workflow/KB authority
  drift was filed for those support changes.
- 2026-05-13: generic `WorkflowDefinition`/`WorkflowRegistration` test builders
  remain watch items while `_register_python_defined_workflows()` stays empty
  and the production registry is Vontology-discovered. File only where the test
  builder or bootstrap helper is the reproducible source for workflow policy, as
  with `JVNAUTOSCI-2315`.
- 2026-05-14: `failure_case_intake_service.py` remains a watch item, not a
  filed issue, while it only resolves an already selected same-conversation
  reference mode to a request id using generic telemetry. File if it starts
  classifying utterance intent, failure cause, prompt hypothesis, or remediation
  policy in Python.
- 2026-05-14: `turn_response_surface_service.py` looks support-only while it
  reconciles deterministic response hashes/statuses and preserves final-answer
  surfaces. Update `JVNAUTOSCI-2324` if this layer starts deciding answer
  correctness, promotion eligibility, or replay-learning policy without a
  represented rubric.
- 2026-05-14: the new Jira incremental import repo-seed bootstrap path is
  covered by `JVNAUTOSCI-2315`; do not file a duplicate merely because
  `jira_task_incremental_import_workflow_vontology_service.py` appears in the
  workflow-purity repo-seed count. Watch the purity baseline and ensure the
  represented workflow becomes independent of repo seed publication.
- 2026-05-15: episode evaluator/critique policy tables remain covered by
  `JVNAUTOSCI-2024`. Evidence rechecked this run:
  `episode_evaluation_workflow_contracts.py` defines evaluator axes and
  improvement surfaces; `episode_critique_memory_service.py` normalises
  category/target/priority aliases; `episode_critique_benchmark_service.py`
  owns audit bucket priority, proxy classification, recommendations, and
  benchmark policy decisions. Update `JVNAUTOSCI-2024` rather than filing a
  duplicate unless a distinct runtime authority path appears.
- 2026-05-16: `llm_step_executor.py` prompt-id constants and
  `model_execution_budget_policy.py` Vontology-backed budget loading looked
  support-only in this scan. File only if prompt bodies, user-facing recovery
  copy, model-choice semantics, or budget policy defaults become Python-owned
  rather than represented prompt/model metadata.
- 2026-05-16: legacy durable modules containing `WorkflowDefinition(...)`
  builders remain watch items, not automatic duplicates, while production
  registry loading reports Vontology as the active source and
  `built_in_registration_count=0`. File only where the Python builder or
  publisher remains the reproducible source for workflow graph/control policy.
- 2026-05-17: `workflow_studio_service.py` child-concept materialisation and
  `workflow_concept_authority_service.py` `create_missing_child_concepts`
  handling looked like workflow-authoring support, not a new policy surface.
  The replay observation changes look like remediation for `JVNAUTOSCI-2324`
  while durable recording still requires represented replay evaluation by
  default. No duplicates were filed for those areas.
- 2026-05-18: required-tool obligation changes from `c6289c1d` look like
  remediation for `JVNAUTOSCI-2329`: operation/evidence/target semantics are
  moved toward metadata rather than inferred from tool-name prefixes. Do not
  file a duplicate without fresh evidence that Python fallback semantics still
  decide operation class or target closure.
- 2026-05-18: `JVNAUTOSCI-2335` and `JVNAUTOSCI-2331` were existing feature
  tasks, not duplicate drift tickets. Fresh residual authority issues were filed
  as `JVNAUTOSCI-2340` and `JVNAUTOSCI-2341`; update those instead of reopening
  broad duplicates for the same email convergence or representation-routing
  audit surfaces.
- 2026-05-19: `f2988eed` (`JVNAUTOSCI-2283`), `46ddd799`
  (`JVNAUTOSCI-2326`), and `8e0ab8a4` (`JVNAUTOSCI-2327`) looked like
  remediation rather than new drift. Gmail evidence roles now come from
  Vontology metadata; AI chat-session source profiles and benchmark suites load
  represented definitions and fail closed unless an explicit fixture path/import
  helper is used. Do not file duplicates without fresh runtime fallback
  evidence.
- 2026-05-19: source-specific adapter classes for Codex/Copilot/Gemini chat
  transcript file discovery are support-only while source roots, file patterns,
  profile concepts, and ontology type/predicate definitions are loaded from
  represented source profiles. File only if adapter code starts authoring source
  profile policy or silently falls back when Vontology authority is missing.
- 2026-05-20: workflow purity still failed only on the four known repo-seed paths
  already tracked by `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`,
  and `JVNAUTOSCI-2341`. Do not file new tickets for those paths without fresh
  drift evidence.
- 2026-05-20: background-task reconciliation of terminal durable conversation
  instances looked like support plumbing while it only maps persisted workflow
  outputs/status into task status/result endpoints. File only if it starts
  deciding answer semantics or workflow success policy in Python.
- 2026-05-21: current `main` still contains the `JVNAUTOSCI-2352` and
  `JVNAUTOSCI-2353` drift surfaces from the previous dirty-worktree scan
  (`durable_discovery_fallback`/`first_routing_match` in
  `turn_execution_actions.py`; nested presenter wording such as
  `NESTED WORKFLOW EVIDENCE (authoritative)` in `von_routes.py`). Do not file
  duplicates; update those issues if new evidence appears.
- 2026-05-21: `JVNAUTOSCI-2354` added useful Gmail OAuth scope tooling but
  fresh residual drift belongs under open `JVNAUTOSCI-2296`: OAuth-scope
  predicate/profile facts and env-fallback/auto-bootstrap behaviour are part of
  the existing mail-profile resource authority cleanup. A Jira comment and
  Relates link to `JVNAUTOSCI-2354` were added instead of creating a duplicate.
- 2026-05-22: nested selected-workflow evidence preservation remains a valid
  support need under `JVNAUTOSCI-2356`; do not file merely because nested
  invocations are preserved or bounded. File/update `JVNAUTOSCI-2357` when the
  preservation path decides surfaceability, artefact type, source-specific
  provenance, or user/model-visible labels from Python rather than represented
  evidence-view/presenter metadata.
- 2026-05-23: `synthesiser_context_framing_service.py` is support-only while it
  loads a Vontology prompt concept, validates the JSON template schema, binds
  variables, and fails closed on missing/invalid template text. The repo-side
  `synthesiser_context_framing_prompt_seed.json` is a repo-seed prompt-authority
  caveat covered by the repo-seed/workflow-purity track, not a new Python drift
  issue by itself.
- 2026-05-23: `tool_invocation_evidence.py` looked support-only in the
  `JVNAUTOSCI-2356` follow-up scan. It canonicalises tool names, walks nested
  invocation payloads, and preserves bounded evidence records; it did not add
  domain workflow policy or user-facing wording.
- 2026-05-23: `fc9bbaff` premium Ollama settings changes are UI/settings-state
  fixes, not workflow/Vontology authority drift. Re-check only if model-choice
  policy or prompt-stage routing starts being decided by the settings route
  rather than represented model/workflow metadata.
- 2026-05-24: `08937b13` live workflow progress changes looked support-only:
  `von_routes.py` preserves `latest_subtask`, `latest_workflow_task`, and
  `latest_tool` from existing progress events, while `chatTab.js` renders
  already-reported workflow-stage history in the default Thinking-card mode.
  File only if this path starts inventing stage/task semantics or presenter
  wording from payload fields.
- 2026-05-24: `1616b84f` client default model resolution looked diagnostic and
  support-only. It records the concrete client default model/provider/host when
  no explicit model is passed; re-check only if this path starts deciding model
  policy rather than reporting the effective call target.
- 2026-05-24: active branch
  `JVNAUTOSCI-2363-required-tool-dispatch-preflight` adds
  `build_selector_call_prompt()` so the selector LLM foreground prompt includes
  the raw user request rather than only `Select workflow`. Treat as a watch
  item, not drift, while it remains generic request passthrough and selector
  policy still lives in the Vontology-rendered selector prompt/context. File if
  Python adds selector instructions, examples, fallback wording, or routing
  semantics to that foreground prompt.
- 2026-05-24: the `predicate|predicates|incidence|extent` required-tool
  inference seam in `orchestrator.py` is not fresh; it is present on
  `origin/main` and should be handled by the existing required-tool authority
  track (`JVNAUTOSCI-2329` / related follow-through) rather than duplicated.
- 2026-05-25: active dirty branch `JVNAUTOSCI-2364-entity-relation-boundary`
  edits looked like a prompt/workflow-authority repair for self-relative
  relation retrieval plus response-surface propagation. The repo-seed prompt
  edits are covered by the existing seed-authority caveat, and the response-key
  precedence changes are support-only unless they start inventing answer
  semantics or presenter wording. No duplicate was filed for `JVNAUTOSCI-2364`.
- 2026-05-25: `build_selector_call_prompt()` remains a watch item, not drift,
  while it only passes the raw user request as the selector LLM foreground
  prompt and selector policy remains in the Vontology-rendered system/context
  prompt. File only if Python adds selector instructions, examples, fallback
  wording, or routing semantics there.
- 2026-06-09: `scripts/author_jvnautosci_2421_gmail_arxiv_progress_projection.py`
  looked like a one-shot Vontology-authoring helper for represented
  progress-projection metadata, not request-path authority. File only if these
  Gmail/arXiv progress facts become startup/runtime repopulation authority or
  if request-path Python starts deciding progress labels/visibility from the
  script's domain tables.
- 2026-06-09: final-answer projected-evidence plumbing in
  `turn_execution_record_service.py` and `turn_execution_runtime_support.py`
  looked support-only: it bounds/redacts projected payloads and carries
  represented tool-evidence projection into critic/completion evidence. File
  only if Python starts deciding field/view semantics or user-facing answer
  wording rather than preserving represented projection metadata.

Do not file solely because a file is large or contains domain nouns. File when
Python owns durable decision policy, evidence choice, prompt/workflow content,
user-visible copy, or KB/profile semantics.
