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
