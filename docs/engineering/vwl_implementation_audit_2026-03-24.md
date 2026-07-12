# VWL Implementation Audit 2026-03-24

> **Document status: Frozen audit and correction record from March 2026.** Its
> authority lessons may remain relevant, but completion state, code anchors,
> fallback paths, and Jira sequencing require current verification. Use the VWL
> manual and live Vontology/runtime evidence as the implementation reference.

Task: `JVNAUTOSCI-1575`  
Date: 2026-03-24

Correction after `JVNAUTOSCI-1582`:

- the earlier wording in this audit overstated the completion state of `JVNAUTOSCI-1576` and `JVNAUTOSCI-1577` by treating repo-side JSON workflow/template bundles as acceptable authoritative VWL/Vontology assets;
- the stronger contract is now explicit: direct authoritative workflow/template/routing/prompt logic belongs in Vontology-native artefacts, while repo-side files count only as migration seeds, generated snapshots, test fixtures, or exports;
- `JVNAUTOSCI-1581` now tracks the substantive migration back to Vontology-native workflow authority, while `JVNAUTOSCI-1582` through `JVNAUTOSCI-1585` provide the policy, purity, tooling, and repo-affordance guardrails around that migration.

Update after `JVNAUTOSCI-1581`:

- canonical workflow publication now derives default publication specs and workflow text from authoritative Vontology state first, with repo-side workflow bundles retained only as missing/incomplete seed fallback;
- workflow-template authority now lives in first-class Vontology template concepts plus template/profile text relations, and `workflow_template_profile_service.py` hydrates from the old JSON bundle only when those template concepts are absent;
- family bootstrap helpers now preserve any valid current Vontology workflow family rather than enforcing repo-seed parity on every run;
- repo-side workflow/template bundles remain in the tree only as seed fixtures pending `JVNAUTOSCI-1585` affordance cleanup.

Update after `JVNAUTOSCI-1585`:

- the misleading `src/backend/workflows/authored_sources` path has been retired in favour of `src/backend/workflows/repo_seed_bundles`;
- the generic seed-materialisation support now lives in `src/backend/services/workflow_repo_seed_bootstrap.py`, and the remaining repo-side helper names explicitly say `repo_seed` rather than `authored`;
- repo bundle filenames and schema versions now declare themselves as repo seed bundles, and the directory carries a README that states the non-authoritative contract directly.

Update after `JVNAUTOSCI-1583`:

- workflow-purity telemetry now treats repo-seed authority drift as a first-class regression via `repo_seed_authority_drift_path_count`;
- workflow-purity telemetry now also guards the two current Vontology-first seed-fallback paths via `vontology_first_seed_fallback_violation_count`;
- deterministic tests now fail if production code introduces repo-seed workflow/template authority outside the designated seed/bootstrap support modules, or if the guarded authority loaders stop resolving Vontology before seed fallback.

Update after `JVNAUTOSCI-1584`:

- `scripts/workflow_authority_review_snapshot.py` now provides deterministic `export` and `diff` commands for workflow-authority review;
- generated review snapshots now live under `docs/generated/workflow_authority_review_snapshots/`, with filenames explicitly marked as `.generated.json` and README guidance stating that they are non-authoritative derived artefacts;
- the snapshot export reads the same Vontology-first workflow and template authority surfaces the runtime consumes, so review ergonomics improve without reintroducing repo-side workflow authority.

Update after `JVNAUTOSCI-1580`:

- remaining workflow-governed prompt bodies were migrated out of Python constants/templates and into authoritative Vontology prompt concepts;
- `workflow_prompt_authority_service.py` now centralises only generic prompt-concept creation, validation, linking, and rendering support, while workflow/testing/bootstrap services no longer persist prompt bodies from Python;
- planning, enrichment, workflow-gap analysis/test/candidate execution, testing-workflow prompt support, parent-specificity prompt support, and workflow-description prompt support now all fail closed when their authoritative Vontology prompt concepts are missing or empty;
- workflow-purity telemetry now includes `python_authored_workflow_prompt_source_count`; current expected value is `0`.

Update after `JVNAUTOSCI-1576`:

- canonical workflow publication sources were migrated out of Python workflow-family spec builders and into version-controlled repo seed bundles under `src/backend/workflows/repo_seed_bundles/*.json`
- generic publication/materialisation support now lives in `src/backend/workflows/workflow_concept_authority_service.py` and `src/backend/services/workflow_repo_seed_bootstrap.py`
- workflow-purity telemetry now includes `python_authored_canonical_workflow_source_count`; current expected value is `0`

