# Run Log

## Compact History

- 2026-04-30: Created `JVNAUTOSCI-2193` through `JVNAUTOSCI-2197`; later
  compacted operational memory at user request.
- 2026-04-30T21:59:17+12:00: Created `JVNAUTOSCI-2216` for file-copy entity
  materialisation contracts and left arXiv launch-input extraction covered by
  `JVNAUTOSCI-2212`.
- 2026-05-01T03:11:18+12:00: Created `JVNAUTOSCI-2219` for PhD-student
  workflow-authoring policy in Python; updated `JVNAUTOSCI-2080`.
- 2026-05-02T03:13:26+12:00: Created `JVNAUTOSCI-2234` and `JVNAUTOSCI-2235`;
  updated `JVNAUTOSCI-1985` with minimal-imposition runtime profile fallback
  evidence.
- 2026-05-04T03:11:46+12:00: Confirmed `JVNAUTOSCI-2260` covers represented
  artefact repo-seed/startup authority; created `JVNAUTOSCI-2262`.
- 2026-05-05T03:07:28+12:00: Created `JVNAUTOSCI-2264`; updated
  `JVNAUTOSCI-1957`; model-family prompt variants were treated as represented
  metadata support.
- 2026-05-06T03:08:27+12:00: Created `JVNAUTOSCI-2271` for the Python-authored
  KR materialisation workflow publisher; workflow-purity passed.
- 2026-05-07T02:07:29+12:00: Created `JVNAUTOSCI-2275` for selected-workflow
  recovery handoff policy; updated `JVNAUTOSCI-1985` with required-tool prefix
  classifier evidence; workflow-purity passed with
  `repo_seed_authority_drift_path_count=1`.

## 2026-05-08T02:08:41.0553242+12:00

- Read required repo guidance, repo-local drift-review memories, and the global
  automation memory from the previous run.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero, while reporting
  `repo_seed_authority_drift_path_count=1`.
- Reviewed Python changes since the previous automation run and the dirty
  `jvnautosci-2282-gmail-replay-fix` worktree, especially selector prompt
  routing-context formatting, Gmail evidence metadata, buttonify skip plumbing,
  chat-history blob offload, and tool-message reconstruction support.
- Created `JVNAUTOSCI-2284` for Gmail read-tool evidence-role defaults in
  `tool_metadata_service.py`: the active Gmail replay fix branch adds
  `operation_category`/`evidence_role` semantics for `gmail_list_messages` and
  `gmail_get_message` directly in Python. Linked it to `JVNAUTOSCI-1913`,
  `JVNAUTOSCI-1985`, and `JVNAUTOSCI-2282`, and added a visible Jira
  user-impact comment.
- Treated selector `routing_policy_fragments`, file-copy upload route-map
  support, and annotation extraction prompt loading as watch/non-issues rather
  than new tasks this run.
- No production code was changed.

## 2026-05-09T02:09:15.5073269+12:00

- Read required repo guidance, repo-local drift-review memories, and attempted
  the global automation memory path; `$CODEX_HOME` was unset in this shell, so
  the memory file was written under `C:\Users\mwit860\.codex\automations`.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero and `repo_seed_authority_drift_path_count=1`.
- Reviewed code changes since `2026-05-07T14:00:30Z`: Gmail evidence-contract
  materialisation (`JVNAUTOSCI-2287`, `JVNAUTOSCI-2288`), Gmail fallback
  metadata (`JVNAUTOSCI-2284` still open), LLM-duration stats, chat-history blob
  offload, buttonify action removal, and workspace-idle reporting.
- Treated LLM-duration stats and chat-history blob offload as support-only
  telemetry/storage work. Treated the Gmail evidence-contract materialisation
  services as covered by `JVNAUTOSCI-2284`/`JVNAUTOSCI-2286` unless they become
  runtime fallback authority.
- Created `JVNAUTOSCI-2291` for talk/presentation representation profile and
  verification policy in Python: `talk_representation_service.py` owns profile
  names, required type sets, predicate/type primitive creation, field-to-predicate
  materialisation, and verification criteria; `talk_representation_workflow.py`
  delegates durable actions to that service. Linked it to `JVNAUTOSCI-1913`,
  `JVNAUTOSCI-2173`, and `JVNAUTOSCI-2216`, and added a user-impact comment.
- No production code was changed.

## 2026-05-10T02:08:11.3452112+12:00

- Read required repo guidance, repo-local drift-review memories, and the global
  automation memory path, which was empty/absent at run start.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero and `repo_seed_authority_drift_path_count=1`.
- Reviewed code changes since `2026-05-08T14:02:09Z`, especially the general
  mail review workflow, mail-profile resolver/materialisation support, Vontology
  tool-evidence projection, final-answer synthesis telemetry, and split selector
  routing tests.
