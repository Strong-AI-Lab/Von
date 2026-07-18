# Run Log

Operational memory only. Keep this compact; detailed diagnosis belongs in Jira tasks.

## Compact History

- 2026-04-30 to 2026-05-07: Created `JVNAUTOSCI-2193` through `JVNAUTOSCI-2197`,
  then `JVNAUTOSCI-2216`, `JVNAUTOSCI-2219`, `JVNAUTOSCI-2234`, `JVNAUTOSCI-2235`,
  `JVNAUTOSCI-2262`, `JVNAUTOSCI-2264`, `JVNAUTOSCI-2271`, and `JVNAUTOSCI-2275`.
  The recurring purity check generally passed with
  `repo_seed_authority_drift_path_count=1`.
- 2026-05-08 to 2026-05-13: Created `JVNAUTOSCI-2284`, `JVNAUTOSCI-2291`,
  `JVNAUTOSCI-2296`, `JVNAUTOSCI-2302`, `JVNAUTOSCI-2311`, `JVNAUTOSCI-2315`,
  and `JVNAUTOSCI-2316`. Later scans treated selector routing fragments,
  Vontology-backed tool metadata support, and generic workflow test builders as watch
  items rather than duplicate issues.
- 2026-05-14: Created `JVNAUTOSCI-2324` for replay/evaluation authority. The purity
  gate failed only on known repo-seed paths for Jira import and KR materialisation,
  covered by `JVNAUTOSCI-2315` and `JVNAUTOSCI-2271`.
- 2026-05-16: Created `JVNAUTOSCI-2326` and `JVNAUTOSCI-2327` for AI coding-session
  source profiles and benchmark/rubric authority in Python/repo seed surfaces.
- 2026-05-17: Created `JVNAUTOSCI-2329` for residual required-tool obligation
  semantics inferred from Python prefixes and payload keys.
- 2026-05-18: Created `JVNAUTOSCI-2340` and `JVNAUTOSCI-2341` for email-source
  convergence schedule/profile authority and representation-routing audit authority.
  The purity gate then failed only on the four known repo-seed paths covered by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and `JVNAUTOSCI-2341`.

## 2026-05-19T02:06:18.1457673+12:00

- Read required repo guidance, workflow/prompt/memory/authority-alignment docs, and
  repo-local automation memory.
- Reviewed commits since `2026-05-17T14:00:43Z`, with focus on `JVNAUTOSCI-2110`,
  `JVNAUTOSCI-2283`, `JVNAUTOSCI-2326`, and `JVNAUTOSCI-2327`.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`; it still
  failed only on `repo_seed_authority_drift_path_count`: baseline `1`, current `4`,
  delta `3`. The four paths remain the known Jira import, KR materialisation, email
  convergence, and representation-routing audit repo-seed/bootstrap paths already
  tracked by `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and
  `JVNAUTOSCI-2341`.
- Treated `f2988eed` (`JVNAUTOSCI-2283`), `46ddd799` (`JVNAUTOSCI-2326`), and
  `8e0ab8a4` (`JVNAUTOSCI-2327`) as remediation rather than new drift: Gmail evidence
  roles, AI chat-session source profiles, and benchmark suites now load represented
  authority and fail closed unless explicit fixture/import paths are used.
- Created `JVNAUTOSCI-2350` under `JVNAUTOSCI-2112` for residual synthesiser context
  framing drift: `synthesiser_context_prep_actions.py` builds LLM-visible active-request
  and hint-wrapper strings in Python, and tests pin those exact literals instead of a
  represented prompt/context-template authority.
- No production code was changed by this review run.

## 2026-05-20T02:06:52.6193094+12:00

- Read required repo guidance, workflow manual, authority-alignment guidance, and
  repo-local automation memory.
