# VWL Implementation Audit 2026-03-24

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

That means the main remaining VWL contract failures are no longer about runtime registration authority alone. After `JVNAUTOSCI-1581`, they are concentrated in three places:

1. File-copy upload routing still depends on Python route maps and hard-coded downstream workflow IDs.
2. Testing Workflows still contain meeting-specific fixture preparation and benchmark-tier branching that should become generic testing-workflow surfaces.
3. Some workflow prompt bodies are still authored in Python and then persisted into Vontology prompt concepts.

The largest source-authority contract failure identified in the original audit has now been addressed by `JVNAUTOSCI-1581`: canonical workflow publication, template selection, routing metadata, and discovery exemplars resolve from Vontology-native authority first. The remaining justified extensions are:

- declarative typed-subworkflow route maps
- generic testing-fixture preparation and test-tier escalation semantics

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

The workflow-purity counters are accurate for runtime purity and now also flag Python-authored canonical workflow publication surfaces via `python_authored_canonical_workflow_source_count`, repo-seed authority drift via `repo_seed_authority_drift_path_count`, and guarded Vontology-first seed-fallback regressions via `vontology_first_seed_fallback_violation_count`.

They still do not fully measure all source-authoring drift. In particular, they do not yet count code-authored workflow prompt bodies, and they do not by themselves classify every hard-coded workflow-adjacent builder outside the canonical publication/template authority paths.

So the zeroed runtime-authority counters plus zero canonical-source and repo-seed-drift counters should now be read as "runtime authority is Vontology-backed, canonical publication sources are no longer Python-authored, repo-seed authority drift is mechanically guarded, and the repo now names the remaining filesystem artefacts honestly as seed bundles". They should still not be read as "workflow-first convergence is complete", because file-copy routing, testing-workflow extensions, prompt-source migration, and snapshot/diff tooling still remain.

## Exhaustive Findings