Update after `JVNAUTOSCI-1577`:

- workflow creation and workflow-gap candidate specs now resolve through repo seed template bundles in `src/backend/workflows/repo_seed_bundles/workflow_template_seed_bundle.json`
- workflow-family-specific workflow-creation discovery seeding was removed from `src/backend/services/workflow_discovery_service.py`
- canonical governance workflows now publish routing profiles and discovery exemplars from authored metadata, and capability-index text incorporates those exemplars for retrieval
- routing-role heuristics remain only as a minimal generic fallback for legacy workflows that still lack explicit routing metadata

## Executive Summary

As of 2026-03-24, Von's workflow runtime is substantially cleaner than earlier conversion-sweep documents suggest. Running `pdm run python scripts/workflow_purity_report.py` produced:

- `built_in_registration_count=0`
- `remaining_python_workflow_family_count=0`
- `direct_instance_create_callsite_count=0`
- `env_event_binding_count=0`
- `legacy_selector_mode_count=0`
- `builtin_capability_override_count=0`
- `non_vontology_discoverable_workflow_count=0`
- `repo_seed_authority_drift_path_count=0`
- `vontology_first_seed_fallback_violation_count=0`

That means the concrete workflow-authority and source-authority failures identified by this audit are now resolved. The original migration set is complete:

- canonical workflow publication, template selection, routing metadata, and discovery exemplars resolve from Vontology-native authority first;
- typed-subworkflow route-map policy resolves from authoritative Vontology metadata;
- testing-workflow fixture preparation and regression-suite escalation semantics resolve from reusable testing metadata rather than Python-held workflow branches;
- workflow-governed prompt bodies now resolve from authoritative Vontology prompt concepts, with Python limited to generic resolution/rendering/validation infrastructure.

Any remaining work in this area is therefore no longer “finish the identified migration”; it is either regression prevention, future capability extension, or broader ontology-mutation generalisation outside the concrete findings below.

## Areas Reviewed

The audit covered these current workflow-first surfaces:

- `docs/engineering/von_workflow_language_manual.md`
- `docs/engineering/workflow_first_conversion_sweep_jvnautosci_1210.md`
- `src/backend/workflows/workflow_purity_report.py`
- `scripts/workflow_purity_report.py`
- `src/backend/workflows/workflow_concept_authority_service.py`
- `src/backend/workflows/durable/workflow_creation_workflow.py`
- `src/backend/workflows/durable/workflow_gap_recovery_workflow.py`
- `src/backend/workflows/durable/file_copy_upload_classification_workflow.py`
- `src/backend/workflows/durable/testing_workflow_actions.py`
- `src/backend/workflows/workflow_selector.py`
- `src/backend/services/workflow_discovery_service.py`
- `src/backend/services/workflow_override_policy_service.py`
- `src/backend/services/paper_representation_workflow_vontology_service.py`
- `src/backend/services/talk_representation_workflow_vontology_service.py`
- `src/backend/services/testing_workflow_vontology_service.py`
- `src/backend/services/experiment_run_service.py`

The audit also used `workflow_list_definitions(limit=200)` to cross-check the current authoritative registry state.

## Baseline Interpretation

The workflow-purity counters are accurate for runtime purity and now also flag Python-authored canonical workflow publication surfaces via `python_authored_canonical_workflow_source_count`, Python-authored workflow prompt sources via `python_authored_workflow_prompt_source_count`, repo-seed authority drift via `repo_seed_authority_drift_path_count`, and guarded Vontology-first seed-fallback regressions via `vontology_first_seed_fallback_violation_count`.

They still do not classify every possible hard-coded workflow-adjacent builder outside the canonical publication/template/prompt authority paths, so the report remains a strong regression gate rather than a proof that no further design opportunities exist.

`JVNAUTOSCI-1584` complements those counters by making the authoritative KB state easier to inspect directly. That matters because ergonomics was part of the original failure mode: if Vontology-first review is awkward, developers and agents will keep drifting back toward file-first local authority.