- Reviewed the dirty `jvnautosci-2351-live-arxiv-gmail-replay` worktree plus broad
  drift patterns. Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py
  --verbose`; it still failed only on the known four repo-seed paths tracked by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and `JVNAUTOSCI-2341`.
- Created `JVNAUTOSCI-2352` for new durable turn workflow-discovery fallback drift in
  `turn_execution_actions.py`: Python stopwords/token overlap/support-workflow
  demotion/first-match routing and selector fallback wording.
- Created `JVNAUTOSCI-2353` for new presenter nested-workflow evidence drift in
  `von_routes.py`: Python-authored read-back/blocker/domain wording and model-visible
  "authoritative" sections.
- Treated durable background-task reconciliation as support plumbing for now.
- No production code was changed by this review run.

## 2026-05-21T02:08:40.6983998+12:00

- Read required repo guidance, workflow manual excerpts, authority-alignment
  guidance, automation memory, and repo-local review memory.
- Reviewed commits since `2026-05-19T14:00:59Z`. The fresh code was mainly
  Gmail OAuth/profile work (`JVNAUTOSCI-2354`) plus the previously identified
  `JVNAUTOSCI-2352`/`JVNAUTOSCI-2353` surfaces now on `main`.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`; it
  still failed only on the known four repo-seed paths tracked by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and
  `JVNAUTOSCI-2341`.
- Confirmed `turn_execution_actions.py` and `von_routes.py` still contain the
  prior `JVNAUTOSCI-2352`/`JVNAUTOSCI-2353` drift evidence; no duplicate tasks
  were created.
- Updated `JVNAUTOSCI-2296` with fresh residual Gmail OAuth scope evidence:
  Python now defines `#V#has_oauth_scope`, writes `attributes.oauth_scopes`,
  falls back to env scope authority, emits re-authorisation/advisory wording,
  and auto-bootstraps missing Gmail profile concepts. Linked `JVNAUTOSCI-2296`
  to completed `JVNAUTOSCI-2354`.
- No production code was changed by this review run.

## 2026-05-22T02:06:36.9847037+12:00

- Read required repo guidance, security guidance, workflow/prompt/memory and
  operational/authority-alignment docs, plus repo-local review memory.
- Reviewed current `origin/main` at `9ecfc2b8` and the dirty
  `fix/selected-workflow-evidence-gate` worktree. Existing dirty production
  files were not edited or staged.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  it still failed only on the known four repo-seed drift paths tracked by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and
  `JVNAUTOSCI-2341`.
- Created `JVNAUTOSCI-2357` for new surfaceable concept evidence drift:
  `tool_evidence_projection_service.py`, `turn_execution_runtime_support.py`,
  and `orchestrator.py` now use Python key lists/substrings/source keys and
  labels such as `Created paper concept` / `Linked file copy` to decide and
  describe surfaceable concept handles. Linked it to `JVNAUTOSCI-1913`,
  `JVNAUTOSCI-2353`, `JVNAUTOSCI-2355`, and `JVNAUTOSCI-2356`; commented on
  `JVNAUTOSCI-2356`.
- No production code was changed by this review run.

## 2026-05-23T02:05:41.1151605+12:00

- Read required repo guidance, security guidance, workflow manual,
  operational/authority-alignment guidance, automation memory, and repo-local
  review memory.
- Reviewed current `origin/main` / `main` at `6aa91175` on branch
  `fix/jvnautosci-2351-live-arxiv-gmail-convergence`. The worktree had
  pre-existing dirty changes in `von_routes.py`, `chatTab.js`,
  `chatTab.test.js`, and `test_tool_progress_liveness.py`; none were edited or
  staged by this review.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  it still failed only on the known four repo-seed drift paths tracked by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and
  `JVNAUTOSCI-2341`.
