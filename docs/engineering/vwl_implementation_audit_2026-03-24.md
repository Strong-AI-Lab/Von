# VWL Implementation Audit 2026-03-24

Task: `JVNAUTOSCI-1575`  
Date: 2026-03-24

Update after `JVNAUTOSCI-1576`:

- canonical workflow publication sources were migrated out of Python workflow-family spec builders and into version-controlled authored workflow source bundles under `src/backend/workflows/authored_sources/*.json`
- generic publication/materialisation support now lives in `src/backend/workflows/workflow_concept_authority_service.py` and `src/backend/services/workflow_authored_source_bootstrap.py`
- workflow-purity telemetry now includes `python_authored_canonical_workflow_source_count`; current expected value is `0`

Update after `JVNAUTOSCI-1577`:

- workflow creation and workflow-gap candidate specs now resolve through authored template bundles in `src/backend/workflows/authored_sources/workflow_template_profiles.json`
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

That means the main remaining VWL contract failures are no longer about runtime registration authority. After `JVNAUTOSCI-1576` and `JVNAUTOSCI-1577`, they are concentrated in three places:

1. File-copy upload routing still depends on Python route maps and hard-coded downstream workflow IDs.
2. Testing Workflows still contain meeting-specific fixture preparation and benchmark-tier branching that should become generic testing-workflow surfaces.
3. Some workflow prompt bodies are still authored in Python and then persisted into Vontology prompt concepts.

The largest current contract failure is therefore no longer "canonical workflows are authored in Python first". That part is now addressed for canonical published workflow families, and `JVNAUTOSCI-1577` also removed the current workflow-template/discovery contract failure for the migrated cases. The remaining justified extensions are:

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

The workflow-purity counters are accurate for runtime purity and now also flag Python-authored canonical workflow publication surfaces via `python_authored_canonical_workflow_source_count`.

They still do not fully measure all source-authoring drift. In particular, they do not yet count code-authored workflow prompt bodies, and they do not by themselves classify every hard-coded workflow-template builder outside the canonical publication path.

So the zeroed runtime-authority counters plus a zero canonical-source counter should be read as "runtime authority is Vontology-backed and canonical publication sources are no longer Python-authored", not "workflow-first convergence is complete".

## Exhaustive Findings

| Family | Locations | Current behaviour | Example workflows / task classes | Current VWL sufficient? | Equivalent VWL representation or smallest coherent extension | Other examples covered | Delete / migrate / retain | Migration risk, dependencies, priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Python-authored canonical workflow publication specs | Historical locations before `JVNAUTOSCI-1576`: `src/backend/workflows/workflow_concept_authority_service.py`; `src/backend/services/paper_representation_workflow_vontology_service.py`; `src/backend/services/talk_representation_workflow_vontology_service.py`; `src/backend/services/testing_workflow_vontology_service.py`. Current source bundles: `src/backend/workflows/authored_sources/*.json`. | Resolved for canonical published workflow families. Canonical workflow graphs are now sourced from version-controlled workflow bundles and imported through the generic publication/materialisation path. | Chat narration, buttonify, tool-calling, turn execution, file-copy typing/upload/interpretation, planning, rumination, workflow authoring, scholarly/arXiv paper workflows, talk workflows, testing workflows. | Yes. | Store the authoritative workflow definition directly as Vontology workflow graph data or as version-controlled workflow source assets imported through the generic publication/materialisation path. Keep only generic import, validation, and publication helpers in Python. | Any future canonical workflow family. | Completed by `JVNAUTOSCI-1576` for the currently published canonical families. Retain the generic publication/import/validation utilities and reject future workflow-family-specific spec builders. | Migration landed. Ongoing risk is regression, so keep purity telemetry and review discipline in place. |
| Code-authored workflow prompts | `src/backend/services/testing_workflow_vontology_service.py:77`, `:102`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py:282`, `:482` | Prompt text is still created from Python constants/templates and then persisted into prompt concepts. | Meeting-invitation structure/evaluation prompts, gap-recovery candidate prompt. | Yes. | Store prompt bodies directly in Vontology prompt concepts and treat Python only as prompt resolution/rendering infrastructure. | Future workflow-repair prompts, authoring prompts, test-fixture prompts. | Migrate prompt text out of Python. Retain prompt rendering and prompt-resolution support. | Low migration risk. Depends mainly on adopting prompt-source discipline alongside workflow-source discipline. Priority `P2`. |
| Hard-coded workflow templates in workflow creation and gap recovery | Historical locations before `JVNAUTOSCI-1577`: `src/backend/workflows/durable/workflow_creation_workflow.py`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py`. Current authored template bundle: `src/backend/workflows/authored_sources/workflow_template_profiles.json`. | Resolved for the current workflow-creation default/scholarly/PhD templates and the workflow-gap candidate execution template. Template selection/rendering now flows through a generic authored template-profile service. | Text-driven workflow creation for scholarly-paper representation, PhD-student representation, single-step recovery candidate workflows. | Yes, for the covered cases. | Store reusable authored workflow-spec templates in an `authored_workflow_template_bundle.v1` bundle and resolve them through generic template-profile selection/rendering support. | User-onboarding workflows, new representation workflows, future recovery-generated candidate workflows. | Completed by `JVNAUTOSCI-1577` for the currently migrated templates. Retain only the generic template loader/selector/renderer and fail-closed synthesis-policy checks. | Migration landed. Residual risk is regression or future reintroduction of workflow-family builders. |
| Workflow discovery seeds and override-role heuristics | Historical workflow-family seed location before `JVNAUTOSCI-1577`: `src/backend/services/workflow_discovery_service.py`. Remaining generic fallback: `src/backend/services/workflow_override_policy_service.py`. Current authored metadata locations: `src/backend/workflows/authored_sources/canonical_workflow_publication_specs.json`; `src/backend/workflows/authored_sources/testing_workflows.json`. | Resolved for the migrated governance and testing workflows. Workflow-family-specific regex seeding was removed; capability discovery now uses authored discovery exemplars and routing profiles. Override policy still retains a minimal generic fallback for legacy workflows that lack explicit routing metadata. | Workflow creation requests, workflow repair/meta workflow promotion, workflow introspection maintenance requests. | Yes, for the migrated cases. | Publish `workflow_routing_profile.v1` and `workflow_discovery_exemplars.v1` metadata from authored sources; incorporate exemplar text into authoritative capability-index text. Keep only minimal generic fallback while legacy workflows are backfilled. | Workflow repair, workflow introspection, testing maintenance workflows, future maintenance/meta workflows. | Completed by `JVNAUTOSCI-1577` for the currently migrated cases. Retain the generic legacy fallback only until metadata coverage is complete. | Low risk for migrated cases. Remaining risk is legacy workflows without backfilled routing metadata. |
| Hard-coded typed-subworkflow route maps in file-copy upload classification | `src/backend/workflows/durable/file_copy_upload_classification_workflow.py:52`, `:185`, `:290`; `src/backend/workflows/workflow_concept_authority_service.py:860` | Python owns the route keys, default downstream workflow IDs, confidence thresholds, env overrides, and fallback policy for upload routing. | Upload -> scholarly paper workflow, upload -> CV workflow, upload -> business-card workflow, upload -> meeting workflow. | No, not coherently. | Add a declarative typed-subworkflow route-map surface: route key, candidate workflow list, threshold policy, fallback mode, and fail-closed/no-op semantics should all be representable in metadata. | URL-ingestion routing, document subtype dispatch, future structured artefact-routing pipelines. | Retain generic file typing and route-execution infrastructure, but migrate route-map policy out of Python once the extension exists. | Medium risk. Depends on a clear route-map schema and loader support. Priority `P1`. |
| Testing-workflow-specific fixture preparation and benchmark escalation | `src/backend/workflows/durable/testing_workflow_actions.py:464`, `:483`; `src/backend/services/experiment_run_service.py:1452`; `src/backend/services/testing_workflow_vontology_service.py:260` | Meeting-invitation fixture preparation is a dedicated Python action. Tier-1 vs Tier-2 regression-suite branching is also owned by Python, with a capability-gap note recorded in the workflow metadata. | `prepare_meeting_invitation_experiment_spec`, synthetic regression-suite workflow, future acceptance tests for arXiv/talk/file-copy flows. | No, not fully. | Add generic testing-workflow fixture-preparation semantics and declarative test-tier/escalation semantics. The current Python services are legitimate temporary runtime support until those surfaces exist. | arXiv paper-ingestion testing, talk-representation testing, workflow-gap candidate evaluation, future benchmark-backed acceptance workflows. | Retain the generic runtime support temporarily. Migrate the meeting-specific action and benchmark-branch logic after the testing-workflow extension lands. | Medium-high risk. Depends on generic fixture schema design and benchmark-binding contract. Priority `P1`. |

