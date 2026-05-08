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