So the zeroed runtime-authority counters plus zero canonical-source, prompt-source, and repo-seed-drift counters should now be read as "runtime authority is Vontology-backed, canonical publication sources are no longer Python-authored, workflow-governed prompt bodies are no longer Python-authored, repo-seed authority drift is mechanically guarded, the repo names the remaining filesystem artefacts honestly as seed bundles, and typed-subworkflow plus testing-workflow policy can be authored as authoritative Vontology metadata". They should still not be read as "all future workflow design work is done", because generic capability evolution can still reveal further reusable VWL/runtime extensions.

## Exhaustive Findings

| Family | Locations | Current behaviour | Example workflows / task classes | Current VWL sufficient? | Equivalent VWL representation or smallest coherent extension | Other examples covered | Delete / migrate / retain | Migration risk, dependencies, priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Canonical workflow publication authority | Historical locations before `JVNAUTOSCI-1576`: `src/backend/workflows/workflow_concept_authority_service.py`; `src/backend/services/paper_representation_workflow_vontology_service.py`; `src/backend/services/talk_representation_workflow_vontology_service.py`; `src/backend/services/testing_workflow_vontology_service.py`. Transitional repo seed bundles now live under `src/backend/workflows/repo_seed_bundles/*.json`. | Resolved by `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585`. Canonical workflow publication now derives specs and workflow text from authoritative Vontology workflow state first, and the remaining filesystem artefacts are explicitly named as repo seed bundles rather than authored sources. | Chat narration, buttonify, tool-calling, turn execution, file-copy typing/upload/interpretation, planning, rumination, workflow authoring, scholarly/arXiv paper workflows, talk workflows, testing workflows. | Yes. | Keep the authoritative workflow definition as Vontology workflow graph data and retain only generic import, validation, publication, or export helpers in Python. Repo-side files remain seed-only. | Any future canonical workflow family. | Mark `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585` done for authority migration plus repo-affordance cleanup. Retain generic publication/import/validation utilities. | Low residual risk. The remaining risk is future regression, which is why `JVNAUTOSCI-1583` and `JVNAUTOSCI-1584` remain important. |
| Code-authored workflow prompts | Historical locations before `JVNAUTOSCI-1580`: `src/backend/services/testing_workflow_vontology_service.py`; `src/backend/services/workflow_gap_vontology_service.py`; `src/backend/services/parent_specificity_vontology_service.py`; `src/backend/services/workflow_description_vontology_service.py`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py`; `src/backend/workflows/durable/planning_workflow.py`; `src/backend/workflows/durable/enrichment_workflow.py`. | Resolved by `JVNAUTOSCI-1580`. Workflow-governed prompt bodies now live in authoritative Vontology prompt concepts, while Python retains only generic prompt validation/linking/rendering infrastructure and fail-closed runtime behaviour when prompt concepts are missing or empty. | Meeting-invitation structure/evaluation prompts, workflow-gap analysis/test/candidate prompts, parent-specificity analysis prompt, concept/workflow description prompts, planning prompt, enrichment prompt resolution. | Yes. | Store prompt bodies directly in Vontology prompt concepts and treat Python only as prompt resolution/rendering infrastructure. | Future workflow-repair prompts, authoring prompts, test-fixture prompts, planning prompts, enrichment prompts. | Mark `JVNAUTOSCI-1580` done. Retain generic prompt rendering, validation, workflow-linking, and fail-closed diagnostics support. | Low residual risk. The main remaining risk is regression back to Python-authored prompt bodies, now guarded by `python_authored_workflow_prompt_source_count`. |
| Workflow templates in workflow creation and gap recovery | Historical locations before `JVNAUTOSCI-1577`: `src/backend/workflows/durable/workflow_creation_workflow.py`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py`. Transitional repo seed fixture now lives at `src/backend/workflows/repo_seed_bundles/workflow_template_seed_bundle.json`. | Resolved by `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585`. Template selection/rendering now resolves authoritative metadata from first-class Vontology template concepts and text relations, with repo seed hydration only when the authoritative template concepts are missing. | Text-driven workflow creation for scholarly-paper representation, PhD-student representation, single-step recovery candidate workflows. | Yes, for the covered cases. | Store reusable authored workflow-spec templates directly in Vontology-native structures and resolve them through generic template-profile selection/rendering support. Repo-side files remain seed-only. | User-onboarding workflows, new representation workflows, future recovery-generated candidate workflows. | Mark `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585` done for template-authority migration and repo-affordance cleanup. Retain the generic template loader/selector/renderer and fail-closed synthesis-policy checks. | Low residual risk. The main remaining risk is future regression back to file-authority paths rather than a missing capability. |
| Workflow discovery seeds and override-role heuristics | Historical workflow-family seed location before `JVNAUTOSCI-1577`: `src/backend/services/workflow_discovery_service.py`. Remaining generic fallback: `src/backend/services/workflow_override_policy_service.py`. Transitional repo seed metadata now lives in `src/backend/workflows/repo_seed_bundles/canonical_workflow_publication_seed_bundle.json` and `src/backend/workflows/repo_seed_bundles/testing_workflow_seed_bundle.json`. | Resolved for authority by `JVNAUTOSCI-1581`, with repo-affordance cleanup completed by `JVNAUTOSCI-1585`. Capability discovery now resolves routing profiles and discovery exemplars from authoritative Vontology text relations; repo-side metadata remains only as seed fallback when the authoritative workflow concepts are absent or incomplete. Override policy still retains a minimal generic fallback for legacy workflows that lack explicit routing metadata. | Workflow creation requests, workflow repair/meta workflow promotion, workflow introspection maintenance requests. | Yes, for the migrated cases. | Publish `workflow_routing_profile.v1` and `workflow_discovery_exemplars.v1` metadata directly from Vontology-native source data; incorporate exemplar text into authoritative capability-index text. Keep only minimal generic fallback while legacy workflows are backfilled. | Workflow repair, workflow introspection, testing maintenance workflows, future maintenance/meta workflows. | Mark `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585` done for metadata authority migration plus repo-affordance cleanup. Retain the generic legacy fallback only until metadata coverage is complete. | Low-medium residual risk. The remaining risk is legacy workflows without backfilled routing metadata plus future reintroduction of file-authority shortcuts. |
| Hard-coded typed-subworkflow route maps in file-copy upload classification | Historical locations before `JVNAUTOSCI-1578`: `src/backend/workflows/durable/file_copy_upload_classification_workflow.py`; `src/backend/workflows/workflow_concept_authority_service.py`. Authoritative route-map metadata is now published on `#V#file_copy_upload_classification_workflow` via `#V#hasWorkflowTypedSubworkflowRouteMapJson`. | Resolved by `JVNAUTOSCI-1578`. Python now retains generic file typing, score selection, and route execution support, while candidate workflow lists, threshold defaults, and unavailable/low-confidence fallback semantics resolve from authoritative Vontology metadata. | Upload -> scholarly paper workflow, upload -> CV workflow, upload -> business-card workflow, upload -> meeting workflow. | Yes. | Keep the declarative route-map surface generic so future artefact-routing workflows can publish their own route maps without adding workflow-family-specific Python. | URL-ingestion routing, document subtype dispatch, future structured artefact-routing pipelines. | Mark `JVNAUTOSCI-1578` done, retain the generic loader/classifier support, and keep classification fail-closed when route-map metadata is missing or invalid. | Low-medium residual risk. The remaining risk is future drift back to code-owned route maps or incomplete metadata publication in new workflow families. |
| Testing-workflow-specific fixture preparation and benchmark escalation | Historical locations before `JVNAUTOSCI-1579`: `src/backend/workflows/durable/testing_workflow_actions.py`; `src/backend/services/experiment_run_service.py`; `src/backend/services/testing_workflow_vontology_service.py`. Canonical testing workflows now publish `testing_experiment_scenario_template.v1` and `testing_regression_suite_policy.v1` metadata via `hasInputMap` JSON payloads. | Resolved by `JVNAUTOSCI-1579`. Meeting fixture preparation now goes through the generic `testing.prepare_experiment_spec` runtime surface, and regression-suite tier/escalation behaviour resolves from workflow-authored suite policy metadata rather than Python-owned benchmark branching. The legacy meeting helper remains only as a compatibility alias. | Meeting-invitation testing workflow, synthetic workflow regression suite, future acceptance tests for arXiv/talk/file-copy flows. | Yes. | Keep the scenario-template and suite-policy semantics generic so future testing workflows can author their own fixtures and escalation rules in VWL/Vontology. | arXiv paper-ingestion testing, talk-representation testing, workflow-gap candidate evaluation, future benchmark-backed acceptance workflows. | Mark `JVNAUTOSCI-1579` done and retain only the generic runtime/template-resolution support. Do not reintroduce workflow-specific Python branches for new testing scenarios. | Low-medium residual risk. The remaining risk is drift back to workflow-specific helpers instead of publishing scenario/policy metadata, not a missing capability. |

