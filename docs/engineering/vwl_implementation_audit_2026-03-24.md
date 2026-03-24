# VWL Implementation Audit 2026-03-24

Task: `JVNAUTOSCI-1575`  
Date: 2026-03-24

## Executive Summary

As of 2026-03-24, Von's workflow runtime is substantially cleaner than earlier conversion-sweep documents suggest. Running `pdm run python scripts/workflow_purity_report.py` produced:

- `built_in_registration_count=0`
- `remaining_python_workflow_family_count=0`
- `direct_instance_create_callsite_count=0`
- `env_event_binding_count=0`
- `legacy_selector_mode_count=0`
- `builtin_capability_override_count=0`
- `non_vontology_discoverable_workflow_count=0`

That means the main remaining VWL contract failures are no longer about runtime registration authority. They are concentrated in four places:

1. Python is still the authoritative authoring source for many canonical workflows and some prompts, even when the executable workflow already lives in Vontology.
2. Workflow creation and gap-recovery still contain hard-coded workflow templates and request-shape heuristics in Python.
3. File-copy upload routing still depends on Python route maps and hard-coded downstream workflow IDs.
4. Testing Workflows still contain meeting-specific fixture preparation and benchmark-tier branching that should become generic testing-workflow surfaces.

The largest current contract failure is therefore: "Vontology is authoritative at runtime, but several workflow families are still authored in Python first and only then published into Vontology." Current VWL already suffices for a large part of that residue. The main justified new extensions are:

- declarative workflow-template applicability and discovery exemplars
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

The workflow-purity counters are accurate for runtime purity, but they do not currently measure source-authoring drift. In particular, they do not flag the case where:

- a canonical workflow graph is built in Python via `_CanonicalWorkflowPublicationSpec`,
- then published into Vontology,
- and then executed authoritatively from Vontology.

So the zeroed purity counters should be read as "runtime authority is now Vontology-backed", not "workflow-first convergence is complete".

## Exhaustive Findings