- Reviewed commits since the prior run, especially `a0a51e8a` (`JVNAUTOSCI-2356`),
  `6aa91175` (`JVNAUTOSCI-2350`), and `fc9bbaff`. `JVNAUTOSCI-2350` moved
  synthesiser context framing from Python literals into a Vontology-resolved
  prompt/template service; the remaining repo-seed prompt JSON caveat is a
  repo-seed authority concern, not a new Python drift issue. Surfaceable
  concept evidence policy remains the open `JVNAUTOSCI-2357` surface, so no
  duplicate was filed.
- Treated `tool_invocation_evidence.py` and the dirty live-progress fields in
  `von_routes.py` as support plumbing: they preserve tool/stage evidence and
  do not introduce new workflow, prompt, or KB authority.
- No new Jira issues were created and no production code was changed by this
  review run.

## 2026-05-24T02:04:15.1730819+12:00

- Read required repo guidance, security guidance, workflow manual,
  operational/authority-alignment guidance, repo-local review memory, and
  current automation memory path. The automation memory file was missing before
  this run and was recreated.
- Reviewed commits since `2026-05-22T14:02:21Z`: `08937b13`
  (`JVNAUTOSCI-2351` live workflow progress), `91b2b891` (Pyright version
  update), `1616b84f` (client default model resolution), and the review-memory
  commits from the previous run.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  it still failed only on the known four repo-seed drift paths tracked by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and
  `JVNAUTOSCI-2341`.
- Treated `08937b13` and `1616b84f` as support-only: the former preserves
  live stage task/tool detail and the latter records concrete client default
  model/provider diagnostics without selecting model policy in Python.
- Reviewed the dirty `JVNAUTOSCI-2363-required-tool-dispatch-preflight`
  worktree. The selector-call foreground prompt change looked like generic
  request passthrough to avoid the model seeing only `Select workflow`; file
  only if it grows selector instructions, fallback wording, or routing policy
  beyond passing through the raw request. The legacy contract-text
  `predicate|incidence|extent` required-tool inference already exists on
  `origin/main` and remains covered by `JVNAUTOSCI-2329` / the required-tool
  authority track.
- No new Jira issues were created and no production code was changed by this
  review run.

## 2026-05-25T02:06:33.3340832+12:00

- Read required repo guidance, security guidance, workflow manual, memory and
  authority-alignment guidance, current automation memory, and repo-local review
  memory.
- Reviewed commits since `2026-05-23T14:00:41Z`. The only production commit on
  `origin/main` was `f63f66ef` (`JVNAUTOSCI-2363 Enforce required-tool dispatch
  preflight`) plus prior review-memory commits.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  it still failed only on the known four repo-seed paths tracked by
  `JVNAUTOSCI-2315`, `JVNAUTOSCI-2271`, `JVNAUTOSCI-2340`, and
  `JVNAUTOSCI-2341`.
- Created `JVNAUTOSCI-2365` for new turn-contract dispatch override drift:
  `orchestrator.py` now decides direct-response/tool-pipeline override,
  external multi-surface recovery, and workflow-execute single-candidate
  recovery in Python. Linked it to `JVNAUTOSCI-1913`, `JVNAUTOSCI-2298`,
  `JVNAUTOSCI-1975`, and `JVNAUTOSCI-2352`.
- Reviewed the dirty `JVNAUTOSCI-2364-entity-relation-boundary` worktree but did
  not edit or stage existing production/test changes. Treated the self-relative
  relation prompt edits and response-output propagation as support/authority
  repair work rather than a duplicate drift task.
- No production code was changed by this review run.

## 2026-06-09T06:22:42.9567772+12:00

- Read required repo guidance, security guidance, workflow/memory/operational
  and authority-alignment docs, current automation memory path, and repo-local
  review memory.
- Local `main` was one commit behind `origin/main`; `git merge --ff-only
  origin/main` failed because Git could not create `.git/ORIG_HEAD.lock`
  (`Permission denied`). Reviewed the local tree plus the `origin/main`
  diff/commit directly.
