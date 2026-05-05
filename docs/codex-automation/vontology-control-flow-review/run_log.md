# Run Log

## 2026-04-30

- Ran `python scripts\check_workflow_purity.py --verbose`; known failures remain
  tracked by `JVNAUTOSCI-2080`.
- Created `JVNAUTOSCI-2193` through `JVNAUTOSCI-2197`.
- Linked new drift tasks to `JVNAUTOSCI-1913` and parented them under
  `JVNAUTOSCI-1116`.
- Updated this memory set with concise detection and duplicate-avoidance notes.
- No production code was changed.

## 2026-04-30T10:06:41.5934960+12:00

- Compacted operational memory files at user request. Removed long per-run
  evidence blocks; kept issue keys, duplicate boundaries, and review heuristics.

## 2026-04-30T21:59:17.4992277+12:00

- Created `JVNAUTOSCI-2216` for file-copy entity materialisation contracts:
  candidate schemas, field-to-predicate bindings, note policy, and success
  criteria still live in Python after `JVNAUTOSCI-1952` moved inference toward
  prompt authority.
- Linked `JVNAUTOSCI-2216` to `JVNAUTOSCI-1913`, `JVNAUTOSCI-1952`,
  `JVNAUTOSCI-2193`, and `JVNAUTOSCI-2173`; added a visible Jira user-impact
  comment.
- Treated the arXiv launch-input extractor as covered by `JVNAUTOSCI-2212`
  rather than filing a duplicate.
- No production code was changed.

## 2026-05-01T03:11:18.0583273+12:00

- Created `JVNAUTOSCI-2219` for PhD-student workflow-authoring policy in
  Python: domain action ids, profile fields, concept/predicate defaults,
  handlers, template states, and tests now need represented Vontology/VWL
  ownership.
- Linked `JVNAUTOSCI-2219` to `JVNAUTOSCI-1913`, `JVNAUTOSCI-1307`,
  `JVNAUTOSCI-2195`, and `JVNAUTOSCI-2080`; added a visible Jira user-impact
  comment.
- Updated `JVNAUTOSCI-2080` with current `check_workflow_purity.py` evidence,
  including the additional repo-seed path
  `multilingual_concept_enrichment_vontology_service.py`.
- Inspected `tool_result_hint_actions.py` and `workflow_mcp_tool_actions.py`;
  treated them as generic support surfaces, not new drift tasks.
- No production code was changed.

## 2026-05-02T03:13:26.5808015+12:00

- Ran `python scripts\check_workflow_purity.py --verbose`; all counters were
  zero and the workflow-purity gate passed.
- Created `JVNAUTOSCI-2234` for concept-summary panel policy in
  `concept_summary_renderer_service.py`: renderer-id branches, domain panel
  builders, section labels, field order, and type exclusions are still
  Python-authored user-visible information policy.
- Created `JVNAUTOSCI-2235` for file-copy semantic typing and route hints in
  `file_copy_typing_service.py`: regexes, semantic type blueprints, score
  thresholds, and route-hint taxonomy are still Python-authored before being
  persisted as Vontology facts.
- Updated `JVNAUTOSCI-1985` with current evidence that
  `minimal_imposition_runtime_profile_vontology_service.py` still falls back
  to Python default decision policy/tool risk classes, including recent Gmail
  tool-risk entries.
- Inspected `conversation_turn_stage_model.py`,
  `renderer_applicability_vontology_service.py`, and
  `scripts/publish_email_resource_link_extraction_workflow.py`; left them as
  watch items rather than filing new tasks this run.
- No production code was changed.

## 2026-05-04T03:11:46.6211323+12:00

- Read the required repo guidance and the repo-local drift-review memories,
  then ran `python scripts\check_workflow_purity.py --verbose`; all counters
  were zero and the workflow-purity gate passed.
- Reviewed changed Python since the previous automation run, including arXiv
  email workflow repair/alignment, represented-artefact startup bootstrap,
  organisation membership/message-route fixes, and Vontology relationship
  extent lookup.
- Confirmed `JVNAUTOSCI-2260` already covers represented-artefact creation
  prompt/workflow authority being repopulated from repo seed files and startup
  Python repair; updated the memory index rather than filing a duplicate.
- Created `JVNAUTOSCI-2262` for
  `knowledge_acquisition_profile_vontology_service.py`: Python-authored
  decision policy, predicate priorities, source confidence adjustments, and
  auto-apply thresholds are loaded/bootstrapped into the rumination workflow.
  Linked it to `JVNAUTOSCI-1913`, `JVNAUTOSCI-1985`, and `JVNAUTOSCI-2260`,
  and added a visible Jira user-impact comment.
- Inspected the arXiv repair script, role resolver stub, and low-level arXiv
  proxy adapter; left them as watch items rather than new tasks.
- No production code was changed.

## 2026-05-05T03:07:28.7273604+12:00

- Read required repo guidance and repo-local drift-review memories; the global
  automation memory file was absent.
- Ran `python scripts\check_workflow_purity.py --verbose`; all counters were
  zero and the workflow-purity gate passed.
- Reviewed Python changes since the last run: per-model LLM timeout/settings
  plumbing and model-family prompt variant resolution.
- Treated model-family prompt variants as generic Vontology-authored prompt
  selection support, not a new drift task.
- Updated `JVNAUTOSCI-1957` with current evidence that structural predicate
  metadata is only partially migrated: Python defaults still merge/fallback
  when Vontology metadata is missing or partial.
- Created `JVNAUTOSCI-2264` for task-status event trigger filtering in
  `workflow_event_integration_service.py`: Python/env status allow-lists decide
  whether `task.status_changed` reaches persisted event bindings. Linked it to
  `JVNAUTOSCI-1913` and `JVNAUTOSCI-1605`, and added a visible user-impact
  comment.
- No production code was changed.

## 2026-05-06T03:08:27.1929297+12:00

- Read required repo guidance and repo-local drift-review memories; the global
  automation memory file was absent at start.
- `pdm run python scripts\check_workflow_purity.py --verbose` produced no
  visible output in this automation shell, and the checkout `.venv` Python was
  inaccessible. The same purity gate passed with direct `python
  scripts\check_workflow_purity.py --verbose`; all counters were zero.
- Reviewed Python changes since the previous run, especially workflow model
  selection, model execution budget policy, paper recommendation policy
  materialisation, and the KR materialisation workflow publisher.
- Created `JVNAUTOSCI-2271` for
  `scripts/publish_kr_materialisation_workflows.py`: it embeds production
  workflow prompts, discovery/routing metadata, required-effect contracts,
  control flow, and response templates in Python before publishing them into
  Vontology. Linked it to `JVNAUTOSCI-1913` and `JVNAUTOSCI-2268`, and added a
  visible Jira user-impact comment.
- Treated the model budget/policy-resolution changes as represented metadata
  support, not new drift tasks. Treated the current paper recommendation
  residual fallback as covered by existing recommender authority tasks unless
  fresh live evidence shows a current fail-open user impact.
- No production code was changed.