| Family | Locations | Current behaviour | Example workflows / task classes | Current VWL sufficient? | Equivalent VWL representation or smallest coherent extension | Other examples covered | Delete / migrate / retain | Migration risk, dependencies, priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Canonical workflow publication authority | Historical locations before `JVNAUTOSCI-1576`: `src/backend/workflows/workflow_concept_authority_service.py`; `src/backend/services/paper_representation_workflow_vontology_service.py`; `src/backend/services/talk_representation_workflow_vontology_service.py`; `src/backend/services/testing_workflow_vontology_service.py`. Transitional repo seed bundles now live under `src/backend/workflows/repo_seed_bundles/*.json`. | Resolved by `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585`. Canonical workflow publication now derives specs and workflow text from authoritative Vontology workflow state first, and the remaining filesystem artefacts are explicitly named as repo seed bundles rather than authored sources. | Chat narration, buttonify, tool-calling, turn execution, file-copy typing/upload/interpretation, planning, rumination, workflow authoring, scholarly/arXiv paper workflows, talk workflows, testing workflows. | Yes. | Keep the authoritative workflow definition as Vontology workflow graph data and retain only generic import, validation, publication, or export helpers in Python. Repo-side files remain seed-only. | Any future canonical workflow family. | Mark `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585` done for authority migration plus repo-affordance cleanup. Retain generic publication/import/validation utilities. | Low residual risk. The remaining risk is future regression, which is why `JVNAUTOSCI-1583` and `JVNAUTOSCI-1584` remain important. |
| Code-authored workflow prompts | `src/backend/services/testing_workflow_vontology_service.py:77`, `:102`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py:282`, `:482` | Prompt text is still created from Python constants/templates and then persisted into prompt concepts. | Meeting-invitation structure/evaluation prompts, gap-recovery candidate prompt. | Yes. | Store prompt bodies directly in Vontology prompt concepts and treat Python only as prompt resolution/rendering infrastructure. | Future workflow-repair prompts, authoring prompts, test-fixture prompts. | Migrate prompt text out of Python. Retain prompt rendering and prompt-resolution support. | Low migration risk. Depends mainly on adopting prompt-source discipline alongside workflow-source discipline. Priority `P2`. |
| Workflow templates in workflow creation and gap recovery | Historical locations before `JVNAUTOSCI-1577`: `src/backend/workflows/durable/workflow_creation_workflow.py`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py`. Transitional repo seed fixture now lives at `src/backend/workflows/repo_seed_bundles/workflow_template_seed_bundle.json`. | Resolved by `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585`. Template selection/rendering now resolves authoritative metadata from first-class Vontology template concepts and text relations, with repo seed hydration only when the authoritative template concepts are missing. | Text-driven workflow creation for scholarly-paper representation, PhD-student representation, single-step recovery candidate workflows. | Yes, for the covered cases. | Store reusable authored workflow-spec templates directly in Vontology-native structures and resolve them through generic template-profile selection/rendering support. Repo-side files remain seed-only. | User-onboarding workflows, new representation workflows, future recovery-generated candidate workflows. | Mark `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585` done for template-authority migration and repo-affordance cleanup. Retain the generic template loader/selector/renderer and fail-closed synthesis-policy checks. | Low residual risk. The main remaining risk is future regression back to file-authority paths rather than a missing capability. |
| Workflow discovery seeds and override-role heuristics | Historical workflow-family seed location before `JVNAUTOSCI-1577`: `src/backend/services/workflow_discovery_service.py`. Remaining generic fallback: `src/backend/services/workflow_override_policy_service.py`. Transitional repo seed metadata now lives in `src/backend/workflows/repo_seed_bundles/canonical_workflow_publication_seed_bundle.json` and `src/backend/workflows/repo_seed_bundles/testing_workflow_seed_bundle.json`. | Resolved for authority by `JVNAUTOSCI-1581`, with repo-affordance cleanup completed by `JVNAUTOSCI-1585`. Capability discovery now resolves routing profiles and discovery exemplars from authoritative Vontology text relations; repo-side metadata remains only as seed fallback when the authoritative workflow concepts are absent or incomplete. Override policy still retains a minimal generic fallback for legacy workflows that lack explicit routing metadata. | Workflow creation requests, workflow repair/meta workflow promotion, workflow introspection maintenance requests. | Yes, for the migrated cases. | Publish `workflow_routing_profile.v1` and `workflow_discovery_exemplars.v1` metadata directly from Vontology-native source data; incorporate exemplar text into authoritative capability-index text. Keep only minimal generic fallback while legacy workflows are backfilled. | Workflow repair, workflow introspection, testing maintenance workflows, future maintenance/meta workflows. | Mark `JVNAUTOSCI-1581` and `JVNAUTOSCI-1585` done for metadata authority migration plus repo-affordance cleanup. Retain the generic legacy fallback only until metadata coverage is complete. | Low-medium residual risk. The remaining risk is legacy workflows without backfilled routing metadata plus future reintroduction of file-authority shortcuts. |
| Hard-coded typed-subworkflow route maps in file-copy upload classification | `src/backend/workflows/durable/file_copy_upload_classification_workflow.py:52`, `:185`, `:290`; `src/backend/workflows/workflow_concept_authority_service.py:860` | Python owns the route keys, default downstream workflow IDs, confidence thresholds, env overrides, and fallback policy for upload routing. | Upload -> scholarly paper workflow, upload -> CV workflow, upload -> business-card workflow, upload -> meeting workflow. | No, not coherently. | Add a declarative typed-subworkflow route-map surface: route key, candidate workflow list, threshold policy, fallback mode, and fail-closed/no-op semantics should all be representable in metadata. | URL-ingestion routing, document subtype dispatch, future structured artefact-routing pipelines. | Retain generic file typing and route-execution infrastructure, but migrate route-map policy out of Python once the extension exists. | Medium risk. Depends on a clear route-map schema and loader support. Priority `P1`. |
| Testing-workflow-specific fixture preparation and benchmark escalation | `src/backend/workflows/durable/testing_workflow_actions.py:464`, `:483`; `src/backend/services/experiment_run_service.py:1452`; `src/backend/services/testing_workflow_vontology_service.py:260` | Meeting-invitation fixture preparation is a dedicated Python action. Tier-1 vs Tier-2 regression-suite branching is also owned by Python, with a capability-gap note recorded in the workflow metadata. | `prepare_meeting_invitation_experiment_spec`, synthetic regression-suite workflow, future acceptance tests for arXiv/talk/file-copy flows. | No, not fully. | Add generic testing-workflow fixture-preparation semantics and declarative test-tier/escalation semantics. The current Python services are legitimate temporary runtime support until those surfaces exist. | arXiv paper-ingestion testing, talk-representation testing, workflow-gap candidate evaluation, future benchmark-backed acceptance workflows. | Retain the generic runtime support temporarily. Migrate the meeting-specific action and benchmark-branch logic after the testing-workflow extension lands. | Medium-high risk. Depends on generic fixture schema design and benchmark-binding contract. Priority `P1`. |