- `scripts/check_workflow_purity.py --verbose` was blocked before producing a
  purity report by Python syntax errors such as
  `src/backend/workflows/workflow_selector.py:151` `except TypeError,
  ValueError:`. The same pattern also appears in `orchestrator.py` and later
  `workflow_selector.py` locations; do not treat this run as a clean purity
  pass.
- Created `JVNAUTOSCI-2472` for the new Mongo query-diagnostics maintenance
  workflow drift: `mongo_query_diagnostics_maintenance_workflow.py` builds a
  `WorkflowDefinition(...)`, registers it with `source="built_in"`, and
  `mongo_query_diagnostics_maintenance_workflow_vontology_service.py` publishes
  that Python graph at startup.
- Added fresh evidence to existing `JVNAUTOSCI-2406` instead of filing a
  duplicate selector task: `workflow_selector.py` now recovers default/prompt
  unavailable selector results to a single specialised candidate and authors
  fallback reasoning in Python; tests pin those behaviours.
- Treated the Gmail/arXiv progress-projection authoring script as a watch item,
  not drift, because current runtime evidence reads represented
  `#V#hasWorkflowProgressProjectionJson` metadata rather than the script.
- No production code was changed by this review run.

## 2026-06-10T02:06:51.5417152+12:00

- Read required repo guidance, security guidance, workflow manual,
  operational/authority-alignment docs, current automation memory path, and
  repo-local review memory.
- Local `main` was clean and aligned with `origin/main` at `c4bafc33`.
  Reviewed recent commits since the previous run: `6a2ce349` launcher/cloud
  deploy fixes, `90187a47` exception syntax fixes, and `c4bafc33` Python
  minimum-syntax guardrail.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  all guarded counts were `0` and the gate passed.
- Rechecked drift-prone orchestrator/selector edits in the recent commits.
  They were Python syntax fixes inside already-known selector/required-tool
  seams, not fresh policy additions.
- Sampled non-test `WorkflowDefinition(...)` builders and the registry path.
  Runtime registry construction still uses Vontology lazy registration; Python
  test definitions remain watch items, while the Mongo diagnostics workflow is
  already tracked by `JVNAUTOSCI-2472`.
- Treated Atlas Query Insights report/review services as support-only for now:
  they are read-only operational diagnostics/Jira triage tools and do not
  mutate indexes, author workflow policy, or feed represented learning loops.
- No new Jira issues were created and no production code was changed by this
  review run.

## 2026-06-11T02:16:50.3578395+12:00

- Read required repo guidance, security guidance, workflow manual,
  authority-alignment guidance, current automation memory path, and repo-local
  review memory.
- Local `main` was aligned with `origin/main` at `c4bafc33`, but the worktree
  was already dirty in review-memory files plus response-preservation
  production/test changes. The scan inspected those dirty changes but did not
  edit or stage production/test files.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`;
  all guarded counts were `0` and the gate passed.
- Created `JVNAUTOSCI-2496` for graph model-policy drift:
  `workflow_policy_graph_service.py` still hard-codes stage aliases,
  `active_llm` primary fallback, fallback-hop default `2`, compatibility mode,
  and local-only parsing while the orchestrator prefers that graph resolver
  before JSON fallback. Linked it to `JVNAUTOSCI-1913` and `JVNAUTOSCI-2473`.
- Added fresh dirty-worktree response-preservation evidence to existing
  `JVNAUTOSCI-2478` instead of filing a duplicate: current changes preserve
  child/user responses by Python field order and `Execution status:` filtering
  in selected-workflow/completion-gate surfaces.
- No production code was changed by this review run.

## 2026-07-16T10:53:42.3898329+12:00

- Read required repo guidance, security guidance, design index, workflow manual,
  enduring-memory guide, authority-alignment guide, fallback automation memory,
  and all repo-local review memory files.
- Current `main` matched `origin/main` at `0749b43a`; the only pre-existing
  dirty file was `Von.code-workspace`, which this review did not edit or stage.
- Reviewed commits since the 2026-06-17 automation marker, focusing on
  actor-scoped workflow authority, represented structured tool transport,
  context-adjudication/required-tool reliability, and operational
  certification/learning-release services.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`.
  All workflow/prompt/source/policy counters were `0`, but the gate failed the
  monolith ratchet: orchestrator `49436` vs baseline `45441`, catalogue `34898`
  vs `32670`, and von_routes `18862` vs `18136`. Commented this on
  `JVNAUTOSCI-1913`; no size-only task was filed.