- Created `JVNAUTOSCI-2296` for mail-profile resource authority in Python:
  `mail_profile_resource_vontology_service.py` defines mail/Gmail profile
  resource vocabulary, authorised/default/runtime-alias predicates, concrete
  alias concept IDs, and user/default-profile relationships from Python specs.
  Linked it to `JVNAUTOSCI-1913`, `JVNAUTOSCI-2295`, and `JVNAUTOSCI-2286`, and
  added a visible user-impact comment.
- Treated `tool_evidence_projection_service.py` as generic represented-contract
  runtime support, the represented `#V#general_mail_review_workflow` resolver
  step as the correct workflow-side authority, and the meeting-invitation
  experiment template as covered by `JVNAUTOSCI-1579` absent fresh runtime
  production-authority evidence.
- No production code was changed.

## 2026-05-11T02:05:21.5376153+12:00

- Read required repo guidance, repo-local drift-review memories, and attempted
  the global automation memory path, which was absent at run start.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero and `repo_seed_authority_drift_path_count=1`.
- Reviewed commits since `2026-05-09T14:01:44Z`, including workflow authority
  actor scoping, required-evidence completion gates, local-model fail-closed UI
  changes, and the `JVNAUTOSCI-2300` general mail workflow profile handoff.
- Treated the new Gmail profile concept-to-runtime-alias bridge in
  `catalogue.py` as low-level integration plumbing covered by `JVNAUTOSCI-2296`
  unless it starts authoring mail-profile vocabulary/facts or fail-open policy.
  Treated the new mail-review repo-seed prompt/control edits as covered by the
  existing repo-seed/workflow-purity track (`JVNAUTOSCI-2080`,
  `JVNAUTOSCI-2260`).
- Created `JVNAUTOSCI-2302` for
  `workflow_introspection_maintenance_workflow.py`: it defines a Python-built
  durable workflow, `source="built_in"` registration, diagnosis heuristics,
  prompt repair wording, Jira remediation wording, and tool-family/domain
  semantics that should move to Vontology-stored VWL/prompt/KB authority.
  Linked it to `JVNAUTOSCI-1913`, `JVNAUTOSCI-1298`, and `JVNAUTOSCI-2080`.
- No production code was changed.

## 2026-05-12T02:09:54.7106020+12:00

- Read required repo guidance, repo-local drift-review memories, and attempted
  the global automation memory path, which was absent at run start.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero and `repo_seed_authority_drift_path_count=1`.
- Reviewed commits since `2026-05-10T14:00:50Z`, especially general mail review
  workflow seed publication, Gmail detail fan-out, represented mail-profile
  resolution, compact Gmail evidence payloads, and workflow result flattening.
- Treated the recent mail-review repo-seed prompt/control edits as already
  covered by `JVNAUTOSCI-2080` and `JVNAUTOSCI-2260`; added a fresh note to
  `JVNAUTOSCI-2260` instead of filing a duplicate. The direct Python changes in
  the same range looked like target-workflow registration plus generic
  tool/runtime support.
- Created `JVNAUTOSCI-2311` for residual episode self-improvement profile
  authority in Python: `episode_self_improvement_profile_vontology_service.py`
  still defines canonical profile concept ids, policy payloads, workflow links,
  fallback profile resolution, and bootstrap materialisation for launch budget,
  priority order, target-surfaces, dedupe identity, and benchmark evidence
  budgets. Linked it to `JVNAUTOSCI-1913` and `JVNAUTOSCI-1993`.
- No production code was changed.

## 2026-05-13T02:14:44.8078433+12:00

- Read required repo guidance, repo-local drift-review memories, and attempted
  the global automation memory path, which was absent at run start.
- Reviewed commits since `2026-05-11T14:01:42Z`: `35e196af` bounded completed
  durable workflow outputs and fixed empty Gmail list payloads; `a186d578`
  updated this review memory. Treated the code changes as support-only.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero and `repo_seed_authority_drift_path_count=1`.
- Created `JVNAUTOSCI-2315` for `#V#jira_task_incremental_import_workflow`:
  `jira_task_incremental_import_workflow.py` still builds a Python
  `WorkflowDefinition`, registers it as `source="built_in"`, and test bootstrap
  publishes that graph into Vontology. The companion full-reconciliation module
  demonstrates the preferred action-only support shape.
- Linked `JVNAUTOSCI-2315` to `JVNAUTOSCI-1913`, `JVNAUTOSCI-1517`,
  `JVNAUTOSCI-1523`, `JVNAUTOSCI-1407`, `JVNAUTOSCI-2205`, and
  `JVNAUTOSCI-2302`, and added a visible user-impact comment.
- No production code was changed.

## 2026-05-13T11:43:36.5962830+12:00

- Read required repo guidance, situation-specific workflow/prompt/memory and
  operational docs, repo-local drift-review memories, and the global automation
  memory path, which was empty/absent at run start.