## What Should Remain as Reusable Runtime Support

These areas are not themselves contract failures and should not be deleted merely because they are written in Python:

- `publish_canonical_chat_workflow_graphs(...)` and related validation/import helpers in `workflow_concept_authority_service.py` should remain as generic publication infrastructure. The problem is that workflow-family specs still live beside them in Python.
- Generic testing-theory and experiment-run actions in `src/backend/workflows/durable/testing_workflow_actions.py` are reusable execution surfaces, not workflow-specific drift.
- Deterministic paper/talk materialisation actions remain legitimate runtime primitives until VWL gains a general, declarative concept-mutation-and-verification surface.
- File typing and route execution are reusable runtime support. The route-map policy is the part that should move into VWL.

## Prioritised Extension and Migration Plan

1. `P0`: Completed 2026-03-24 via `JVNAUTOSCI-1582`: correct the workflow-authority contract so repo-side declarative workflow files are no longer counted as compliant authoritative VWL/Vontology assets.
2. `P1`: Completed 2026-03-24 via `JVNAUTOSCI-1581`: move canonical published workflow definitions, workflow-template authority, routing metadata, and discovery exemplars out of repo-authored bundles and into Vontology-native authoritative sources, leaving repo bundles seed-only.
3. `P1`: Add declarative typed-subworkflow route maps and migrate file-copy upload classification policy out of Python.
4. `P1`: Generalise testing-workflow fixture preparation and test-tier escalation semantics so meeting-specific and benchmark-specific logic does not keep reappearing in Python.
5. `P2`: Move remaining workflow prompt text out of Python and into Vontology prompt concepts as first-class authored artefacts.
6. `P2`: Completed 2026-03-24 via `JVNAUTOSCI-1585`: rename and quarantine repo seed bundle paths, filenames, schema identifiers, and helper names so the repo layout itself no longer advertises file-first workflow authority.
7. `P2`: Completed 2026-03-25 via `JVNAUTOSCI-1583`: add source-authority purity gates so repo-seed authority drift and Vontology-first fallback regressions now fail deterministically.
8. `P2`: `JVNAUTOSCI-1584`: add Vontology-authored snapshot/diff tooling so the stronger authority model remains easy to inspect without drifting back to file-first authoring.

## Residual Uncertainty

This audit was exhaustive across the current workflow runtime, authoring, discovery, routing, and testing modules under `src/backend/workflows` and `src/backend/services/*workflow*`, plus adjacent services used directly by those modules.

Residual uncertainty remains in two narrow areas:

- Generic concept-mutation services were not audited line-by-line unless a workflow module depended on them directly. There may still be future opportunities to turn some domain-specific durable actions into more general VWL mutation primitives.
- Workflow-purity telemetry now surfaces Python-authored canonical workflow sources, but it still does not directly count code-authored prompt bodies.

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

The parent epic `JVNAUTOSCI-833` should continue to track this report plus the follow-up migration/guardrail tasks created from it.