- Runtime attribution companion was attempted with
  `.venv\Scripts\python.exe scripts\report_turn_decision_attribution.py --limit 50`
  and timed out before producing a report.
- Created `JVNAUTOSCI-2586` under `JVNAUTOSCI-2575` for operational
  certification/release gate drift: `aggregate_five_trial_campaign()` fixes
  five trials and pass windows `1,3,5`, while
  `promote_operational_learning_release_candidate()` requires specific pass,
  certified, live-source, persisted-evidence, and `live_release_evidence_eligible`
  gate semantics in Python. Linked it to `JVNAUTOSCI-2578` and `JVNAUTOSCI-1913`.
- Added fresh prior-turn obligation carry-forward evidence to active
  `JVNAUTOSCI-2574` instead of filing a duplicate: Python token aliases and
  structural defaults in `turn_expected_outcome_obligation_carry_forward.py`
  still decide carry/suppress behaviour that should become a structured
  represented context-adjudication directive.
- Treated `turn_contract_dispatch_policy_service.py` as a non-issue because it
  resolves represented `#V#turn_contract_dispatch_policy` and fails closed
  rather than inventing dispatch rules.
- No production code was changed by this review run.

## 2026-07-17T02:08:16.7792022+12:00

- Read required repo guidance, security guidance, design index, workflow manual,
  enduring-memory guide, authority-alignment guide, and all repo-local review
  memory files. The personal automation memory file was still absent at start.
- Current `main` matched `origin/main` at `cfdeda00`; the only pre-existing
  dirty file was `Von.code-workspace`, which this review did not edit or stage.
  The only commit since the last automation timestamp was the prior
  review-memory commit (`cfdeda00`), changing no production code.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`.
  All workflow/prompt/source/policy counters were `0`; the gate still failed
  only on the known monolith ratchet: orchestrator `49436` vs baseline `45441`,
  catalogue `34898` vs `32670`, and von_routes `18862` vs `18136`.
- Runtime attribution companion timed out at both `--limit 50` and `--limit 10`,
  so no architecture-integrity score or fallback-signature histogram was
  available from this run.
- Scanned high-risk Python surfaces (`WorkflowDefinition`/`WorkflowRegistration`,
  `_POLICY`/`_BLUEPRINTS`/`_ALIASES` tables, fallback/recovery selectors,
  schedule/profile bootstraps, tool metadata/projection, and representation
  required-effects paths). The strongest candidates mapped to existing indexed
  issues or watch items: `JVNAUTOSCI-2193`, `JVNAUTOSCI-2196`,
  `JVNAUTOSCI-1985`, `JVNAUTOSCI-2284`, `JVNAUTOSCI-2296`,
  `JVNAUTOSCI-2329`, `JVNAUTOSCI-2357`, `JVNAUTOSCI-2496`, and
  `JVNAUTOSCI-2586`.
- Treated the representation required-effects code in
  `turn_execution_record_service.py` as a watch item rather than a fresh Jira
  task: it prefers represented contracts/profiles and represented
  `required_payload_fields`, but still has compatibility defaults for
  `scholarly_representation` and paper read-back diagnostics that should not be
  extended to new domains in Python.
- No new Jira issues were created and no production code was changed by this
  review run.

## 2026-07-18T02:13:51.9491652+12:00

- Read required repo guidance, security guidance, design index, workflow manual,
  enduring-memory guide, operational guide, authority-alignment guide, personal
  automation memory, and all repo-local review memory files.
- Started from `main` at `b6286b71`, then fetch/push preparation showed
  `origin/main` had advanced 39 commits to `99dc6f19`. Rebased the review-memory
  commit and rescanned the newer `origin/main`; the only pre-existing dirty file
  was `Von.code-workspace`, which this review did not edit or stage.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`.
  All workflow/prompt/source/policy counters were `0`; the gate still failed
  only on the known monolith ratchet: orchestrator `49605` vs baseline `45441`,
  catalogue `35061` vs `32670`, and von_routes `18866` vs `18136`.