## What Should Remain as Reusable Runtime Support

These areas are not themselves contract failures and should not be deleted merely because they are written in Python:

- `publish_canonical_chat_workflow_graphs(...)` and related validation/import helpers in `workflow_concept_authority_service.py` should remain as generic publication infrastructure. The problem is that workflow-family specs still live beside them in Python.
- Generic testing-theory and experiment-run actions in `src/backend/workflows/durable/testing_workflow_actions.py` are reusable execution surfaces, not workflow-specific drift.
- Deterministic paper/talk materialisation actions remain legitimate runtime primitives until VWL gains a general, declarative concept-mutation-and-verification surface.
- File typing and route execution are reusable runtime support. The route-map policy is the part that should move into VWL.

## Prioritised Extension and Migration Plan

1. `P1`: Completed 2026-03-24 via `JVNAUTOSCI-1576`: move canonical published workflow definitions out of Python spec builders and into direct VWL/Vontology-authored sources.
2. `P1`: Completed 2026-03-24 via `JVNAUTOSCI-1577`: add declarative workflow-template profiles and migrate workflow creation / gap recovery off hard-coded template builders.
3. `P1`: Add declarative typed-subworkflow route maps and migrate file-copy upload classification policy out of Python.
4. `P1`: Generalise testing-workflow fixture preparation and test-tier escalation semantics so meeting-specific and benchmark-specific logic does not keep reappearing in Python.
5. `P2`: Completed 2026-03-24 via `JVNAUTOSCI-1577`: backfill routing profiles for the migrated governance/testing workflows and add declarative discovery exemplars so workflow-specific discovery seeding can disappear. Residual generic fallback remains only for legacy workflows without explicit metadata.
6. `P2`: Move remaining workflow prompt text out of Python and into Vontology prompt concepts as first-class authored artefacts.

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

The durable policy update required by this audit is already reflected in `AGENTS.md`: if a workflow can be implemented through a genuine VWL extension, that extension should be made first and the workflow should then be implemented fully in VWL.

No further `AGENTS.md` change is justified by this task alone. `JVNAUTOSCI-1577` now justifies the manual updates for:

- workflow-template profile semantics
- discovery-exemplar semantics

The next durable documentation change should happen when the remaining extension families above land, at which point `docs/engineering/von_workflow_language_manual.md` should be updated further with:

- typed-subworkflow route-map semantics
- testing-fixture preparation and tier-escalation semantics

The parent epic `JVNAUTOSCI-833` should receive a short summary comment pointing to this report and the follow-up migration/extension tasks created from it.