## What Should Remain as Reusable Runtime Support

These areas are not themselves contract failures and should not be deleted merely because they are written in Python:

- `publish_canonical_chat_workflow_graphs(...)` and related validation/import helpers in `workflow_concept_authority_service.py` should remain as generic publication infrastructure. The problem is that workflow-family specs still live beside them in Python.
- Generic testing-theory and experiment-run actions in `src/backend/workflows/durable/testing_workflow_actions.py` are reusable execution surfaces, not workflow-specific drift.
- Deterministic paper/talk materialisation actions remain legitimate runtime primitives until VWL gains a general, declarative concept-mutation-and-verification surface.
- File typing and route execution are reusable runtime support. The route-map policy is the part that should move into VWL.

## Prioritised Extension and Migration Plan

1. `P0`: Completed 2026-03-24 via `JVNAUTOSCI-1582`: correct the workflow-authority contract so repo-side declarative workflow files are no longer counted as compliant authoritative VWL/Vontology assets.
2. `P1`: Completed 2026-03-24 via `JVNAUTOSCI-1581`: move canonical published workflow definitions, workflow-template authority, routing metadata, and discovery exemplars out of repo-authored bundles and into Vontology-native authoritative sources, leaving repo bundles seed-only.
3. `P1`: Completed 2026-03-25 via `JVNAUTOSCI-1578`: add declarative typed-subworkflow route maps and migrate file-copy upload classification policy out of Python.
4. `P1`: Completed 2026-03-25 via `JVNAUTOSCI-1579`: generalise testing-workflow fixture preparation and test-tier escalation semantics so meeting-specific and benchmark-specific logic no longer needs to reappear in Python.
5. `P2`: Completed 2026-03-25 via `JVNAUTOSCI-1580`: move remaining workflow prompt text out of Python and into Vontology prompt concepts as first-class authored artefacts.
6. `P2`: Completed 2026-03-24 via `JVNAUTOSCI-1585`: rename and quarantine repo seed bundle paths, filenames, schema identifiers, and helper names so the repo layout itself no longer advertises file-first workflow authority.
7. `P2`: Completed 2026-03-25 via `JVNAUTOSCI-1583`: add source-authority purity gates so repo-seed authority drift and Vontology-first fallback regressions now fail deterministically.
8. `P2`: Completed 2026-03-25 via `JVNAUTOSCI-1584`: add Vontology-authored snapshot/diff tooling so the stronger authority model remains easy to inspect without drifting back to file-first authoring.