- Runtime attribution companion
  `.venv\Scripts\python.exe scripts\report_turn_decision_attribution.py --limit 10`
  timed out after about two minutes, so no architecture-integrity score or
  fallback-signature histogram was available.
- Rechecked source-only `WorkflowDefinition(...)`,
  `WorkflowRegistration(...)`, `source="built_in"`, selector-fallback,
  prior-obligation, certification/release-gate, representation-effects, and
  new grounded-read/tool-evidence contract surfaces.
- Confirmed legacy `source="built_in"` test-registration helpers are not active
  runtime authority while `_register_python_defined_workflows()` is empty and
  purity reports `built_in_registration_count=0` plus Vontology-only runtime
  registry sources.
- Created `JVNAUTOSCI-2589` for fresh Python-owned grounded-read/Jira
  tool-evidence contract authority: startup support bootstraps now materialise
  source-specific tool concepts, field/path/view ids, required final-answer
  fields, and tool descriptions from Python constants rather than independent
  represented Vontology authority. Linked it to `JVNAUTOSCI-1913`,
  `JVNAUTOSCI-1985`, and `JVNAUTOSCI-2575`.
- No production code was changed by this review run.

## 2026-07-19T02:05:55.0443309+12:00

- Read current repo guidance, security guidance, design index, relevant VWL
  manual sections, enduring-memory guidance, authority-alignment guidance,
  repo-local review memory, and the automation memory path. `CODEX_HOME` was
  unset in the shell, so the personal automation memory was created under the
  standard local Codex home.
- Fast-forwarded local `main` from `b2141c2e` to `ea61cece`; the only
  pre-existing dirty file was `Von.code-workspace`, which this review did not
  edit or stage.
- Reviewed the two fresh commits since the previous run: `7403e296`
  (`JVNAUTOSCI-2588` internal MCP deadlines) and `ea61cece` design-guidance
  refresh.
- Ran `.venv\Scripts\python.exe scripts\check_workflow_purity.py --verbose`.
  All workflow/prompt/source/policy counters were `0`; the gate still failed
  only on the known monolith ratchet: orchestrator `49639` vs baseline `45441`,
  catalogue `35061` vs `32670`, and von_routes `18896` vs `18136`.
- Runtime attribution companion
  `.venv\Scripts\python.exe scripts\report_turn_decision_attribution.py --limit 10`
  timed out after about two minutes, so no architecture-integrity score or
  fallback-signature histogram was available.
- Treated the internal MCP deadline path as support-only for this scan:
  `transport.py` enforces bounded hard deadlines and exposes typed timeout,
  saturation, queue/handler timing, and late-result-discard facts;
  `workflow_mcp_tool_actions.py`, `mcp_tool_bridge.py`, `orchestrator.py`,
  route serialisation, and timing summaries preserve those facts without
  choosing domain workflows or authoring user-facing recovery wording.
- Rechecked production `WorkflowDefinition(...)`,
  `WorkflowRegistration(...)`, and `source="built_in"` surfaces. They remain
  watch items rather than fresh tickets while `_register_python_defined_workflows()`
  is empty and the purity registry reports Vontology-only runtime sources.
- No new Jira issues were created and no production code was changed by this
  review run.
