# JVNAUTOSCI-1415 Paper and arXiv Workflow Design

Status: Planning baseline for implementation
Last updated: 2026-03-10 (Pacific/Auckland)
Task: `JVNAUTOSCI-1415`

## 1. Why this design exists

Representing scientific papers is a core lab activity, but the current workflow picture is split across:

- representation contracts and completion-gate logic,
- tool-handler-side materialisation in `download_paper`, `finalise_cached_paper`, and `interpret_file_copy`,
- an existing but incomplete `#V#scholarly_paper_representation_workflow`,
- and a code-only upload constant `#V#integration_scholarly_paper_representation_workflow`.

That is enough for some paths to work, but it is not a good knowledge-base representation of the behaviour. `JVNAUTOSCI-1415` therefore needs explicit VWL/Vontology workflow artefacts for:

- general scientific/scholarly paper representation, and
- arXiv-specific acquisition and normalisation as a wrapper around that general paper workflow.

## 2. Audited current state

### 2.1 Confirmed Vontology and code facts

- `#V#scholarly_paper_representation_workflow` exists in Vontology.
- `#V#integration_scholarly_paper_representation_workflow` does not currently exist in Vontology.
- no explicit arXiv wrapper workflow concept currently exists.
- the current `#V#scholarly_paper_representation_workflow` loads and validates, but its graph is legacy/incomplete:
  - 10 steps,
  - most steps invoke `workflow_creation.emit_marker`,
  - only `workflow_creation.resolve_scholarly_authors` is domain-specific,
  - no `invokesWorkflow` delegation,
  - no explicit arXiv acquisition branch,
  - no meaningful context-input or tool-output mapping contracts.
- file-upload classification still defaults the scholarly route to the non-existent `#V#integration_scholarly_paper_representation_workflow`.
- current real paper/arXiv materialisation mostly lives in reusable services and tool handlers:
  - `src/backend/services/arxiv_paper_link_service.py`
  - `src/backend/integrations/internal_mcp/catalogue.py`
- the current manual was broadly aligned on runtime semantics, but it did not yet document the scholarly-workflow incompleteness or the upload-workflow identity split.

### 2.2 Design implication

The current scholarly workflow should be treated as a legacy baseline to repair or replace while preserving compatibility, not as the finished canonical topology.

## 3. Design goals

- Keep workflow behaviour represented primarily in Vontology/VWL, not hidden in Python orchestration.
- Reuse the existing `#V#scholarly_paper_representation_workflow` identifier as the canonical general-paper workflow, so current references do not all need to be renamed.
- Introduce a new explicit arXiv wrapper workflow that delegates to the general paper workflow.
- Align upload routing with the canonical general-paper workflow instead of a code-only placeholder ID.
- Make the workflow family inspectable:
  - explicit workflow concepts,
  - explicit step graphs,
  - explicit workflow-to-workflow delegation,
  - explicit context keys and output mappings,
  - explicit verification outputs.
- Keep Python changes limited to reusable workflow actions/validators/control surfaces that VWL needs in order to express the intended graph.

## 4. Non-goals

- Do not treat `#V#tool_calling_workflow` as the long-term canonical representation of paper/arXiv behaviour.
- Do not encode the paper/arXiv sequence as bespoke route-specific Python logic if VWL can represent it.
- Do not make arXiv a separate domain that duplicates the whole general paper workflow; it should wrap and specialise the general workflow.

## 5. Target workflow family

### 5.1 Canonical general workflow

Workflow ID:

- `#V#scholarly_paper_representation_workflow`

Role:

- canonical workflow for representing scientific/scholarly papers in general,
- reusable from upload-driven, URL-driven, and already-registered file-copy flows,
- domain workflow that owns paper-specific representation semantics rather than conversation-turn wrapper semantics.

Expected inputs:

- `file_copy_concept_id` (required for current implementation phase)
- `paper_concept_id` (optional pre-existing target)
- `paper_metadata` (optional structured metadata)
- `source_uri` (optional)
- `source_label` (optional)
- `arxiv_id` (optional)
- `verification_profile` (`generic` or `arxiv`)