- Found the local memory Markdown files line-collapsed while `origin/main`
  already contained the formatted prior-run updates. Restored the working
  copies from the formatted `origin/main` versions before appending this run.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate passed with
  all failure counters zero and `repo_seed_authority_drift_path_count=1`.
- Reviewed registry authority and drift-prone durable surfaces. Treated generic
  `WorkflowDefinition` test builders as watch items while production registry
  loading stays Vontology-discovered and `_register_python_defined_workflows()`
  remains empty.
- Created `JVNAUTOSCI-2316` for multilingual concept-enrichment policy in
  Python/env defaults: target languages, language aliases, candidate thresholds,
  confidence gate, mutation budget, reanalysis window, and managed schedule
  default inputs should be represented workflow/profile policy. Linked it to
  `JVNAUTOSCI-1913`, `JVNAUTOSCI-2080`, and `JVNAUTOSCI-2315`, and added a
  visible user-impact comment.
- No production code was changed.

## 2026-05-14T02:09:18.5848307+12:00

- Read required repo guidance, situation-specific workflow/prompt/memory docs,
  repo-local drift-review memories, and the global automation memory.
- Reviewed commits since `2026-05-12T23:38:22.251Z`, including Jira import
  workflow materialisation, replay prompt-variant arms, bounded workflow MCP
  evidence projections, turn response reconciliation, same-conversation failure
  triggers, and replay experiment helper extraction.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate failed on
  `repo_seed_authority_drift_path_count`: baseline `1`, current `2`, delta `1`.
  The reported paths were
  `src/backend/services/jira_task_incremental_import_workflow_vontology_service.py`
  and `src/backend/services/kr_materialisation_workflow_vontology_service.py`.
  KR materialisation remains covered by `JVNAUTOSCI-2271`; Jira import remains
  covered by `JVNAUTOSCI-2315`, so no duplicate was filed.
- Created `JVNAUTOSCI-2324` for replay evaluation policy in Python:
  `scripts/run_live_kb_tool_prompt_sampler.py` owns prompt-bank cases,
  response-marker rubrics, user-happiness scoring, required-tool expectations,
  promotion blockers, and policy-update reporting; companion replay experiment
  services turn those Python decisions into persisted observations and
  pass/partial/fail verdicts. Linked it to `JVNAUTOSCI-1913`,
  `JVNAUTOSCI-1894`, `JVNAUTOSCI-2318`, `JVNAUTOSCI-2322`, and
  `JVNAUTOSCI-2323`, and added visible Jira comments.
- Treated failure-case intake reference resolution, turn-response surface
  reconciliation, Vontology-authored tool-result hints, and bounded MCP
  projection support as support/watch items rather than new tasks.
- No production code was changed.

## 2026-05-15T02:07:00.5508972+12:00

- Read required repo guidance, situation-specific workflow/prompt/memory and
  operational docs, repo-local drift-review memories, and the global automation
  memory.
- Found no new commits on `main` since `2026-05-13T14:01:25Z`, but the
  worktree contained dirty replay-evaluation authority changes. Treated those
  as current code evidence without editing production code.
- Ran `python scripts\check_workflow_purity.py --verbose`; the gate still
  failed only on `repo_seed_authority_drift_path_count`: baseline `1`, current
  `2`, delta `1`, with the same `jira_task_incremental_import` and
  `kr_materialisation` repo-seed paths already covered by `JVNAUTOSCI-2315` and
  `JVNAUTOSCI-2271`.
- Reviewed the dirty replay-evaluation patch. It appears to be remediation for
  `JVNAUTOSCI-2324`: local sampler verdicts are marked non-authoritative,
  durable experiment observations fail closed unless a represented replay
  evaluation result/rubric is supplied, and the new authority service loads and
  validates a Vontology rubric rather than scoring arms itself. No duplicate
  was filed.
- Rechecked older episode evaluator/critique policy tables. Added fresh
  evidence to `JVNAUTOSCI-2024` for Python-owned evaluator axes, improvement
  category/target/priority aliases, audit-bucket priority, proxy
  classifications, recommendations, and benchmark policy decisions. No new
  Jira issue was created.
- No production code was changed by this review run.

## 2026-05-16T02:10:12.7619816+12:00

- Read required repo guidance, situation-specific workflow/prompt/memory docs,
  operational docs, repo-local drift-review memories, and checked the global
  automation memory path. `$CODEX_HOME` was unset in the shell, so the configured
  writable Codex home path was used directly for global memory.
- Found no new commits on `main` since `2026-05-14T14:01:45Z`, but the worktree
  still contained dirty replay-evaluation authority remediation from prior work.
  Treated those changes as current evidence without editing production code.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  the gate still failed only on `repo_seed_authority_drift_path_count`: baseline
  `1`, current `2`, delta `1`, with
  `jira_task_incremental_import_workflow_vontology_service.py` and
  `kr_materialisation_workflow_vontology_service.py` already covered by
  `JVNAUTOSCI-2315` and `JVNAUTOSCI-2271`.