| Family | Locations | Current behaviour | Example workflows / task classes | Current VWL sufficient? | Equivalent VWL representation or smallest coherent extension | Other examples covered | Delete / migrate / retain | Migration risk, dependencies, priority |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Python-authored canonical workflow publication specs | `src/backend/workflows/workflow_concept_authority_service.py:378`, `:2242`, `:3349`; `src/backend/services/paper_representation_workflow_vontology_service.py:125`, `:526`, `:1041`; `src/backend/services/talk_representation_workflow_vontology_service.py:171`, `:499`, `:522`, `:712`; `src/backend/services/testing_workflow_vontology_service.py:383`, `:456`, `:780`, `:932`, `:992`, `:1388` | Canonical workflow graphs are still assembled as Python dataclass specs and then published into Vontology. | Chat narration, buttonify, tool-calling, turn execution, file-copy typing/upload/interpretation, planning, rumination, workflow authoring, scholarly/arXiv paper workflows, talk workflows, testing workflows. | Yes. | Store the authoritative workflow definition directly as Vontology workflow graph data or as version-controlled workflow source assets imported through the generic publication/materialisation path. Keep only generic import, validation, and publication helpers in Python. | Any future canonical workflow family. | Migrate workflow-family-specific spec builders out of Python. Retain the generic publication/import/validation utilities. | Medium-high migration volume. Main dependency is choosing one durable source-of-truth format for authored workflow definitions. Priority `P1`. |
| Code-authored workflow prompts | `src/backend/services/testing_workflow_vontology_service.py:77`, `:102`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py:282`, `:482` | Prompt text is still created from Python constants/templates and then persisted into prompt concepts. | Meeting-invitation structure/evaluation prompts, gap-recovery candidate prompt. | Yes. | Store prompt bodies directly in Vontology prompt concepts and treat Python only as prompt resolution/rendering infrastructure. | Future workflow-repair prompts, authoring prompts, test-fixture prompts. | Migrate prompt text out of Python. Retain prompt rendering and prompt-resolution support. | Low migration risk. Depends mainly on adopting prompt-source discipline alongside workflow-source discipline. Priority `P2`. |
| Hard-coded workflow templates in workflow creation and gap recovery | `src/backend/workflows/durable/workflow_creation_workflow.py:531`, `:586`, `:704`, `:1113`; `src/backend/workflows/durable/workflow_gap_recovery_workflow.py:482`, `:806` | Python builds full workflow specs for default, scholarly-paper, PhD-student, and gap-recovery candidate workflows. | Text-driven workflow creation for scholarly-paper representation, PhD-student representation, single-step recovery candidate workflows. | Partly. Template bodies are already expressible; automatic template choice is not yet declarative. | Today: support explicit `workflow_template_id` or authored workflow-definition fragments stored in Vontology. Extension needed for automatic selection: a declarative `workflow_template_profile` with applicability metadata and examples. | User-onboarding workflows, new representation workflows, future recovery-generated candidate workflows. | Migrate template bodies out of Python now. Retain only generic template-loading and materialisation support after a template-profile extension exists. | Medium risk. Depends on a stable template asset format and template-resolution contract. Priority `P1`. |
| Workflow discovery seeds and override-role heuristics | `src/backend/services/workflow_discovery_service.py:914`, `:923`, `:1021`; `src/backend/services/workflow_override_policy_service.py:81`, `:98`, `:295`, `:422` | Workflow creation is boosted by a Python regex + intent seed. Override policy still infers `authoring` and `maintenance` roles from name/description/action hints when `routing_profile` metadata is absent. | Workflow creation requests, authoring/meta workflow promotion, maintenance workflow decline. | Partly. Routing profiles already cover role and authoring intent, but discovery exemplars are not yet declarative. | Backfill `workflow_routing_profile.v1` for all relevant workflows now. Add a declarative discovery-exemplar surface so regex-based workflow-specific seeding can be retired coherently. | Workflow repair, workflow introspection, future maintenance/meta workflows. | Migrate heuristics out of code as metadata coverage improves. Retain only minimal generic fallback while legacy workflows are being backfilled. | Low-medium risk. Depends on loader/index/search support for exemplar metadata. Priority `P2`. |
| Hard-coded typed-subworkflow route maps in file-copy upload classification | `src/backend/workflows/durable/file_copy_upload_classification_workflow.py:52`, `:185`, `:290`; `src/backend/workflows/workflow_concept_authority_service.py:860` | Python owns the route keys, default downstream workflow IDs, confidence thresholds, env overrides, and fallback policy for upload routing. | Upload -> scholarly paper workflow, upload -> CV workflow, upload -> business-card workflow, upload -> meeting workflow. | No, not coherently. | Add a declarative typed-subworkflow route-map surface: route key, candidate workflow list, threshold policy, fallback mode, and fail-closed/no-op semantics should all be representable in metadata. | URL-ingestion routing, document subtype dispatch, future structured artefact-routing pipelines. | Retain generic file typing and route-execution infrastructure, but migrate route-map policy out of Python once the extension exists. | Medium risk. Depends on a clear route-map schema and loader support. Priority `P1`. |
| Testing-workflow-specific fixture preparation and benchmark escalation | `src/backend/workflows/durable/testing_workflow_actions.py:464`, `:483`; `src/backend/services/experiment_run_service.py:1452`; `src/backend/services/testing_workflow_vontology_service.py:260` | Meeting-invitation fixture preparation is a dedicated Python action. Tier-1 vs Tier-2 regression-suite branching is also owned by Python, with a capability-gap note recorded in the workflow metadata. | `prepare_meeting_invitation_experiment_spec`, synthetic regression-suite workflow, future acceptance tests for arXiv/talk/file-copy flows. | No, not fully. | Add generic testing-workflow fixture-preparation semantics and declarative test-tier/escalation semantics. The current Python services are legitimate temporary runtime support until those surfaces exist. | arXiv paper-ingestion testing, talk-representation testing, workflow-gap candidate evaluation, future benchmark-backed acceptance workflows. | Retain the generic runtime support temporarily. Migrate the meeting-specific action and benchmark-branch logic after the testing-workflow extension lands. | Medium-high risk. Depends on generic fixture schema design and benchmark-binding contract. Priority `P1`. |

## What Should Remain as Reusable Runtime Support

These areas are not themselves contract failures and should not be deleted merely because they are written in Python:

- `publish_canonical_chat_workflow_graphs(...)` and related validation/import helpers in `workflow_concept_authority_service.py` should remain as generic publication infrastructure. The problem is that workflow-family specs still live beside them in Python.
- Generic testing-theory and experiment-run actions in `src/backend/workflows/durable/testing_workflow_actions.py` are reusable execution surfaces, not workflow-specific drift.
- Deterministic paper/talk materialisation actions remain legitimate runtime primitives until VWL gains a general, declarative concept-mutation-and-verification surface.
- File typing and route execution are reusable runtime support. The route-map policy is the part that should move into VWL.

## Prioritised Extension and Migration Plan

1. `P1`: Move remaining canonical workflow definitions out of Python spec builders and into direct VWL/Vontology-authored sources.
2. `P1`: Add declarative workflow-template profiles and migrate workflow creation / gap recovery off hard-coded template builders.
3. `P1`: Add declarative typed-subworkflow route maps and migrate file-copy upload classification policy out of Python.
4. `P1`: Generalise testing-workflow fixture preparation and test-tier escalation semantics so meeting-specific and benchmark-specific logic does not keep reappearing in Python.
5. `P2`: Backfill routing profiles everywhere and add declarative discovery exemplars so workflow-specific discovery/override heuristics can disappear.
6. `P2`: Move remaining workflow prompt text out of Python and into Vontology prompt concepts as first-class authored artefacts.

## Residual Uncertainty

This audit was exhaustive across the current workflow runtime, authoring, discovery, routing, and testing modules under `src/backend/workflows` and `src/backend/services/*workflow*`, plus adjacent services used directly by those modules.

Residual uncertainty remains in two narrow areas:

- Generic concept-mutation services were not audited line-by-line unless a workflow module depended on them directly. There may still be future opportunities to turn some domain-specific durable actions into more general VWL mutation primitives.
- The current workflow-purity telemetry does not yet surface source-authoring drift, so a future audit helper should probably add counters for Python-authored workflow specs and code-authored prompt bodies.

No additional workflow-family-specific authoring surfaces were found by broad searches over:

- `_CanonicalWorkflowPublicationSpec`
- `_build_*workflow_spec`
- `publish_canonical_chat_workflow_graphs`
- `routing_profile`
- `specialised_workflow_ids`
- workflow-gap candidate-spec builders

## Guidance Updates

The durable policy update required by this audit is already reflected in `AGENTS.md`: if a workflow can be implemented through a genuine VWL extension, that extension should be made first and the workflow should then be implemented fully in VWL.

No further `AGENTS.md` change is justified by this task alone. The next durable documentation change should happen when the new extension families above land, at which point `docs/engineering/von_workflow_language_manual.md` should be updated with:

- workflow-template profile semantics
- discovery-exemplar semantics
- typed-subworkflow route-map semantics
- testing-fixture preparation and tier-escalation semantics

The parent epic `JVNAUTOSCI-833` should receive a short summary comment pointing to this report and the follow-up migration/extension tasks created from it.
