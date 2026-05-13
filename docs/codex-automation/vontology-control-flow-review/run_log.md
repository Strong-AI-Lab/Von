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
  priority order, target surfaces, dedupe identity, and benchmark evidence
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