- Created `JVNAUTOSCI-2326` for AI coding-session ingestion ontology/profile
  authority in Python: `ENVIRONMENT_CONFIGS`, source-system labels, document and
  file-copy type ids, predicates, and materialisation/validation from Python
  constants. Linked it to `JVNAUTOSCI-1362` and `JVNAUTOSCI-1913`, and added a
  visible user-impact comment.
- Created `JVNAUTOSCI-2327` for benchmark/rubric authority in Python/repo seed
  bundles across selector routing, context-bundle, and context-grounded-answering
  benchmark services. Linked it to `JVNAUTOSCI-2065`, `JVNAUTOSCI-2324`,
  `JVNAUTOSCI-2024`, `JVNAUTOSCI-1913`, and `JVNAUTOSCI-1995`, and added a
  visible user-impact comment.
- Treated prompt-id constants, Vontology-backed model budget loading, and legacy
  `WorkflowDefinition` builders as watch/non-issues for this run where there was
  no fresh evidence of active production Python authority.
- No production code was changed by this review run.

## 2026-05-17T02:06:21.4906983+12:00

- Read required repo guidance, situation-specific workflow/memory docs,
  repo-local drift-review memories, and the global automation memory. As in
  earlier shells, `$CODEX_HOME` was unset, so the writable
  `C:\Users\mwit860\.codex\automations` memory path was used.
- Reviewed commits since `2026-05-15T14:01:05Z`: prompt replay observation
  recording, required-tool target closure, Workflow Studio child materialisation
  support, and backend type diagnostics.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  the gate still failed only on `repo_seed_authority_drift_path_count`: baseline
  `1`, current `2`, delta `1`, with the known
  `jira_task_incremental_import_workflow_vontology_service.py` and
  `kr_materialisation_workflow_vontology_service.py` paths already covered by
  `JVNAUTOSCI-2315` and `JVNAUTOSCI-2271`.
- Created `JVNAUTOSCI-2329` for the residual required-tool obligation seam:
  `required_tool_obligation_service.py` still infers operation classes from
  tool-name prefixes and target identity/closure semantics from Python payload
  key lists. Linked it to `JVNAUTOSCI-1913`, `JVNAUTOSCI-2328`,
  `JVNAUTOSCI-1985`, and `JVNAUTOSCI-2284`.
- Treated Workflow Studio child materialisation as support-only and the replay
  observation path as `JVNAUTOSCI-2324` remediation because durable recording
  now requires represented replay evaluation by default.
- No production code was changed by this review run.

## 2026-05-18T02:08:08.5919602+12:00

- Read required repo guidance, situation-specific workflow/prompt/memory docs,
  operational authority-alignment docs, repo-local drift-review memories, and
  checked the global automation memory path. No prior global memory file existed
  in this shell.
- Reviewed commits since `2026-05-16T14:00:53Z`: required-tool metadata
  remediation, deictic arXiv launch-input fixes, representation-routing coverage
  audit workflow, and email-source representation convergence workflow/schedule
  work.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  the gate failed on `repo_seed_authority_drift_path_count`: baseline `1`,
  current `4`, delta `3`. Known paths remained
  `jira_task_incremental_import_workflow_vontology_service.py` and
  `kr_materialisation_workflow_vontology_service.py`; new paths were
  `email_source_representation_convergence_workflow_vontology_service.py` and
  `representation_workflow_routing_coverage_audit_vontology_service.py`.
- Created `JVNAUTOSCI-2340` under `JVNAUTOSCI-2335` for email convergence
  authority drift: repo-seed workflow/hint publication plus Python/env schedule
  defaults for user/org/namespace, Gmail profile, query, cadence, max results,
  launch prompt, and workflow id. Linked it to `JVNAUTOSCI-1913` and
  `JVNAUTOSCI-2080`.
- Created `JVNAUTOSCI-2341` under `JVNAUTOSCI-2064` for representation-routing
  audit authority drift: repo-seed prompt/profile/workflow assets, audit cases,
  thresholds, evaluation dimensions, suggestion policy, and
  `WorkflowRegistration(source="built_in")` test graph should be represented
  authority or proven disposable fixtures. Linked it to `JVNAUTOSCI-2331`,
  `JVNAUTOSCI-2327`, `JVNAUTOSCI-1913`, and `JVNAUTOSCI-2080`.
- Treated `JVNAUTOSCI-2329` required-tool metadata changes as remediation, not
  a fresh issue. The current residual signals are now about represented schedule,
  repo-seed workflow/profile/prompt, and benchmark/rubric authority.
- No production code was changed by this review run.