## Residual Uncertainty

This audit was exhaustive across the current workflow runtime, authoring, discovery, routing, and testing modules under `src/backend/workflows` and `src/backend/services/*workflow*`, plus adjacent services used directly by those modules.

Residual uncertainty remains in one narrow area:

- Generic concept-mutation services were not audited line-by-line unless a workflow module depended on them directly. There may still be future opportunities to turn some domain-specific durable actions into more general VWL mutation primitives.

No additional workflow-family-specific authoring surfaces were found by broad searches over:

- `_CanonicalWorkflowPublicationSpec`
- `_build_*workflow_spec`
- `publish_canonical_chat_workflow_graphs`
- `routing_profile`
- `specialised_workflow_ids`
- workflow-gap candidate-spec builders

## Guidance Updates

`JVNAUTOSCI-1582` now justifies further `AGENTS.md` and manual changes. The durable additions are:

- a named warning against repo-declarative masquerading as KB-first;
- an explicit no-self-serving-policy-relaxation rule for workflow-authority tasks;
- a workflow-authority readiness/closure checklist requiring the authoritative KB artefacts to be named up front and requiring closure checks against permissive docs drift and repo-file dependency.

The next durable documentation changes should happen when the remaining extension families above land, at which point `docs/engineering/von_workflow_language_manual.md` should be updated further with:

- typed-subworkflow route-map semantics
- testing-fixture preparation and tier-escalation semantics
- generated snapshot/export guidance once `JVNAUTOSCI-1584` lands
- prompt-source discipline and fail-closed workflow prompt semantics, now landed via `JVNAUTOSCI-1580`

The parent epic `JVNAUTOSCI-833` should continue to track this report plus the follow-up migration/guardrail tasks created from it.