Expected outputs:

- `paper_concept_id`
- `scholarly_representation`
- `scholarly_representation_verified`
- `verification_failures`
- `author_concept_ids`
- `topic_concept_ids`
- `representation_mode`

Proposed step graph:

1. `normalise_inputs`
   - new reusable action: `scholarly_paper.normalise_inputs`
   - purpose: validate required inputs, normalise metadata payload shape, set verification defaults, expose deterministic context keys.
2. `materialise_base_representation`
   - new reusable action: `scholarly_paper.materialise_from_file_copy`
   - purpose: use the canonical materialisation helpers to ensure a paper concept exists, has the correct type, and is linked to the file copy.
3. `enrich_from_metadata`
   - new reusable action: `scholarly_paper.enrich_from_metadata`
   - purpose: assert/update title, description, topic labels, and author-name candidates from supplied metadata.
4. `resolve_authors`
   - new reusable action: `scholarly_paper.resolve_authors`
   - likely implemented by extracting/generalising current `workflow_creation.resolve_scholarly_authors` behaviour into a domain action with a non-workflow-creation name.
5. `verify_representation`
   - new reusable action: `scholarly_paper.verify_representation`
   - purpose: check the representation against the requested verification profile and emit explicit failure codes.
6. `completed`
7. `failed`

Branching expectations:

- any failed domain action goes to `failed`;
- `verify_representation` succeeds only when the requested profile is satisfied;
- verification profile `generic` should require:
  - paper concept exists,
  - paper typed as `#V#scholarly_article`,
  - file-copy link exists,
  - at least one stable name/title signal exists;
- verification profile `arxiv` should require the generic checks plus:
  - arXiv identifier asserted,
  - title present,
  - summary present,
  - authors linked,
  - topic/category evidence present when available from metadata.

### 5.2 Explicit arXiv wrapper workflow

Workflow ID:

- `#V#arxiv_paper_representation_workflow`

Role:

- recognise and normalise canonical arXiv inputs,
- acquire or finalise the paper artefact,
- delegate to `#V#scholarly_paper_representation_workflow`,
- apply stricter arXiv-specific verification.

Expected inputs:

- `prompt` or `source_uri` or `arxiv_id`
- optional `file_copy_concept_id`
- optional `paper_metadata`

Expected outputs:

- `arxiv_id`
- `file_copy_concept_id`
- `paper_concept_id`
- `scholarly_representation`
- `scholarly_representation_verified`
- `verification_failures`

Proposed step graph:

1. `normalise_arxiv_source`
   - new reusable action: `arxiv.normalise_source`
   - purpose: extract a canonical arXiv ID from URL/ID/PDF filename/current context.
2. `decide_acquisition_mode`
   - new reusable action: `arxiv.decide_acquisition_mode`
   - purpose: decide whether the workflow already has a usable file copy or must call `download_paper`.
3. `download_or_finalise`
   - action/tool: `download_paper`
   - only on the branch where acquisition is required.
   - rely on current tool semantics that prefer `finalise_cached_paper` when appropriate.
4. `delegate_to_general_paper_workflow`
   - action: `workflow_invoke_subworkflow`
   - invoked workflow: `#V#scholarly_paper_representation_workflow`
   - pass `file_copy_concept_id`, `paper_metadata`, `arxiv_id`, and `verification_profile=arxiv`.
5. `verify_arxiv_path`
   - action: `scholarly_paper.verify_representation`
   - explicit second verification step so the wrapper itself can fail closed with arXiv-specific failure codes.
6. `completed`
7. `failed`

Branching expectations:

- if `decide_acquisition_mode` says an existing file copy is already available, skip `download_or_finalise` and go straight to delegation;
- if `download_paper` returns failure or no usable `computer_file_copy_concept_id`, fail closed;
- if the delegated general workflow fails or produces incomplete verification, the wrapper fails.

### 5.3 Upload alignment

The upload-classification scholarly default should be changed from:

- `#V#integration_scholarly_paper_representation_workflow`

to:

- `#V#scholarly_paper_representation_workflow`

This keeps upload-event-driven scholarly handling aligned with the same canonical general paper workflow that the arXiv wrapper delegates to.

## 6. Required reusable action/control-surface work

This task should add reusable workflow actions where the workflow graph currently has no good first-class primitive.

Actions to add or refactor:

- `scholarly_paper.normalise_inputs`
- `scholarly_paper.materialise_from_file_copy`
- `scholarly_paper.enrich_from_metadata`
- `scholarly_paper.resolve_authors`
- `scholarly_paper.verify_representation`
- `arxiv.normalise_source`
- `arxiv.decide_acquisition_mode`

Migration note:

- `workflow_creation.resolve_scholarly_authors` should not remain the canonical domain action name inside the repaired paper workflow.
- It can be retained temporarily as a compatibility alias if needed, but the canonical workflow should use a domain-specific action namespace.

## 7. Vontology artefacts to create or update

### 7.1 Update existing concept

- repair/rewrite `#V#scholarly_paper_representation_workflow`
- replace the current marker-heavy step graph with the designed general-paper graph
- update its `hasDescription` and `hasContent` text so they describe the actual workflow, not the legacy generated request

### 7.2 Create new workflow concept

- create `#V#arxiv_paper_representation_workflow`
- type it as `#V#ai_workflow` and `#V#durable_workflow`
- attach explicit step concepts and `invokesWorkflow` edge to `#V#scholarly_paper_representation_workflow`

### 7.3 Create or update supporting artefacts

- step concepts for both workflows
- context-key concepts as needed for new mappings
- input-mapping concepts
- tool-output-to-context mapping concepts
- prompt/content text relations when a step truly needs prompt-governed behaviour

## 8. Routing and execution implications

### 8.1 Conversation turns

Target direction:

- bare arXiv URL turns should become discoverable as an explicit arXiv workflow case rather than relying only on the generic tool-calling wrapper plus tool-requirement overrides.

Pragmatic transition rule:

- if full direct workflow selection is not completed in the same change, the arXiv wrapper must at minimum exist and be invocable as a first-class workflow, with a follow-on routing change making workflow discovery select it directly.

### 8.2 Upload events

- file-copy upload handling should invoke the canonical general paper workflow for scholarly uploads.
- no upload pathway should depend on the non-existent integration-only workflow ID after this task.

## 9. Validation plan

Required validation after materialisation:

- concept existence checks:
  - `#V#scholarly_paper_representation_workflow`
  - `#V#arxiv_paper_representation_workflow`
- process-graph/load validation:
  - `build_workflow_process_graph(...)`
  - `load_workflow_definition_from_vontology(...)`
  - `validate_workflow_definition_contract(...)`
- targeted behavioural tests:
  - `tests/backend/test_von_generate_bare_arxiv_url_integration.py`
  - `tests/backend/test_von_file_upload_scholarly_integration.py`
  - `tests/backend/test_turn_execution_required_effects_contract.py`
  - relevant workflow authority/publication tests if publication helpers are touched

## 10. Jira step plan

Step 1. Audit current implementation and manual drift.
Step 2. Write and attach/update this design baseline.
Step 3. Repair the general scholarly paper workflow in Vontology.
Step 4. Create the explicit arXiv wrapper workflow in Vontology.
Step 5. Align upload routing and any workflow-discovery/routing hooks.
Step 6. Validate with targeted tests and Vontology inspection.
Step 7. Update Jira with per-step notes and close out only after workflow materialisation is real.

## 11. Immediate implementation decisions from this plan

- Preserve `#V#scholarly_paper_representation_workflow` as the canonical general-paper ID, but replace its graph.
- Create `#V#arxiv_paper_representation_workflow` as a new explicit wrapper.
- Treat the current scholarly workflow as legacy/incomplete, not authoritative.
- Treat the upload default `#V#integration_scholarly_paper_representation_workflow` as technical debt to eliminate in this task.
