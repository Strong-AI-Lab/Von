# Von Workflow Language (VWL) Manual

Status: Draft (current implementation-aligned)
Last updated: 2026-05-03 (Pacific/Auckland)
Audience: Human engineers and AI agents

## 1. Purpose and Scope

Von Workflow Language (VWL) is the executable workflow language used by Von for LLM-era agent behaviour orchestration. VWL is not a standalone parser language. It is a graph-native language represented in Vontology concepts and predicates, then compiled into runtime workflow definitions.

VWL workflow creation and modification are implementation activities, not documentation-only activities. When a behaviour can be expressed in VWL, engineers and AI agents SHOULD create or update the workflow directly in Vontology as part of the task, rather than deferring it to later bespoke code.

This manual documents:

- the authoritative VWL vocabulary in Vontology,
- compilation and runtime semantics in code,
- durable execution semantics (instances, schedules, event bindings),
- the MCP workflow control surface,
- and a diagram appendix with tool-ready generation specifications.

Normative keywords in this manual:

- MUST: required for conformance with current VWL runtime.
- SHOULD: strong recommendation.
- MAY: optional.

## 2. Authoritative Sources

VWL is defined by the combination of:

- Vontology concepts/predicates (source of meaning and graph data),
- workflow loader/compiler/runtime in backend code,
- durable runtime subsystem,
- internal MCP workflow tools.

Primary implementation anchors:

- `src/backend/workflows/vontology_loader.py`
- `src/backend/workflows/engine.py`
- `src/backend/workflows/execution_contracts.py`
- `src/backend/workflows/metadata_validation.py`
- `src/backend/workflows/subworkflow_contracts.py`
- `src/backend/workflows/workflow_definition_identity_service.py`
- `src/backend/workflows/durable/models.py`
- `src/backend/workflows/durable/control_flow_actions.py`
- `src/backend/workflows/durable/testing_workflow_actions.py`
- `src/backend/workflows/durable/worker.py`
- `src/backend/workflows/durable/scheduler.py`
- `src/backend/workflows/durable/workflow_instance_submission_service.py`
- `src/backend/services/testing_theory_service.py`
- `src/backend/services/experiment_run_service.py`
- `src/backend/server/utils_flask.py`
- `src/backend/integrations/internal_mcp/catalogue.py`

Authoring policy:

- Vontology workflow graphs, mappings, schedules, and event bindings are first-class implementation artefacts.
- When behaviour can be expressed in VWL, preferred implementation is to materialise it in Vontology and then add only the supporting code/tooling needed for execution, validation, and telemetry.
- Backend code SHOULD primarily supply reusable actions, validators, loaders, and telemetry for workflows, rather than embedding task-specific orchestration that VWL could already express.
- Repo-side workflow/template/prompt files are non-authoritative by default. They MAY exist only as migration seeds, generated snapshots, test fixtures, or exports unless an explicitly approved exception says otherwise.
- Replacing bespoke Python workflow builders with repo-side declarative files is therefore not, by itself, workflow-first convergence. The authoritative authored logic still belongs in Vontology-native artefacts.
- Workflow-related Jira work SHOULD follow the workflow-authority readiness/closure checklist in `AGENTS.md` before implementation and before `Done`.

### 2.1 Workflow Authority Model

Canonical workflow definitions, reusable workflow-authoring metadata, routing metadata, and prompt metadata SHOULD be authored directly in Vontology-native graph/text/policy structures whenever the runtime can represent them there.

Normative authority rule:

- supporting code MAY compile, validate, publish, or export these artefacts;
- repo-side files MUST NOT be treated as the canonical authored source when the same logic can be held in Vontology;
- if a production path depends on repo-side files for workflow authority, that path is source-authority drift and should be treated as contract debt, not normal conformance.

### 2.2 Transitional Repo-Side Workflow Artefacts

Some repo-side workflow artefacts currently exist while the stronger authority model is being restored. They do not satisfy the workflow-authority contract on their own.

Current transitional examples include:

- `src/backend/workflows/repo_seed_bundles/*.json`
- `src/backend/workflows/repo_seed_bundles/workflow_template_seed_bundle.json`
- `src/backend/workflows/repo_seed_bundles/README.md`

Permitted roles for such artefacts:

- migration seed data used to move existing file-authored logic into Vontology-native authority;
- generated snapshots exported from Vontology for diff/review;
- test fixtures or deterministic export examples.

Non-permitted role:

- acting as the authoritative source of workflow logic, template logic, routing metadata, or prompt metadata for production behaviour.

Supporting code notes:

- generic publication/materialisation helpers, validators, and export tooling remain valid runtime support;
- `workflow_concept_authority_service.py` now derives canonical publication specs/text from authoritative Vontology workflow state where present, and only falls back to repo-side seed bundles when a canonical workflow is missing or clearly incomplete;
- `workflow_template_profile_service.py` now resolves workflow-template concepts and template/profile text relations from Vontology, and only hydrates them from the seed bundle when the authoritative template concepts are absent;
- `workflow_prompt_authority_service.py` now provides only generic prompt-concept creation, validation, linking, and rendering support; workflow-governed prompt bodies themselves remain authoritative Vontology text relations rather than Python-authored defaults;
- `workflow_repo_seed_bootstrap.py` now treats repo-side workflow bundles as seed fixtures only: a valid current Vontology workflow family is preserved rather than being overwritten back to seed parity, and bundle/publication-spec mismatches are drift diagnostics rather than an excuse to restore file authority at startup;
- repo seed bundles MAY declare top-level `support_concepts` for prerequisite non-workflow concepts that the workflow graph needs to execute, such as abstract parent/type concepts. The bootstrapper materialises these as seed support data before publishing or validating the workflow family. This is generic support for Vontology materialisation, not permission to move workflow policy into Python;
- `paper_representation_workflow_vontology_service.py` is therefore a startup-only seed publication/repair entry point for the current paper workflow family; live routing/execution must rely on the Vontology materialisation, and any repo-seed repair should be surfaced as drift diagnostics rather than treated as normal request-path authority;
- refresh repo-side workflow bundle snapshots from authoritative Vontology state with `pdm run python scripts/workflow_repo_seed_bundle_export.py export --asset-path <bundle-path>` and inspect drift with the corresponding `diff` command instead of hand-editing bundle JSON;
- generated review snapshots under `docs/generated/workflow_authority_review_snapshots/` are acceptable because they are explicitly non-authoritative derived artefacts;
- use `pdm run python scripts/workflow_authority_review_snapshot.py export` to refresh local review snapshots and `pdm run python scripts/workflow_authority_review_snapshot.py diff` to compare the current authoritative KB state against the last generated local snapshot without restoring file authority.

## 3. VWL Ontology Vocabulary

### 3.1 Core Type Concepts

Canonical concepts (confirmed via Vontology MCP):

- `#V#ai_workflow`: structured machine-interpretable workflow plan type.
- `#V#durable_workflow`: workflow subtype with persistent execution/checkpointing semantics.
- `#V#workflow_step`: type for workflow states/steps.
- `#V#workflow_context_key`: type for deterministic context key symbols.
- `#V#workflow_stage`: type for model-policy stage scoping.
- `#V#workflow_model_policy`: type for stage-aware model selection policy.

Typing rule:

- A workflow concept typed as a subtype of `#V#ai_workflow` (for example `#V#durable_workflow`) MUST satisfy workflow-authority checks that require `#V#ai_workflow`.

### 3.2 Core Predicate Concepts

- `#V#workflow_step_invokes_tool`
- `#V#workflow_step_uses_llm_prompt`
- `#V#workflow_step_writes_context_key`
- `#V#workflow_step_maps_context_key_to_tool_param`
- `#V#workflow_step_maps_tool_output_field_to_context_key`
- `#V#applies_to_workflow_stage`

### 3.3 Graph Predicate Aliasing

VWL accepts canonical and legacy aliases for graph links. Canonical names are preferred; aliases remain accepted for backward compatibility.

Canonical graph families include:

- `hasInitialStep`, `hasStep`
- `invokesAction`, `invokesWorkflow`
- `workflowStepInvokesTool`
- `workflowStepUsesLlmPrompt`
- `nextStep`
- `onTrueNextStep`, `onFalseNextStep`, `onFailureNextStep`, `onUnknownNextStep`
- `onApprovalRequiredNextStep`
- `onBreakNextStep`, `onContinueNextStep`
- `hasPrecondition`, `hasEffect`
- `readsVariable`, `writesVariable`
- `hasInputMap`
- `workflowStepMapsContextKeyToToolParam`
- `workflowStepWritesContextKey`
- `workflowStepMapsToolOutputFieldToContextKey`
- `hasWorkflowVariable`

### 3.4 Action-Contract Concepts

`invokesAction` MAY point to either:

- a raw executable action ID such as `workflow_control.context_set`, or
- a concept-backed action contract, typically an instance of `#V#workflow_action_contract`.

Semantic rule:

- `invokesAction` is the deterministic execution target for a step; it is not, by itself, evidence that the step is an ordinary reasoning step.
- Prompt-bearing reasoning steps MUST compile as `llm` execution-mode steps, even when they also expose an underlying `action_id` for traceability or publication.
- Genuinely deterministic and reliable steps SHOULD remain explicit deterministic or control executions rather than being wrapped in LLM reasoning.

When a workflow step points at an action-contract concept, the concept SHOULD carry the machine-readable contract in both:

- `concept_data.workflow_action_contract`
- `#V#hasWorkflowActionContractJson` text (primary `en-NZ`)

The canonical payload schema is `workflow_action_contract.v1` and includes:

- `concept_id`
- `action_id`
- `description`
- optional `input_schema`
- optional `output_schema`
- optional `side_effects`
- optional `postconditions`

Runtime rule:

- the loader MUST resolve the concept-backed target to its underlying executable `action_id`;
- the original concept target MUST remain available as the step's contract concept for introspection, validation, and publication repair.

### 3.4a Bounded Internal MCP Tool Invocation

VWL workflows may invoke internal MCP tools through the generic durable action:

- `workflow_mcp.invoke_tool`

Required input:

- `tool_name`: the static internal MCP method name to invoke.

Optional input:

- `tool_arguments`: object payload passed to the internal MCP gateway.

Runtime semantics:

- workflow context bindings inside `tool_arguments` are resolved by the normal action-input resolver before invocation;
- authenticated namespace context is propagated into the MCP payload as `namespace` when available;
- the action returns the workflow-visible MCP payload under `result` and `mcp_result`, with `mcp_tool`, `mcp_requested_tool`, `mcp_resolved_tool`, and `mcp_duration_ms` diagnostics;
- when the invoked tool has a represented tool-evidence projection, `result` and `mcp_result` carry that compact projected payload and projection telemetry instead of raw source-specific bulk data;
- `tool_output_context_mappings` should map fields from `result.<field>` or `mcp_result.<field>` into workflow context for downstream steps and subworkflows.

Validation semantics:

- `tool_name` MUST be statically declared, or the step MUST fail Workflow Studio contract validation;
- the named tool MUST resolve to a registered internal MCP method;
- read-only tools may be published without write metadata;
- write or destructive tools MUST declare an explicit represented write policy such as step `mutation_authority` or `workflow_execution_side_effect_policy`;
- domain sequencing, extraction, filtering, and user-facing policy MUST remain in VWL, prompt, KB, or Vontology artefacts rather than in the generic action implementation.

### 3.4b Wrapper Workflow Boundaries for External and Low-Level Tools

When a low-level tool is brittle, source-specific, external, or only partially
aligned with Von's durable artefact model, create or repair a wrapper workflow
that owns the domain-specific acquisition, fallback, verification, and read-back
policy. Downstream workflows SHOULD consume the wrapper's represented outputs
rather than calling the low-level tool directly.

Normative rules:

- a wrapper workflow is the authority boundary around source-specific or
  external-tool behaviour;
- external MCP/tool failures SHOULD be normalised into structured telemetry and
  typed context fields by support code, but fallback selection and recovery
  sequencing SHOULD remain in the wrapper workflow;
- do not hide fallback policy inside the low-level MCP tool merely because that
  tool is where the failure was observed;
- list-processing, email-ingestion, and discovery workflows that encounter many
  source references SHOULD fan out to the relevant wrapper workflow for each
  reference, then aggregate per-item results;
- downstream workflows SHOULD use durable artefact identifiers produced by the
  wrapper, such as a file-copy or represented-concept ID, for later reading,
  extraction, linking, and verification;
- downstream workflows MUST NOT bypass a wrapper by directly invoking the
  source-specific low-level MCP tool unless the wrapper workflow is itself
  unavailable and the fallback path is explicitly represented, audited, and
  fail-closed;
- step output contracts such as `writes_context_keys` MUST be declared only on
  steps that actually produce the context key on that execution path. Decision,
  pass-through, or routing steps MAY map optional pre-existing values forward,
  but MUST NOT advertise those values as newly produced artefacts.

This keeps transient tool defects, third-party server quirks, retry/wait
interpretation, and direct HTTP/source recovery inside an inspectable VWL
composition while preserving Python as support infrastructure for execution,
telemetry, validation, and generic tool bridging.

### 3.4c Tool Follow-Up Hint Resolution

VWL workflows may resolve Vontology-authored tool follow-up hints through the generic durable action:

- `resolve_tool_output_followup_hint`

Required input:

- `source_tool_concept`: the tool concept whose `#V#output_followup_hint` text relation should be resolved.

Optional inputs:

- `hint_predicate_id`: override predicate; defaults to `#V#output_followup_hint`.
- `lang`: text-relation language; defaults to `en-NZ`.
- `required_action_kind`: when supplied, selects the first hint entry with the matching `action_kind` and fails closed if absent.

Runtime semantics:

- the action reads and parses the JSON hint body from Vontology;
- it returns the complete parsed hint under `hint`;
- it exposes the selected entry under `selected_entry`, with convenience mappings `selected_action`, `selected_tool_arguments`, and `selected_upstream_filter`;
- `selected_tool_arguments` may be authored either as entry-level `tool_arguments` or, for compatibility with older hints, under `action.tool_arguments`;
- workflows can then map those fields into context and pass them into later declarative steps such as `workflow_control.context_template` or `workflow_mcp.invoke_tool`.

Authority rule:

- the follow-up decision policy and user-facing action semantics belong in the Vontology hint body;
- the action is only a resolver/parser and MUST NOT contain integration-specific policy such as Gmail labels, arXiv IDs, or paper-ingestion behaviour.

## 4. VWL Program Model

A VWL program is a workflow concept graph:

- Workflow node (concept ID, usually an instance of `#V#ai_workflow` or `#V#durable_workflow`).
- Step nodes linked via `hasStep`.
- One initial step via `hasInitialStep` (or inferred first step if absent).
- Per-step execution contract:
  - `llm` for ordinary prompt-driven reasoning,
  - `deterministic` for explicit reliable tool/action execution,
  - `control` for workflow control primitives,
  - `subworkflow` for child workflow invocation.
- Per-step deterministic/subworkflow target:
  - tool/action (`invokesAction` or `workflow_step_invokes_tool`), or
  - subworkflow (`invokesWorkflow`).
- Optional per-step prompt contract link, required for KB-authored prompt-bearing LLM steps:
  - `workflow_step_uses_llm_prompt` (legacy aliases accepted for compatibility).
- Per-step control flow links:
  - `next`
  - `on_true`, `on_false`
  - `on_failure`, `on_unknown`.
  - `on_approval_required`.
  - `on_break`, `on_continue`.
- Optional per-step declarative branch payload in
  `concept_data.workflow_step_control_flow.conditions`, where each item is an
  object with:
  - `to` (or `to_state`/`next`) target step concept ID
  - `reason` stable branch label
  - `condition` transition-condition spec
- Optional metadata and mapping contracts.

Compilation rule:

- If a step carries a prompt contract and is intended to perform ordinary reasoning, the loader MUST compile it as execution mode `llm`.
- A prompt-bearing executable step compiled as `deterministic` is semantically contradictory and MUST fail workflow validation unless it is an explicitly documented deterministic exception.
- `WorkflowActionInvocation` is now the compiled step execution contract surface. In addition to `action_id`/`inputs`, it carries first-class `execution_mode`, `prompt_contract`, `llm_policy`, `validation_policy`, and `subworkflow_id`.

### 4.1 Workflow Publication Lifecycle

Workflow authoring MAY attach explicit publication lifecycle metadata to a workflow concept. This is the current mechanism used by the workflow-creation workflow to keep drafts loadable but non-routable until validation and final publish complete.

Canonical storage:

- `concept_data.workflow_publication_lifecycle`
- `#V#hasWorkflowLifecycleJson` text (primary `en-NZ`)

Canonical schema: `workflow_publication_lifecycle.v1`

Current phases:

- `draft`
- `validated`
- `validation_failed`
- `draft_failed_completion_gate`
- `published`

Current routing/discovery rule:

- workflows with explicit lifecycle metadata where `published=false` MUST remain loadable for validation and repair;
- those workflows MUST NOT be returned by workflow discovery or treated as executable for routing;
- only workflows with `published=true`, or workflows with no explicit lifecycle metadata, are discoverable.

### 4.1a Generic Workflow Authoring Primitives

When workflow authoring can be expressed as a reusable VWL/runtime capability, that capability MUST be added first and the workflow MUST then use the generic surface rather than leaving the logic hidden in bespoke Python handlers.

Current generic authoring action IDs:

- `workflow_authoring.ensure_workflow_identity`
- `workflow_authoring.discover_existing_workflows`
- `workflow_authoring.extract_existing_workflow_spec`
- `workflow_authoring.decide_repair_or_create`
- `workflow_authoring.design_repair_spec`
- `workflow_authoring.materialise_workflow_definition`
- `workflow_authoring.validate_workflow_definition`
- `workflow_authoring.publish_workflow_definition`

Current intent:

- `ensure_workflow_identity` creates or updates the target workflow concept and draft lifecycle metadata;
- `discover_existing_workflows` gathers existing workflow candidates so authoring can make explicit reuse-versus-repair-versus-create decisions in VWL;
- `extract_existing_workflow_spec` renders an authoritative existing workflow into declarative authoring-spec form so repair workflows can remain generic rather than editing graph links imperatively;
- `decide_repair_or_create` is the prompt-driven preflight decision step that MUST emit explicit structured decision evidence rather than hiding duplicate/repair logic in handler code;
- `design_repair_spec` is the prompt-driven repair-design step that produces an updated declarative workflow spec for the shared materialisation/validation/publication pathway;
- `materialise_workflow_definition` turns a declarative authoring spec into authoritative workflow graph concepts, step bindings, and transitions through the canonical publication pathway;
- `validate_workflow_definition` performs structural validation plus bounded execution against declared postconditions while the workflow is still unpublished;
- `publish_workflow_definition` promotes the draft only when the validation/completion gate passes.

Current canonical workflow-authoring composition:

- `#V#workflow_repair_or_create_workflow` is the discover-and-decide wrapper that routes to `reuse`, `repair`, or `create`;
- `#V#workflow_authoring_repair_workflow` loads the existing workflow, designs a repaired declarative spec, and then reuses the shared creation/materialisation workflow;
- `#V#von_workflow_creation_workflow` remains the shared identity/materialise/validate/publish pipeline rather than owning duplicate-versus-repair policy internally.

Authoring-policy rule:

- if a new behaviour would require a context-specific Python patch but can instead be represented as a genuine reusable VWL/runtime extension, implement the extension first and then express the workflow through these generic authoring surfaces.

### 4.2 Workflow Routing Profile

Workflow concepts MAY attach explicit routing-role metadata that helps
selector-override policy distinguish execution workflows from authoring or
maintenance/meta workflows when several discovered candidates are launchable.

Canonical storage:

- `#V#hasWorkflowRoutingProfileJson` text (primary `en-NZ`)

Canonical schema: `workflow_routing_profile.v1`

Current fields:

- `role`: `execution` | `authoring` | `maintenance`
- `authoring_intent_required`: bool
- `prefer_existing_capability`: bool

Current override-policy rule:

- launchability remains necessary but is not sufficient for promotion;
- routing-time promotion MUST prefer semantically closer launchable workflows
  over authoring/meta workflows that are only generically related;
- authoring workflows SHOULD set `authoring_intent_required=true` so exploratory
  workflow questions do not get reinterpreted as permission to author new
  workflows;
- when no discovered launchable custom workflow remains suitable after applying
  semantic-fit and role checks, routing MUST preserve the non-custom fallback
  path and record an explicit decline reason in routing diagnostics.

Current fallback discipline:

- generic fallback role inference MAY remain only for legacy workflows whose routing metadata has not yet been backfilled;
- workflow-family-specific authoring/maintenance hint lists or request-shape regexes MUST NOT be reintroduced for migrated workflows once explicit routing metadata exists.

### 4.3 Workflow Discovery Exemplars

Workflow concepts MAY attach explicit discovery metadata so capability-index and selector retrieval can match realistic requests without relying on workflow-family-specific discovery seeds.

Canonical storage:

- `#V#hasWorkflowDiscoveryExemplarsJson` text (primary `en-NZ`)

Canonical schema: `workflow_discovery_exemplars.v1`

Current fields:

- `keywords`: ordered list of compact lexical anchors
- `examples`: ordered list of representative request texts

Current discovery rule:

- authoritative workflow-capability text MAY be augmented with discovery keywords and example requests when this metadata is present;
- discovery exemplars are a retrieval aid, not an execution permission grant;
- discovery SHOULD prefer explicit exemplar metadata over workflow-family-specific capability-seeding code.
- discovery metadata SHOULD represent general workflow applicability rather than overfitting the current replay bank or a small set of English phrasings;
- English lexical anchors MUST NOT become the effective semantic authority for workflow discovery. If applicability collapses outside English or depends on English stopwords, regexes, or token overlap in support code, the design is wrong;
- low-complexity replay prompts are useful architectural sentinels only when they succeed for the same multilingual applicability surface that would also support richer compositional turns combining workflows, tools, KB retrieval, and background knowledge.

### 4.4 Workflow Template Profiles

When a workflow authoring path needs to synthesise a new workflow definition from declarative authored assets, the template-selection policy MAY be represented as a workflow template profile.

Current repo-seed surface:

- bundle schema: `repo_seed_workflow_template_bundle.v1`
- profile schema: `workflow_template_profile.v1`

Current fields:

- `selection_mode`: `automatic` | `explicit_only` | `fallback`
- `priority`: integer preference weight within a selection pool
- `keywords`: lexical anchors for retrieval-time scoring
- `exemplars`: representative request texts for applicability scoring
- `required_terms_all`
- `required_terms_any`
- `forbidden_terms_any`
- `requires_synthesis_policy`

Current selection rule:

- explicit template IDs MUST win over automatic selection;
- automatic selection MUST be driven by authored template-profile metadata and generic scoring logic;
- fallback templates MAY exist for broad request classes, but only as declarative authored assets;
- if a selected template requires synthesis-policy text, the runtime MUST fail closed when that policy cannot be resolved.

## 5. Condition Language (Transition Expressions)

Transition condition specs are JSON-like objects normalised by runtime code.

Supported kinds:

- `always`
- `context_flag`
- `context_value_equals`
- `context_value_in`
- `context_exists`
- `context_is_null`
- `context_compare`
- `context_cardinality`
- `transition_result_truth`
- `control_signal`
- `all`
- `any`
- `not`

Canonical forms:

```json
{"kind":"always"}
{"kind":"context_flag","key":"last_action_failed","expected":true}
{"kind":"context_value_equals","key":"mode","value":"strict"}
{"kind":"context_value_in","path":"event.predicate","values":["#V#has_research_interest","#V#working_on_project"]}
{"kind":"context_exists","path":"current_item.pull_request_url","expected":false}
{"kind":"context_is_null","path":"current_item.pull_request_url","expected":true}
{"kind":"context_compare","path":"candidate_issue_count","operator":"gt","value":0}
{"kind":"context_cardinality","path":"candidate_issues","operator":"gte","value":1}
{"kind":"transition_result_truth","expected":false}
{"kind":"control_signal","signal":"break"}
{"kind":"control_signal","signal":"continue","scope":"main_loop"}
{"kind":"all","conditions":[{"kind":"context_flag","key":"a"},{"kind":"context_flag","key":"b"}]}
{"kind":"any","conditions":[...]}
{"kind":"not","condition":{"kind":"context_flag","key":"disabled"}}
```

Invalid or malformed condition specs MUST fail workflow loading with deterministic error codes.

## 6. Compilation Semantics (Ontology -> Executable Definition)

Compilation flow:

1. Resolve workflow concept and relationships.
2. Build process graph (`build_workflow_process_graph`):
   - collect steps,
   - resolve invocation target,
   - resolve optional action-contract metadata,
   - resolve optional publication lifecycle metadata,
   - resolve control-flow edges,
   - capture metadata and mapping references.
3. Compile into runtime state machine (`load_workflow_definition_from_vontology`):
   - create `WorkflowStateSpec` objects,
   - create `WorkflowActionInvocation` records,
   - compile transition condition specs,
   - attach metadata contracts.

Important deterministic ordering:

- Generated branch precedence for implicit branch links is:
- Explicit declarative `conditions` listed on the step are evaluated first, in
  stored order.
- `on_failure`
- `on_unknown`
- `on_approval_required`
- `on_break`
- `on_continue`
- `on_true`/`on_false`
- `next_step`

`on_failure` and `on_unknown` compile to context-flag conditions (`last_action_failed`, `last_action_unknown`).
`on_approval_required` compiles to a context-flag condition (`approval_required == true`).
`on_break`/`on_continue` compile to control-signal conditions (`last_control_signal == break|continue`).

### 6.1 Runtime Registry Authority

Production runtime registry construction is now read-only with respect to
workflow authority:

- the runtime registry MUST discover already-materialised workflow concepts from
  Vontology and load definitions from Vontology on demand;
- the runtime registry MUST NOT publish or materialise authoritative workflow
  graphs from Python workflow-definition helpers during normal startup or route
  selection;
- Python workflow-definition modules MAY remain for test support, publication
  repair, or action registration, but they are not production registration
  authority;
- workflow parity/purity checks are expected to fail closed on drift by default,
  and authoritative routing text is expected to come from `source=vontology`
  workflows with non-empty narrative text.
- workflow purity checks MUST treat repo-seed workflow/template usage outside
  the designated seed/bootstrap support paths as source-authority drift, and
  the guarded seed-fallback counters
  `repo_seed_authority_drift_path_count=0` and
  `vontology_first_seed_fallback_violation_count=0` are expected in steady
  state;
- authoritative selector prompts for workflow routing MUST also come from
  Vontology prompt concepts; missing or malformed selector prompts MUST fail
  closed with explicit diagnostics instead of regenerating a code-authored
  ranker prompt.

## 7. Execution Semantics

Runtime execution model (`WorkflowExecutor`):

0. Initialise declared workflow variables from `definition.metadata["variable_declarations"]` — only for keys not already present in the caller-supplied context.
1. Enter current state.
2. Run pre-action metadata validation (unless validation mode disables enforcement).
3. Execute actions in state order according to their compiled execution mode.
4. Merge action outputs into context for non-failure outcomes.
5. Apply tool-output-to-context mappings.
6. Emit canonical step-result envelope for each executed action.
7. Evaluate transitions in stored order and take first satisfied transition.
8. If no valid transition and state terminal, complete.
9. Emit canonical workflow-result envelope at completion/failure.
10. Abort on max transition count or unrecoverable errors.

Action outcomes are normalised to:

- success,
- failure,
- unknown.

`last_action_failed` and `last_action_unknown` flags drive explicit failure/unknown routes.

Canonical envelope keys:

- step envelope list: `workflow_step_result_envelopes`
- latest step envelope: `last_workflow_step_result_envelope`
- workflow envelope: `workflow_result_envelope`

Control-signal keys:

- `last_control_signal` (`none|break|continue|return|error`)
- `last_control_signal_scope`
- boolean convenience flags (`last_control_signal_break`, `last_control_signal_continue`, `last_control_signal_return`, `last_control_signal_error`)

Step execution dispatch:

- `llm` steps execute through the generic LLM step executor. Prompt resolution, bounded tool use, structured validation, and tool-policy enforcement happen inside that runtime path.
- `deterministic` steps execute via the deterministic action registry and MCP fallback bridge.
- `control` steps are deterministic executions reserved for workflow control and validation primitives.
- `subworkflow` steps invoke a child workflow and map its outputs back into the parent context.
- The current canonical tool-calling workflow no longer models plan/validate/execute/backfill as separate VWL states. Ordinary tool-augmented reasoning happens inside a single prompt-driven `llm` step, with critic/completion policy stages remaining explicit.

### 7.1 Thinking-Card Progress Terminal Precedence (JVNAUTOSCI-1316)

For chat progress payloads consumed by the thinking card:

- Terminal status MUST be authoritative over liveness when rendering run-state badges.
- Terminal detection MUST check both `status` and `orchestrator_status` (and treat values such as `completed|done`, `failed|error`, `cancelled|aborted|canceled`, `terminated` as terminal).
- If any terminal status is present, the badge MUST render terminal state immediately (never remain `Active`/`Waiting`/`Stalled` because of `liveness_state`).
- `liveness_state` remains informational for metadata (for example "Last activity"), not authoritative for terminality.
- Progress polling SHOULD stop once terminality is detected (except explicit user-triggered refresh flows).

### 7.2 Thinking-Card Workflow Visibility Contract (JVNAUTOSCI-1378)

For live chat progress payloads used by workflow-aware thinking-card rendering:

- the payload SHOULD include `workflow_stage_path` derived from the canonical conversation-turn stage model, not just raw internal event/status labels;
- `workflow_stage_path.workflow_id` SHOULD represent the mapped execution-stage consensus when the observed stage path yields one unambiguous workflow, and SHOULD fall back to the selected-route hint only when the stage path itself carries no workflow membership;
- `workflow_stage_path.observed_workflow_ids` SHOULD expose the workflow IDs actually observed in the mapped stage path so route selection (`selected_workflow_id`) and execution-stage membership can be compared without ambiguity;
- workflow discovery SHOULD emit an explicit completion payload even when zero workflows match, so the UI can render a visible "no applicable workflow found" step rather than silently omitting discovery outcome;
- workflow discovery payloads SHOULD remain explicit even when both routing matches and near-match candidates are empty, so selector fallback, gap-recovery, and turn-execution diagnostics can distinguish "discovery ran and found nothing" from "discovery did not run";
- the authoritative workflow-capability search substrate SHOULD self-populate from Vontology-authored workflow descriptions when deferred startup indexing is not yet ready, rather than silently collapsing routed turns to builtin-only selector candidates;
- live workflow-dispatch progress SHOULD expose `selected_workflow_id` and SHOULD expose a human-readable `selected_workflow_name` when available;
- selector metadata such as `workflow_selector_verdict` and `workflow_selector_source` SHOULD be preserved in the live payload so the UI can explain why a workflow route was chosen;
- selector fail-closed diagnostics such as `selector_prompt_unavailable` or
  `selector_prompt_missing_candidate_list` SHOULD remain visible in live payloads
  and traces when authoritative routing prompt content is unavailable or
  malformed;
- tool history and tool success/failure/pending counters SHOULD be derived from concrete tool lifecycle events (`tool_call_start` / terminal tool events) rather than route-selection metadata such as `workflow_task`;
- thinking-card step labels SHOULD prefer canonical workflow-stage labels (for example `Workflow discovery`, `Workflow dispatch`, `Plan tool calls`) over transport/internal labels such as `orchestrator_start`.

### 7.3 Completion-Gate Terminal Semantics (JVNAUTOSCI-1380)

For workflow-governed conversation turns:

- completion safety MUST be determined by completion-gate outputs, not by whether a response text was produced;
- if `completion_gate_safe_to_claim_completion=false`, the orchestrator MUST NOT surface the turn as `completed`;
- unresolved required effects or inconclusive mutation/representation verification MUST produce `follow_up_required` or `failed`, with explicit blocking effect IDs and failure codes preserved in diagnostics;
- once tool-pipeline contract resolution succeeds, the runtime MUST either emit a `workflow_handoff` start boundary or emit a more local failed handoff boundary with blocker reason/error metadata; `tool_dispatch_not_started` alone is not a sufficient terminal explanation when a narrower root cause is available;
- UI/progress surfaces MUST derive terminal state from authoritative completion status (`completed`, `follow_up_required`, `failed`, `cancelled`) rather than from liveness/heartbeat signals.

### 7.4 Workflow Episode Continuation and Repair Semantics (JVNAUTOSCI-1380)

Workflow routing for multi-turn agentive work MUST be stateful.

Authoritative routing context SHOULD include:

- active workflow/episode identity,
- active workflow source (`conversation_turn`, `durable_instance`, or equivalent),
- unresolved required effects,
- prior completion-gate verdict,
- and any workflow contract/profile identifiers already resolved for the episode.

Continuation/repair rules:

- terse continuation turns such as `please proceed`, `continue`, or `go ahead` SHOULD prefer continuing the active workflow episode over rediscovering a new workflow from the raw prompt;
- terse repair turns such as `you did not create it`, `that relation was not added`, or `this is still missing` SHOULD route into verification/repair on the active workflow episode rather than generic search fallback;
- previously resolved representation contract profiles MAY be reused for follow-up and repair turns when the active episode and artefact context remain compatible;
- if the active workflow episode cannot be resumed safely, the system MUST fail closed with explicit diagnostics rather than silently claiming the work was completed.

## 8. Metadata Contract Semantics

Per-state metadata keys currently used:

- `preconditions`
- `effects`
- `reads_variables`
- `writes_variables`
- `reads_context_keys`
- `writes_context_keys`
- `context_input_mappings`
- `tool_output_context_mappings`
- `launch_input_contract`
- `subworkflow_contract`
- `invokes_workflow`
- `retry_policy`
- `approval_gate`
- `mutation_authority`
- `idempotency_policy`
- `checkpoint_policy`
- `loop_scope_id`
- `fork_id`
- `join_fork_id`
- `variable_declarations`
- `progress_projection`

### 8.1 Thinking-Card Progress Projection Metadata

Workflow and state metadata MAY declare `progress_projection` (also accepted
as `workflow_progress_projection` or `thinking_card_progress_projection`) to
name specific facts that the Thinking card may show while a step runs or
completes. This is the authored surface for user-visible salient progress
facts. Python support code may validate, redact, truncate, transport, and
render the facts, but it MUST NOT infer domain-specific items such as email
subjects or paper titles from workflow IDs, tool names, or source strings.
Durable Vontology-authored projection metadata is stored as singleton JSON
text relations on workflow or step concepts via
`#V#hasWorkflowProgressProjectionJson`.

Example:

```json
{
  "schema_version": "workflow_progress_projection.v1",
  "facts": [
    {
      "fact_id": "email_subject",
      "label": "Email subject",
      "source_path": "context.email.subject",
      "value_kind": "title",
      "visibility": "default",
      "contract_id": "#V#email_subject_progress_fact"
    },
    {
      "fact_id": "paper_concept",
      "label": "Paper concept",
      "source_path": "action_outputs.paper.concept_id",
      "value_kind": "concept_id",
      "visibility": "expert"
    }
  ]
}
```

Projection facts are evaluated against `context`, `context_after`,
`context_before`, `action_outputs`/`output_payload`, and `event` roots. Each
fact SHOULD include a stable `fact_id`, a human label, `source_path`,
`value_kind`, `visibility`, and, where useful, a Vontology `contract_id` that
names the projection contract.

Visibility is a display-density hint:

- `default`, `display`, `user`, or `thinking_card_default`: eligible for the
  compact user card when the value is available and not redacted.
- `expert`: shown only in expert/debug expanded details.
- `debug`: shown only in debug expanded details.
- `hidden`, `none`, or `telemetry_only`: carried only as telemetry and not
  displayed by the Thinking card.

Sensitive facts MUST either declare a redaction policy or explicitly set
`allow_raw=true`. Sensitive facts without a redaction policy fail closed:
telemetry records that the projection existed and was redacted, but no raw
value is rendered.

### 8.2 Runtime Policy Metadata

Canonical workflow-step runtime policy payloads are stored as singleton text relations on the step concept:

- `#V#hasWorkflowStepRetryPolicyJson`
- `#V#hasWorkflowStepApprovalGateJson`
- `#V#hasWorkflowStepMutationAuthorityJson`
- `#V#hasWorkflowStepIdempotencyPolicyJson`
- `#V#hasWorkflowStepCheckpointPolicyJson`
- `#V#hasWorkflowProgressProjectionJson`

Workflow-level long-horizon policy payloads are stored as singleton text relations on the workflow concept:

- `#V#hasWorkflowPlanStatePolicyJson`
- `#V#hasWorkflowCompletionGateJson`
- `#V#hasWorkflowLaunchInputContractJson`
- `#V#hasWorkflowProgressProjectionJson`

Workflow-template authoring metadata is stored as first-class Vontology text relations on template concepts:

- `#V#hasWorkflowTemplateId`
- `#V#hasWorkflowTemplateProfileJson`
- `#V#hasWorkflowSpecTemplateJson`
- `#V#hasWorkflowTemplateDefaultDescription`

Current schema versions:

- `workflow_step_retry_policy.v1`
- `workflow_step_approval_gate.v1`
- `workflow_step_mutation_authority.v1`
- `workflow_step_idempotency_policy.v1`
- `workflow_step_checkpoint_policy.v1`
- `workflow_plan_state_policy.v1`
- `workflow_completion_gate.v1`
- `workflow_terminal_success_contract.v1`
- `workflow_launch_input_contract.v1`
- `workflow_template_profile.v1`

Normative semantics:

- invalid policy payloads MUST fail workflow loading with deterministic error codes;
- retry and idempotency policies currently apply only to single-action states;
- approval gates MUST fail closed when the required approval context key is absent or falsey;
- destructive or otherwise high-risk approval-gated states SHOULD provide an explicit `on_approval_required` route to a blocked terminal or escalation state;
- mutation-authority policies cap the strongest mutation class a step may exercise, using `maximum_level` from `workflow_step_mutation_authority.v1`;
- effective mutation authority is the intersection of user grant, workflow-step cap, global runtime policy, and environment hard stops, and MUST fail closed when any input is invalid;
- mutation guardrail decisions use stable outcomes `allowed|blocked|approval_required|deferred`;
- every mutation guardrail evaluation and every tool-surface guardrail hit MUST emit explicit `mutation_guardrail` telemetry with workflow/step IDs when available, risk class, required/effective authority, decision basis, and block source;
- checkpoint policies update the shared runtime plan-state artefact rather than introducing workflow-specific Python persistence logic;
- plan-state items support `pending|in_progress|blocked|done` statuses, bounded checkpoint history, periodic summary snapshots, resumable cursor snapshots, and resume telemetry;
- completion gates MUST fail closed before terminal success when required plan items or required context keys are not satisfied;
- terminal-success contracts MAY declare which terminal statuses count as success for a workflow boundary and which execution-summary fields MUST be present before a parent turn or workflow may treat the child workflow as successful;
- when a workflow declares a terminal-success contract, runtimes and parent completion gates MUST fail closed if terminal status or other contracted summary fields are absent or violate the contract;
- launch input contracts MAY map invocation-context values into workflow context keys before the initial state executes;
- launch input contracts MUST remain declarative, so reusable extractors such as quoted-text extraction or workflow-ID list extraction are configured in metadata rather than hard-coded for specific workflow IDs.
- typed-subworkflow route maps MAY be published on workflow concepts via `#V#hasWorkflowTypedSubworkflowRouteMapJson` using schema `workflow_typed_subworkflow_route_map.v1`;
- typed-subworkflow route maps define supported route keys, candidate subworkflow lists, threshold defaults, and unavailable/low-confidence fallback semantics declaratively;
- runtimes that depend on typed-subworkflow route maps MUST resolve them from Vontology authority and fail closed with explicit diagnostics when the required metadata is missing or invalid.

### 8.1 Terminal outcome receipts

The canonical conversation-turn workflow uses
`terminal_outcome_receipt.v1` (`#V#terminal_outcome_receipt`) to carry the
represented postcondition critic's terminal judgement into completion and
recovery. The receipt records:

- the LLM-authored outcome and causal stage;
- bounded references to the evidence used for that judgement;
- effects already committed and obligations still outstanding;
- retryability and the recovery affordances that remain available;
- a non-authoritative learning candidate when the trajectory may warrant later
  review; and
- decision provenance and redaction status.

The authority split is normative:

- VWL controls the critic, completion-gate, recovery-decision, retry, alternate
  tool/workflow, answer, and follow-up transitions;
- the represented critic and recovery prompts ask the LLM to judge semantics
  from current evidence and select among represented affordances;
- Python MAY validate and bound the receipt, redact sensitive fields, persist
  it, expose telemetry, and veto a claimed success that conflicts with a hard
  required-effect or safety invariant;
- Python MUST NOT infer receipt outcomes, causes, recovery actions, or
  user-facing failure wording from tool names, workflow IDs, domains, or error
  strings; and
- learning candidates MUST NOT become prompt, workflow, or KB authority without
  a separate evidence-gated promotion workflow.

A missing or invalid receipt must remain visible as typed validation evidence.
It may fail closed for completion safety, but it must not erase already
committed effects or the remaining represented recovery opportunities.

Validation phases:

- pre-action: preconditions/reads checks,
- post-action: effects/writes checks.

Validation mode env var:

- `VON_WORKFLOW_METADATA_VALIDATION_MODE`
- values: `enforce`, `warn`, `off`

Semantics:

- `enforce`: metadata failures fail execution.
- `warn`: metadata failures are recorded but non-blocking.
- `off`: metadata validation is skipped (with explicit skip diagnostics).

## 9. Context and Mapping Semantics

### 9.1 Input Mapping

Two mechanisms:

- text mapping in `hasInputMap` (`key=value` or `key:value`),
- semantic mapping via `workflow_step_maps_context_key_to_tool_param` concepts.

Static step-local defaults that are not plain strings MUST be encoded as
`key=json:<json>`. The loader restores the JSON payload as a structured action
input so workflows can carry authored dict/list/scalar policy data without
falling back to Python-owned branching.

Semantic mappings bind:

- a workflow context key,
- to a specific tool parameter name,
- for the step invocation.

### 9.2 Output Mapping

`workflow_step_maps_tool_output_field_to_context_key` mappings bind:

- a tool output field name,
- to a target workflow context key.

Runtime writes mapping events into context diagnostics for traceability.

## 10. Control-Flow and Subworkflow Semantics

Control-flow action IDs:

- `workflow_control.break`
- `workflow_control.continue`
- `workflow_control.fork`
- `workflow_control.join`
- `workflow_control.for_each`
- `workflow_control.context_set`

### 10.1 Break/Continue

- `workflow_control.break` MUST be routed by an explicit `on_break` transition.
- `workflow_control.continue` MUST be routed by an explicit `on_continue` transition.
- Validation rejects break/continue usage without loop-scope declaration (`loop_scope_id` metadata or explicit action input).

### 10.2 Fork/Join

Fork defaults:

- failure policy default: `fail_fast`
- supported failure policies: `fail_fast`, `collect_errors`, `allow_partial_success`
- merge policy default: `deterministic_last_writer_wins`

Runtime semantics:

- Fork executes branch workflows in isolated branch contexts.
- Join requires a matching prior fork ID.
- Join merges successful branch declared outputs in deterministic branch-ID order.
- Validation rejects join usage without a matching fork declaration.

### 10.3 Subworkflow

Subworkflow action ID:

- `workflow_invoke_subworkflow`

Contract schema:

- `workflow_subworkflow_contract.v1`

Failure modes:

- `propagate_as_action_failure`
- `capture_child_failure`

Subworkflow contracts carry:

- child workflow ID,
- input mappings,
- output mappings,
- required/provided input-output lists,
- failure mode.

Recursion guards:

- cycle guard via invocation chain check,
- depth limit (env: `VON_WORKFLOW_SUBWORKFLOW_MAX_DEPTH`),
- invocation budget limit (env: `VON_WORKFLOW_SUBWORKFLOW_MAX_INVOCATIONS`).

Nested propagation:

- child workflow envelopes are surfaced through `subworkflow_result_envelope`,
- non-`none` child control signals propagate to parent action outputs.

### 10.13 For Each Fan-Out

`workflow_control.for_each` is the canonical sequential fan-out primitive for bounded collection processing.

Required inputs:

- `workflow_id`
- one of `items`, `items_context_key`, or `items_path`

Optional inputs:

- `item_context_key` (default `current_item`)
- `index_context_key` (default `index`)
- `max_items`
- `max_transitions`
- `success_policy` (`all_must_succeed` or `allow_partial`)

Runtime semantics:

- items are processed in deterministic source order;
- each item executes the declared child workflow in an isolated child context;
- the child context receives the bound item and index keys;
- per-item results are returned in `iteration_results`;
- aggregate counts are returned in `for_each_success_count`, `for_each_error_count`, and `for_each_partial_success`;
- `all_must_succeed` returns failure when any child execution fails;
- `allow_partial` returns success while preserving structured failure details.

### 10.3a Context Set (JVNAUTOSCI-1440)

`workflow_control.context_set` is the canonical declarative context-mutation primitive for VWL.

Required inputs:

- `assignments` (list of dicts): each assignment MUST contain:
  - `key` (string, required): the target context key to write.
  - `value` (any, optional): literal value to assign.
  - `value_from_context` (string, optional): context path to copy from (resolved via `resolve_context_path`). If the source path is absent, the target key receives `None`.
  - `value_from_context_options` (list of strings, optional): candidate context paths to check in order. The first present, non-empty value is copied.
  - `skip_if_unresolved` (boolean-like, optional): when true, an unresolved `value_from_context` or `value_from_context_options` assignment is skipped rather than writing `None`.
  - `preserve_existing` (boolean-like, optional): when true, an existing present target value is left unchanged.
  - Exactly one of `value`, `value_from_context`, or `value_from_context_options` SHOULD be present. If both `value_from_context_options` and `value_from_context` are supplied, `value_from_context_options` takes precedence. If neither contextual source is supplied, `value` is used.

Outputs:

- One output key per assignment (using the declared `key` as the output key), merged into context by the standard action-output merge.
- `_context_set_applied_keys` (list of strings): ordered list of keys that were written.

Error semantics:

- Missing or non-list `assignments` → `context_set:assignments_missing_or_invalid`.
- Non-mapping assignment entry → `context_set:assignment_{index}_not_mapping`.
- Missing or empty `key` → `context_set:assignment_{index}_missing_key`.
- Non-list `value_from_context_options` → `context_set:assignment_{index}_value_from_context_options_not_sequence`.

Usage pattern:

```json
{
  "invokesAction": "workflow_control.context_set",
  "hasInputMap": [
    "assignments=[{\"key\":\"my_flag\",\"value\":true},{\"key\":\"title\",\"value_from_context_options\":[\"title\",\"metadata.title\"],\"skip_if_unresolved\":true}]"
  ]
}
```

### 10.3b Variable Declarations (JVNAUTOSCI-1440)

Workflow-level variable declarations provide deterministic default initialisation for workflow context keys before execution begins.

Ontology representation:

- The workflow concept is linked to variable concepts via the `hasWorkflowVariable` predicate (canonical: `#V#hasWorkflowVariable`).
- Each variable concept SHOULD contain `concept_data` with:
  - `variable_name` (preferred) or `key` (fallback): the context key name. The resolution chain is `variable_name` → `key` → concept `name` → concept ID.
  - `default_value` (optional): default value assigned during initialisation. May be any JSON-serialisable value, including `null`.

Compilation:

- `build_workflow_process_graph()` reads `hasWorkflowVariable` relationships from the workflow concept.
- Each resolved variable concept is compiled into a declaration dict:
  ```json
  {"concept_id": "<id>", "name": "<resolved_name>", "default_value": <value>}
  ```
- The list is stored in the graph output as `variable_declarations`.
- `load_workflow_definition_from_vontology()` propagates `variable_declarations` into `workflow_metadata`.

Runtime initialisation:

- `WorkflowExecutor.run()` reads `variable_declarations` from `definition.metadata` before entering the state-machine loop.
- For each declaration, if the variable `name` is **not already present** in the context, the engine sets `context[name] = default_value`.
- Caller-supplied context data takes precedence over declared defaults.
- Malformed declarations (non-mapping entries, empty names) are silently skipped.

This enables workflow authors to declare expected context variables and their defaults in the ontology, ensuring consistent initial state without requiring callers to supply every expected key.

### 10.4 File-Copy Upload Routing Workflows (JVNAUTOSCI-1309)

Canonical workflow IDs:

- `#V#file_copy_upload_classification_workflow`
- `#V#file_copy_upload_handler_workflow`
- `#V#file_copy_interpretation_workflow` (baseline safe path)

Classification outputs (persisted and propagated to the handler) include:

- `route_key` (`scholarly|cv|business_card|meeting|interpret|noop`)
- `route_mode` (`specialised|interpret|fail_closed|noop`)
- `route_confidence`, `route_reasons`, `target_workflow_id`, `target_workflow_available`
- `unsupported_specialised_route` and `unsupported_route_reason` when a mutation-class route was selected but no specialised workflow is available.
- `typed_subworkflow_route_map_source` and `typed_subworkflow_route_map_schema_version` so route decisions remain traceable to the authoritative VWL metadata surface that governed them.

Safety semantics:

- `#V#file_copy_upload_classification_workflow` now resolves its default routing policy from `#V#hasWorkflowTypedSubworkflowRouteMapJson` rather than from Python-owned route maps or hard-coded downstream workflow IDs.
- Low-confidence mutation routes MUST fail closed (`route_mode=fail_closed`) rather than invoking mutation workflows.
- When specialised CV/business-card workflows are not available, classification MUST emit explicit unsupported-route diagnostics (`unsupported_specialised_route=true`) and route to non-mutation handling (`interpret` or `noop` per fallback policy).
- Missing or invalid typed-subworkflow route-map metadata MUST no-op classification with explicit diagnostics rather than silently recreating routing policy in code.
- Handler outcome persistence (`#V#has_file_copy_upload_route_outcome_json`) records selected/effective route mode, success, reasons, and unsupported-route diagnostics for post-run inspection.

Event-launch semantics:

- `file_copy.uploaded` launches resolve through persisted workflow event bindings; the canonical production route is a single enabled binding to `#V#file_copy_upload_handler_workflow` to avoid duplicate uncontrolled launches.
- Production runtime no longer bootstraps default `file_copy.uploaded` bindings from Python. Missing bindings MUST surface explicit `workflow_not_configured` diagnostics instead of silently recreating authority at runtime.
- Operators should resolve obsolete `file_copy.uploaded` routes through workflow binding governance (`workflow_list_event_bindings`, `workflow_set_event_binding_enabled`, `workflow_delete_event_binding`) rather than by adding Python-side routing switches.

### 10.5 PDF Diagram-Aware Organisation Extraction (JVNAUTOSCI-1017)

When `#V#file_copy_interpretation_workflow` runs `interpret_file_copy` for PDF documents:

- The interpreter MUST preserve prose-derived extraction and diagram-derived extraction as separate candidate sets.
- Diagram analysis SHOULD use open-source components only (PyMuPDF for page/image access and OCR via `pytesseract` when available).
- Candidate entities and candidate relations derived from diagrams MUST be emitted as verification candidates (not automatic ontology assertions).
- Every extracted diagram candidate MUST carry provenance metadata (source, page/figure scope, extraction method, timestamp/evidence).
- Output MUST include explicit `requires_human_confirmation=true` semantics for diagram-derived candidates.
- If diagram OCR dependencies are unavailable, interpretation MUST fail soft (diagnostic payload preserved) rather than silently asserting diagram-derived facts.

### 10.6 Scholarly Paper Representation Contract (JVNAUTOSCI-1369 / JVNAUTOSCI-1372)

For paper-representation intents, VWL required-effects semantics MUST ensure execution paths include mutation-capable tooling, not read-only enrichment alone.

Canonical paper profile source expectations:

- `file_copy` source: required tool set MUST include `materialise_scholarly_representation_for_file_copy`.
- `url` source (including arXiv URLs/IDs): required tool set MUST include `download_paper` plus `materialise_scholarly_representation_for_file_copy`.
- `mixed` source: required tool set SHOULD include both `download_paper` and `materialise_scholarly_representation_for_file_copy`.

Intent boundary for URL inputs:

- A canonical arXiv abstract URL or ID on its own **does** authorise low-risk additive `download_paper` execution and the follow-up scholarly-materialisation path, unless the user explicitly denies mutation.
- Explicit phrasing such as "represent that paper" remains an equivalent positive signal, but it is not required.
- Explicit denial (for example "do not download/store this") MUST block mutation even when a canonical arXiv source is present.
- Deterministic route-level coverage for this boundary is tracked by `JVNAUTOSCI-1399`; the current URL-first regression to fix is `JVNAUTOSCI-1394`.

Minimal-imposition rationale:

- a pasted canonical source URL is sufficient evidence for this low-risk additive representation path;
- workflow policy SHOULD prefer acting on that evidence over asking the user for redundant permission language;
- the fail-closed boundary for this path is explicit denial, not absence of verbs such as `download` or `store`.

Important runtime contract note:

- Current `required_effects_contract.v1` evaluates `required_tools` as an any-of set (one observed required tool can satisfy the effect), so source-specific tool lists MUST be authored to preserve mutation guarantees.
- Read-only tools such as `extract_url` or `get_paper_metadata` MAY be used for enrichment, but MUST NOT be the sole required-effect satisfaction path for paper representation.

Operational expectations for arXiv tool handlers:

- `download_paper` and `finalise_cached_paper` SHOULD stop at durable acquisition and authenticated file-copy registration; they MUST NOT hide scholarly-paper materialisation side effects inside acquisition helpers.
- Explicit scholarly-paper materialisation SHOULD run through `materialise_scholarly_representation_for_file_copy`, which SHOULD expose `attempted`, `verified`, `paper_concept_id`, metadata provenance, and explicit verification/failure fields.

Turn-execution gate expectation:

- Tool invocations whose payload explicitly reports `success=false` MUST be treated as failed execution for required-effect evaluation and completion gating.

Current implementation caveat (JVNAUTOSCI-1415):

- `#V#scholarly_paper_representation_workflow` currently exists as a loadable Vontology workflow concept, but its present graph is legacy/incomplete: most steps invoke `workflow_creation.emit_marker`, with only author resolution represented as a domain-specific action;
- the current arXiv and upload pathways therefore still depend on explicit acquisition/materialisation tool sequencing (`download_paper`, `finalise_cached_paper`, `materialise_scholarly_representation_for_file_copy`) plus representation-contract and completion-gate semantics, not yet on a fully expressive domain workflow family;
- `JVNAUTOSCI-1415` is the task that repairs/replaces this workflow family with a proper general-paper workflow plus an explicit arXiv wrapper workflow.

### 10.7 Person Representation Contract (JVNAUTOSCI-1369 / JVNAUTOSCI-1373)

For person-representation intents driven by CV/business-card artefacts, required-effects semantics MUST enforce identity materialisation and source linkage before completion can be claimed.

Canonical person profile source expectations:

- `file_copy` source (CV/business-card uploads): required tool set MUST include `interpret_file_copy`.
- `url` source MAY use `extract_url` for enrichment, but file-copy person materialisation remains the canonical mutation path for uploaded artefacts.

Operational expectations for `interpret_file_copy` on person artefacts:

- The handler SHOULD attempt deterministic person materialisation when CV/business-card cues are detected.
- Materialisation SHOULD persist core identity effects (person concept + `hasName`) and source linkage (`#V#documentary_evidence_for` from file-copy concept to person concept).
- Contact/role/affiliation extraction MAY use conservative defaults (`#V#has_email`, `#V#hasRole`, `#V#hasNote`) without additional user questioning.
- If core identity cannot be resolved or source linkage fails, the tool MUST fail closed (`success=false`) and return explicit `person_representation` diagnostics (`attempted`, `verified`, `reason`, IDs, and error details).

Completion-gate expectation:

- Person-representation turns MUST remain non-complete when `interpret_file_copy` reports unresolved core identity effects (for example `reason=person_identity_unresolved`).

### 10.8 Company Representation Contract (JVNAUTOSCI-1369 / JVNAUTOSCI-1374)

For company-representation intents driven by web-page artefacts, required-effects semantics MUST enforce company identity + URL identity materialisation and source linkage before completion can be claimed.

Canonical company profile source expectations:

- `file_copy` source (uploaded web-page artefacts): required tool set MUST include `interpret_file_copy`.
- `url` source MAY use `extract_url` for enrichment, but file-copy company materialisation is the canonical mutation path for uploaded web-page artefacts.

Operational expectations for `interpret_file_copy` on company artefacts:

- The handler SHOULD attempt deterministic company materialisation when web-page/company cues are detected.
- Materialisation SHOULD persist core identity effects (company concept + `hasName`), URL identity (`#V#has_url`), and source linkage (`#V#documentary_evidence_for` from file-copy concept to company concept).
- Descriptor extraction MAY use conservative defaults (`#V#hasNote`) without additional user questioning.
- If core company identity, URL identity, or source linkage cannot be resolved, the tool MUST fail closed (`success=false`) and return explicit `company_representation` diagnostics (`attempted`, `verified`, `reason`, IDs, URLs, and error details).

Completion-gate expectation:

- Company-representation turns MUST remain non-complete when `interpret_file_copy` reports unresolved core company effects (for example `reason=company_identity_unresolved` or `reason=company_url_unresolved`).

### 10.9 Meeting Representation Contract (JVNAUTOSCI-1369 / JVNAUTOSCI-1375)

For meeting-representation intents driven by transcript/calendar artefacts, required-effects semantics MUST enforce meeting identity materialisation and source linkage before completion can be claimed.

Canonical meeting profile source expectations:

- `file_copy` source (transcript/calendar uploads): required tool set MUST include `interpret_file_copy`.
- `url` source MAY use `extract_url` for enrichment, but file-copy meeting materialisation is the canonical mutation path for uploaded artefacts.

Operational expectations for `interpret_file_copy` on meeting artefacts:

- The handler SHOULD attempt deterministic meeting materialisation when transcript/calendar cues are detected.
- Materialisation SHOULD persist core identity effects (meeting concept + `hasName`) and source linkage (`#V#documentary_evidence_for` from file-copy concept to meeting concept).
- Date/time, participant, and outcome extraction MAY use conservative defaults (`#V#hasNote`) with explicit provenance.
- If core meeting identity cannot be resolved or source linkage fails, the tool MUST fail closed (`success=false`) and return explicit `meeting_representation` diagnostics (`attempted`, `verified`, `reason`, IDs, and error details).

Completion-gate expectation:

- Meeting-representation turns MUST remain non-complete when `interpret_file_copy` reports unresolved core meeting effects (for example `reason=meeting_identity_unresolved`).

### 10.10 Cross-Domain Representation Regression Suite (JVNAUTOSCI-1369 / JVNAUTOSCI-1376)

Canonical regression coverage for representation intent contracts is maintained in:

- `tests/backend/test_representation_intent_cross_domain_regression_suite.py`
- shared fixtures/helpers: `tests/backend/representation_intent_regression_helpers.py`

Required suite behaviours:

- For each anchor domain (`paper`, `person`, `company`, `meeting`), unresolved required effects MUST block completion with explicit domain failure codes in `completion_gate.blocking_failure_codes`.
- Verified required effects MUST permit completion (`decision=completed`, `safe_to_claim_completion=true`).
- Contract policy telemetry MUST preserve minimal-imposition defaults (`auto_apply_low_risk_defaults=true`, `requires_explicit_user_decision_for_high_risk=true`).
- Repeated runs with unchanged prompt + tool context MUST remain idempotent at contract/effect/gate level.
- Gateway-path `interpret_file_copy` failures for unresolved representation verification MUST fail closed with explicit `persist_errors` reason codes per domain.

Extension rule:

- Any new representation domain profile added to VWL MUST add a corresponding scenario to this suite before merge.

### 10.11 Workflow-Governed Low-Imposition Knowledge Acquisition (JVNAUTOSCI-1380)

Low-imposition knowledge acquisition is a workflow policy, not just a prompt style.

Canonical Vontology surface:

- profile type: `#V#knowledge_acquisition_profile`
- profile payload predicate: `#V#has_knowledge_acquisition_profile_json`
- workflow-to-profile link predicate: `#V#has_knowledge_acquisition_profile`
- current canonical profile: `#V#knowledge_acquisition_profile_low_imposition_relation_completion`

Current runtime anchor:

- `#V#rumination_workflow` relation-completion assess/dispatch reads its knowledge-acquisition policy from the linked profile concept.

Current canonical profile payload shape:

- `relation_candidate_priority_policy`
  Defines explicit predicate-priority scores for relation-completion candidate ranking. Runtime ranking MUST resolve from this represented policy rather than lexical keyword matching in Python.
- `relation_auto_apply_policy`
  Defines default and per-predicate confidence/evidence/source-adjustment semantics for low-risk auto-apply behaviour.

Normative policy semantics:

- workflows MUST retrieve existing context/evidence first before asking the user for new input;
- relation-completion candidate ranking MUST resolve from explicit represented predicate priorities in the linked profile, not English substring heuristics;
- low-risk defaults MAY be auto-applied only when the linked profile policy allows it and confidence/evidence thresholds are met;
- high-risk or ambiguous changes MUST require explicit user confirmation;
- relation-completion runs SHOULD ask at most one focused clarification question per run when machine-side evidence is insufficient;
- if the linked acquisition profile cannot be resolved, the workflow MUST fail closed with explicit `knowledge_acquisition_profile_unavailable` diagnostics rather than falling back to ad hoc prompting.
- if the linked acquisition profile is present but incomplete/invalid for the workflow's dispatch mode, the workflow MUST fail closed with explicit `knowledge_acquisition_profile_invalid` diagnostics rather than synthesising fallback policy in Python.

Minimal-imposition mutation semantics:

- human interruption is an exception path, not the default operational posture for ordinary Von workflows;
- low-risk additive Vontology writes SHOULD default-allow when the workflow has evidence-backed inputs and there is no explicit user denial;
- non-delete mutations of existing state MAY proceed when the user has clearly requested them;
- destructive mutations MUST branch through explicit `on_approval_required` confirmation/escalation states rather than relying on blanket pre-emptive hesitation;
- approval gates are targeted risk controls for destructive/high-risk actions, not a default doctrine for all mutations.

Persistence semantics:

- uncertain or provisional relation proposals SHOULD be stored through the canonical uncertain-assertion pathway, not legacy side channels;
- acquisition workflows SHOULD preserve provenance (`source`, interaction identifier, evidence count, confidence score) for later promotion/audit.

### 10.12 Workflow-Governed Episode Self-Improvement Profiles (JVNAUTOSCI-1993)

Critique-driven self-improvement policy is a workflow contract, not a Python
constant slab.

Canonical Vontology surface:

- profile type: `#V#episode_self_improvement_profile`
- profile payload predicate: `#V#has_episode_self_improvement_profile_json`
- workflow-to-profile link predicate: `#V#has_episode_self_improvement_profile`
- current canonical profile: `#V#episode_self_improvement_profile_workflow_revision_default`

Current runtime anchors:

- `#V#episode_evaluation_workflow`
- `#V#episode_self_improvement_proposal_workflow`
- `#V#episode_self_improvement_promotion_workflow`

Current canonical profile payload shape:

- `candidate_selection_policy`
  Defines represented launch budget, priority ordering, eligible target
  surfaces, and dedupe identity for critique-driven candidate selection.
- `benchmark_policy`
  Defines represented benchmark evidence budget, including scan limit and audit
  depth for proposal and promotion context construction.

Normative policy semantics:

- self-improvement candidate launch budget MUST resolve from the linked profile,
  not from Python constants;
- priority ordering and eligible target-surface policy MUST resolve from the
  linked profile, not from hard-coded workflow-only filtering;
- dedupe identity for candidate selection SHOULD remain explicit in the linked
  profile so later surface expansion does not require hidden Python policy;
- benchmark evidence budget for proposal and promotion context MUST resolve
  from the linked profile, not from ad hoc `scan_limit` / `max_audit_cases`
  literals in support code;
- if the linked episode self-improvement profile cannot be resolved, the live
  path MUST fail closed with explicit
  `episode_self_improvement_profile_unavailable` diagnostics;
- if the linked profile is present but incomplete or invalid, the live path
  MUST fail closed with explicit
  `episode_self_improvement_profile_invalid` diagnostics rather than
  synthesising fallback launch or benchmark policy in Python.

### 10.13 Representation and Acquisition Profiles as Workflow Contracts (JVNAUTOSCI-1380)

Representation profiles and knowledge-acquisition profiles are workflow contracts.

Therefore:

- workflow-governed runtime code SHOULD resolve contract/profile semantics from Vontology profile concepts, not from hard-coded prompt wording;
- canonical profile concepts SHOULD exist before runtime use and SHOULD be bootstrapped through shared service pathways;
- missing canonical profile concepts are configuration/runtime dependency failures and MUST remain visible in diagnostics;
- adding a new agentive workflow domain SHOULD normally include both:
  - a workflow/process graph, and
  - a Vontology-backed contract/profile concept that defines completion or acquisition policy for that domain.

## 11. Durable Runtime Semantics

### 11.1 Instance Model

Durable instance statuses:

- `pending`
- `running`
- `completed`
- `failed`
- `cancelled`
- `paused`

Terminal statuses:

- `completed`, `failed`, `cancelled`

Retry fields:

- `retry_count`
- `max_retries`

### 11.2 Worker

Background worker claims pending/resumable work, executes with checkpointing, heartbeat lock extension, and graceful shutdown.

### 11.3 Scheduler

Schedule types:

- `once`
- `interval`
- `cron` (5-field)

Scheduler creates workflow instances through the verified submission pathway (never bypassing runnability verification), updates `next_run_at`, and records polling telemetry.

### 11.4 Event Binding

Persistent event-to-workflow bindings include:

- `event_type`
- `workflow_id`
- `input_mapping`
- `condition` (optional workflow condition spec evaluated against the event payload)
- `enabled`
- revision/actor metadata.

Event-binding conditions use the same condition language as workflow transitions.
The evaluation context exposes the event payload both at top level and under
`event`, plus launch inputs under `inputs`. For example, a task-status binding
that should launch only for completed tasks can declare:

```json
{"kind":"context_value_equals","key":"event.new_status","value":"completed"}
```

This represented condition is the authority for status/event selection. Python
emits structured events and evaluates the stored condition; it must not encode
task-status trigger policy in environment allow-lists or code-side status sets.

## 12. Runnability and Safety Gates

Instance creation uses the canonical verified pathway:

- preflight runnable verification,
- instance create,
- postflight runnable verification,
- fail-closed rejection or failure marking if checks do not pass.

This prevents false "started/running" claims for non-runnable workflows.

## 13. Automatic Background Operation

Application startup (`utils_flask`) initialises durable workflows asynchronously by default:

- gated by `VON_DURABLE_WORKFLOWS_ENABLE=1`,
- starts worker and scheduler,
- recovers orphaned instances,
- records startup status in app config,
- registers graceful shutdown hook,
- bootstraps identity-resolution background schedule.

Blocking startup can be enabled via:

- `VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP=1`

Operational cadence controls include:

- `VON_DURABLE_WORKER_POLL_INTERVAL`
- `VON_DURABLE_SCHEDULER_CHECK_INTERVAL`

## 14. VWL MCP Standard Library

### 14.1 Discovery and Diagnostics

- `workflow_list_definitions`
- `workflow_mcp_health_check`
- `workflow_list_event_bindings`

Operator-facing discovery rule:

- `workflow_list_definitions` SHOULD remain responsive even when full parity diagnostics are still being built.
- When deferred inventory/parity work has not yet published a full snapshot, the tool MAY return `parity_inventory.build_state="pending_background_build"` with reason code `inventory_pending_background_build`.
- When a listed workflow is still represented only by a lazy registry placeholder, the tool MAY return `definition_loaded=false` and `definition_identity.build_state="pending_lazy_definition"` rather than forcing immediate full definition materialisation.
- Clients and authoring workflows MUST treat this as a non-error read state rather than inferring workflow-authority drift from the temporary absence of a full parity snapshot.

### 14.2 Instance Control

- `workflow_create_instance`
- `workflow_execute`
- `workflow_list_instances`
- `workflow_list_execution_traces`
- `workflow_get_instance`
- `workflow_get_execution_trace`
- `workflow_cancel_instance`
- `workflow_retry_instance`

### 14.3 Event Binding Control

- `workflow_bind_event`
- `workflow_list_event_bindings`
- `workflow_set_event_binding_enabled`
- `workflow_delete_event_binding`

### 14.4 Schedule Control

- `workflow_create_schedule`
- `workflow_list_schedules`
- `workflow_get_schedule`
- `workflow_set_schedule_enabled`
- `workflow_delete_schedule`
- `workflow_trigger_schedule`

These tools are the default operational control surface for VWL runtime behaviour.
Workflow instance and schedule creation paths canonicalise namespace context to
`#V#user@org` (or `#V#user` when no organisation scope exists) via the
namespace service. Legacy slash-form inputs may still be accepted on read/query
surfaces or normalised at write boundaries for compatibility, but new
authoritative workflow launches must not emit fresh `user/org` namespaces.

Awaited durable execution contract:

- `workflow_execute` MUST use the canonical verified submission pathway and MUST NOT introduce a parallel launch bypass.
- When `await_terminal=true`, `workflow_execute` SHOULD poll the launched durable instance until a terminal status or timeout, then return bounded execution telemetry including:
  - final/current status,
  - progress snapshot,
  - `workflow_result_envelope`,
  - latest step-result envelope and optional full step-result envelope list,
  - metadata-validation summary/events,
  - `execution_trace_id` when available.
- Durable runs SHOULD persist a workflow execution trace and attach the stable `execution_trace_id` link to the workflow instance row.
- `workflow_get_execution_trace` and `workflow_list_execution_traces` are the canonical MCP read surfaces for persisted durable execution traces; inline trace expansion from `workflow_execute` is optional and MUST remain bounded/redacted.

### 14.5 Testing Workflow Theory and Experiment Library

Testing Workflows are now a first-class reusable VWL action family rather than ad-hoc orchestration code. The reusable deterministic surfaces are:

- Theory-slice control: `testing_theory_create_slice`, `testing_theory_import_canonical_context`, `testing_theory_assert_local_claims`, `testing_theory_compute_diff`, `testing_theory_rollback_local_writes`, `testing_theory_promote_validated_claims`, `testing_theory_gc_expired`
- Experiment control: `experiment_create_spec`, `experiment_start_run`, `experiment_record_observation`, `experiment_compute_verdict`, `experiment_emit_learning_signal`, `experiment_execute_target_workflow`, `experiment_execute_regression_suite`
- Scenario helper: `testing_prepare_experiment_spec`
- Evidence inspection: `experiment_run_list`, `experiment_run_get`

Semantic rules:

- Testing workflows SHOULD materialise first-class `#V#testing_theory`, `#V#ephemeral_theory`, `#V#experiment_spec`, and `#V#experiment_run` artefacts rather than hiding the state inside workflow-local context only.
- `experiment_compute_verdict` is the canonical promotion-gate precursor. A passing verdict MAY recommend promotion-ready assertions, but canonical writes MUST still pass through an explicit promotion gate step or workflow.
- `experiment_emit_learning_signal` is the canonical bridge from experiment evidence into workflow-selection learning loops and retained-case replay.
- `experiment_execute_target_workflow` MAY run in awaited mode (`await_terminal=true`). In that mode it SHOULD poll the launched durable child instance to a terminal state or timeout, surface final-status evidence under `workflow_execution`, and record a generic experiment observation when a `run_id` is supplied.
- `testing_prepare_experiment_spec` resolves a workflow-authored `testing_experiment_scenario_template.v1` payload into `experiment.create_spec` inputs plus suggested `theory_slice_inputs` and `seed_claims`.
- `experiment_execute_regression_suite` MAY consume a workflow-authored `testing_regression_suite_policy.v1` payload so tier aliases, suite mode, and benchmark defaults are carried in workflow metadata rather than Python branches.
- New workflows SHOULD use `testing_prepare_experiment_spec`. `testing_prepare_meeting_invitation_spec` remains a compatibility alias for the legacy meeting fixture and MUST NOT be used as the primary authoring surface for new testing workflows.
- Meeting-invitation scenarios SHOULD still default to conservative verdict rules. All declared expected outcomes should be evidenced before a passing verdict is allowed; a pure execution-only observation is intentionally insufficient for promotion.
- Outcome text predicates such as `#V#has_expected_outcome` and `#V#has_observed_outcome` are multi-valued evidence surfaces and MUST NOT be collapsed to singleton semantics.

## 15. Conformance Checklist for Workflow Authors

A VWL workflow is conformant when:

- it has a resolvable workflow concept,
- step graph is loadable and initial step is determinable,
- step invocation targets are unambiguous,
- transition conditions are valid,
- break/continue and fork/join control contracts are valid,
- metadata contracts are syntactically valid,
- required mappings are parseable,
- discovered actions are runnable in current registry,
- workflow typing satisfies canonical workflow authority rules, including subtype satisfaction for required workflow types,
- submission verification passes preflight and postflight.

## 16. Analysis-Driven Refinements (March 2026 Planning Baseline)

This section captures planning-level refinements derived from the March 2026 source-analysis cycle (Codex workflow examples, SKILL models, and prompt-file metadata patterns).

Unless explicitly implemented in runtime code, items below are normative planning targets for upcoming VWL capability work.

### 16.1 Canonical Task Identity and External Projection

- VWL workflows that operate on issue/task systems MUST treat Von-native task entities as canonical state.
- External systems (for example Jira) SHOULD be modelled as projection/mirror surfaces.
- Workflow steps that project state outward MUST carry stable traceability identifiers linking:
  - canonical task identity,
  - workflow instance/request identity,
  - and external issue/update identity.

### 16.2 Fan-Out and Batch Semantics

- VWL SHOULD support first-class fan-out/map execution over task sets with per-item failure isolation.
- Batch-oriented workflows SHOULD provide bounded windows and batch-level gate hooks before promote/commit stages.
- Per-item and per-batch idempotency keys SHOULD be modelled explicitly to avoid duplicate side effects (for example branch, PR, and comment operations).

### 16.3 Long-Horizon Workflow State

VWL now supports KB-authored long-horizon execution state through workflow-level plan-state and completion-gate policies plus per-step checkpoint policies.

Implemented semantics:

- plan-state items are declared in `workflow_plan_state_policy.v1` and recorded in the shared `workflow_plan_state` runtime artefact;
- item status is limited to `pending|in_progress|blocked|done`;
- step checkpoints can update named plan items, emit progress messages, capture summary snapshots, and persist resumable cursor snapshots from declared context paths;
- resumptions increment runtime resume telemetry and preserve bounded checkpoint history;
- terminal success is blocked by `workflow_completion_gate.v1` unless all required plan items and required context keys are satisfied.

### 16.4 Gate and Policy Semantics

- Workflow definitions SHOULD support explicit approval and escalation gates between analysis and mutation stages.
- LLM/tool steps SHOULD allow typed output contracts with deterministic validation failure branches.
- Retry semantics SHOULD include bounded retries, backoff policy, and terminal-failure routing.

### 16.5 Prompt Metadata Contract Extensions

Prompt-bearing workflow steps now support a first-class KB-authored prompt contract. The canonical step-level prompt link is:

- `#V#workflow_step_uses_llm_prompt`

Accepted legacy aliases remain:

- `#V#uses_prompt`
- `#V#hasPromptTemplate`

Prompt or prompt-related concepts MAY declare the following metadata predicates:

- `#V#hasPromptName`
- `#V#hasPromptDescription`
- `#V#hasArgumentHint`
- `#V#usesAgentProfile`
- `#V#usesModelPreference`
- `#V#allowsTool` (repeatable)
- `#V#hasPromptScope` (`workspace|user|organisation|extension`)
- `#V#hasPromptVariables`
- `#V#hasPromptSource`
- `#V#hasToolResolutionPriority`

Step-local defaults MAY be supplied through reserved `hasInputMap` entries:

- `__prompt_defaults=<json object>` (or `prompt_defaults=<json object>`)
- `__prompt_validation_policy=warn|fail` (or `prompt_validation_policy=warn|fail`)

Deterministic merge precedence is:

1. prompt concept metadata,
2. metadata on the first resolved `#V#usesAgentProfile`,
3. step-local defaults from `hasInputMap`.

Conflict handling is field-wise rather than union-based. In particular:

- scalar fields use first-non-empty precedence,
- list-valued fields (`allowed_tools`, `prompt_variables`, `tool_resolution_priority`, `agent_profile_ids`) use the first populated source wholesale,
- `hasToolResolutionPriority` reorders the selected `allowed_tools` subset without expanding it.

Validation and fail-closed behaviour:

- missing prompt text for a declared prompt is always an error,
- workflow-governed bootstrap/support services MAY create or link prompt concepts, but they MUST NOT author or silently repopulate prompt body text from Python;
- planning, enrichment, workflow-gap analysis/test/candidate execution, and analogous workflow-governed prompt consumers MUST fail closed when the authoritative prompt concept is missing or empty;
- unavailable tools or agent profiles follow the declared validation policy (`warn` or `fail`),
- `validation_policy.output_format=json_value` means the runtime MUST parse a single JSON object or array from the raw LLM response, expose it as `validated_json`, and fail the step if parsing does not succeed,
- for `json_value`, `validation_policy.json_field_defaults` MAY declare a map of object field paths to default values. When present, non-object JSON is treated as an empty object for that contract, and missing or blank fields are filled before output mappings run,
- for `json_value`, `validation_policy.required_json_fields` MAY declare object field paths that must be present after defaults and contract normalisation. Missing fields fail the LLM step with `json_required_fields_missing`,
- stable merged prompt state is carried in workflow-state metadata as `prompt_contract`,
- runtime diagnostics are attached to action inputs as `__prompt_resolution_diagnostics`,
- the compiled action/step contract carries the prompt contract as a first-class field; prompt-bearing steps MUST NOT depend on hidden `__prompt_contract` input passthrough,
- fail-policy violations reject workflow loading with deterministic `workflow_prompt_contract_invalid` errors.

LLM policy and model-selection notes:

- compiled `llm` steps MAY carry first-class `llm_policy` fields such as `prompt_candidates`, `selected_prompt_id`, `allowed_tools`, `tool_resolution_priority`, `prompt_text_context_key`, `response_contract_text`, and `selection_policy`;
- `selection_policy` currently distinguishes declared intent such as `fixed`, `adaptive`, and `bandit`, and the runtime records the chosen policy in the step envelope even when the concrete model candidate set is resolved through stage policy and enabled-model settings;
- runtime model candidate discovery is sourced from `enabled_llms` settings (global, organisation, or user scope as applicable), with `active_llm` retained as the fallback/default model when no enabled-model list is present;
- multiple enabled LLMs are therefore part of the canonical VWL execution substrate, not a UI-only convenience.

### 16.6 External SKILL Interoperability

VWL now supports a constrained markdown-SKILL interoperability layer with two aligned modes:

1. direct execution by transpiling a compatible `SKILL.md` artefact into an ephemeral VWL definition and executing it through the ordinary workflow runtime, and
2. deterministic transpilation of that artefact into a reusable VWL workflow definition for later materialisation in Vontology.

Current supported dialect contract:

- canonical `SKILL.md` frontmatter is parsed deterministically using scalar-only YAML-style key/value fields;
- required fields: `name`, `description`;
- optional fields: `argument-hint`, `user-invokable`, `disable-model-invocation`;
- `name` MUST be lowercase kebab-case and MUST match the parent skill directory;
- the current direct-execution adapter is intentionally conservative: it supports instruction-body execution plus explicit deferred resource loads, but does not execute arbitrary scripts or autonomous side effects outside the workflow runtime.

Direct-execution semantics:

- compatible skills are executed through the reusable `skill.execute_markdown` workflow action;
- `disable-model-invocation=true` blocks automatic invocation and fails closed unless the run is explicitly manual;
- provenance (`source_scope`, discovery root, skill directory, skill file, referenced resources) is carried into workflow metadata and runtime outputs;
- transpiled skill workflows use ordinary VWL checkpoint and completion-gate semantics, so they remain visible to the same lightweight runtime and observability surface as any other workflow.

Skill metadata projection for Vontology now uses the following predicate set:

- `#V#has_skill_name`
- `#V#has_skill_description`
- `#V#has_skill_argument_hint`
- `#V#is_user_invokable`
- `#V#disables_model_invocation`
- `#V#has_skill_source_scope` (`project|personal|extension|shared`)
- `#V#has_skill_discovery_location`

Skill loading preserves progressive disclosure semantics:

1. discovery by lightweight metadata (`name`/`description`),
2. instruction-body load on relevance or explicit invocation,
3. deferred resource-file loading on demand.

This allows large skill catalogues without unbounded prompt-context consumption and aligns with deterministic VWL runnability and observability goals.

### 16.7 Provider-Agnostic Integration Boundary

- Workflow logic SHOULD remain provider-agnostic (GitHub/GitLab/Jira adapters as bindings, not baked workflow semantics).
- Security- and quality-sensitive loops SHOULD expose policy hooks for risk-tiered review requirements.
- Scanner/model/tool provenance SHOULD be preserved in context diagnostics for reproducibility audits.

### 16.8 Implementation Planning Output Requirement

After analysis and manual refinement are complete, maintainers SHOULD create linked implementation tasks that:

- scope each capability increment separately,
- define acceptance checks and sequencing,
- and preserve traceability back to analysis sources.

### 16.9 Canonical Design-Only Workflow Artefacts

Some workflows are intentionally represented first as design-only KB artefacts before the language/runtime can execute them directly. In those cases:

- the canonical artefact SHOULD still be created in Vontology,
- the concept SHOULD keep the final workflow identity if it is expected to become executable later,
- and the artefact SHOULD fail closed as a design document rather than inviting bespoke Python orchestration.

Preferred pattern:

- create the workflow concept in Vontology as the target durable or AI workflow identity,
- attach narrative contract text via `hasContent`/`hasDescription`,
- and, where useful, also type it as `#V#workflow_description_document`.

### 16.10 Worked Example: Jira-GitHub Autofix Loop (`JVNAUTOSCI-1338`)

Canonical concept:

- `#V#jira_github_autofix_loop_workflow`

Current status:

- design-only canonical VWL artefact,
- intended to become a durable workflow once iterator, approval, idempotency, and checkpoint semantics are available.

Normative design requirements:

- discover explicitly labelled Jira issues via bounded JQL;
- map each issue to an allow-listed repository and base branch;
- use guarded internal GitHub MCP methods for branch, PR, or Copilot delegation paths;
- persist per-issue outcomes and run summary under stable context keys;
- comment resulting GitHub metadata back to Jira;
- fail closed on missing auth, allow-list mismatch, missing approval, or missing loop semantics.

This example MUST NOT be implemented as a bespoke Python polling service. Supporting code may add generic VWL capabilities, validators, and telemetry, but the autofix-loop behaviour itself belongs in the Vontology workflow representation.

Reference design document:

- `docs/engineering/jira_github_autofix_loop_workflow.md`

### 16.11 Canonical Fan-Out Example (`JVNAUTOSCI-1339`)

Minimal per-item dispatch shape:

1. `discover_candidates`
   Action writes `candidate_issues`.

2. `dispatch_per_issue`
   Uses `workflow_control.for_each` with:
   - `items_context_key=candidate_issues`
   - `workflow_id=#V#jira_github_autofix_issue_workflow`
   - `item_context_key=current_item`
   - `index_context_key=index`
   - `max_items` set explicitly
   - `success_policy=all_must_succeed` or `allow_partial`

3. `dispatch_per_issue` step metadata MAY also declare:
   - retry policy via `#V#hasWorkflowStepRetryPolicyJson`
   - approval gate via `#V#hasWorkflowStepApprovalGateJson`
   - idempotency policy via `#V#hasWorkflowStepIdempotencyPolicyJson`

4. `summarise_dispatch`
   Reads `iteration_results`, `for_each_success_count`, and `for_each_error_count`.

Blocked-state routing:

- destructive or otherwise approval-gated child-dispatch steps SHOULD declare `on_approval_required` to an explicit blocked or escalation state;
- blocked runs MUST preserve `approval_required`, `approval_state`, and approval-gate event diagnostics rather than silently continuing.

---

## Appendix A: Diagram Specification Pack (Tool-Ready)

This appendix defines a complete diagram contract for tools that generate VWL diagrams.

### A.1 Universal Diagram Envelope

All VWL diagrams MUST conform to this envelope:

```json
{
  "spec_version": "vwl.diagram.v1",
  "diagram_type": "string",
  "title": "string",
  "purpose": "string",
  "source": {
    "workflow_id": "string|null",
    "instance_id": "string|null",
    "schedule_id": "string|null",
    "generated_at_utc": "ISO-8601 string",
    "source_artifacts": ["string"]
  },
  "nodes": [
    {
      "id": "string",
      "kind": "string",
      "label": "string",
      "attrs": {}
    }
  ],
  "edges": [
    {
      "id": "string",
      "from": "node-id",
      "to": "node-id",
      "kind": "string",
      "label": "string|null",
      "attrs": {}
    }
  ],
  "groups": [
    {
      "id": "string",
      "label": "string",
      "node_ids": ["node-id"]
    }
  ],
  "legend": [
    {
      "kind": "string",
      "visual": "string",
      "meaning": "string"
    }
  ],
  "layout": {
    "engine": "dagre|dot|elk",
    "direction": "LR|TB",
    "rank_separation": "number",
    "node_separation": "number"
  },
  "render": {
    "target": "mermaid|graphviz|plantuml|svg-json",
    "theme": "light|dark|auto",
    "font_family": "string"
  },
  "validation": {
    "errors": ["string"],
    "warnings": ["string"]
  }
}
```

Hard requirements:

- Node IDs MUST be unique.
- Edge IDs MUST be unique.
- Edges MUST reference existing node IDs.
- `diagram_type` MUST be one of the types in A.2.

### A.2 Diagram Types

#### A.2.1 `workflow_topology`

Purpose: static workflow graph structure.

Required nodes:

- `workflow`
- `step`

Required edges:

- `hasInitialStep`
- `hasStep`
- `invokesAction` or `invokesWorkflow`
- zero or more control-flow edges: `next`, `on_true`, `on_false`, `on_failure`, `on_unknown`

Node attrs:

- workflow: `workflow_id`, `source`, `initial_state`
- step: `step_id`, `terminal` (bool), `action_id|null`, `invokes_workflow|null`

Edge attrs:

- control-flow edges: `reason`, `condition_kind`

Layout:

- direction `LR`
- initial step pinned left-most.

#### A.2.2 `transition_decision_graph`

Purpose: transition precedence and condition logic per step.

Required nodes:

- `step`
- `condition`
- `target_step`

Required edges:

- `evaluates` (step -> condition)
- `routes_to` (condition -> target_step)

Condition attrs:

- `kind`
- `expected` (where applicable)
- `key` (for `context_flag` and `context_value_equals`)
- `value` (for `context_value_equals`)
- `priority_index`

Validation:

- priority order MUST match compiled order.

#### A.2.3 `context_dataflow`

Purpose: flow from context keys to tool params and back to context keys.

Required nodes:

- `context_key`
- `step`
- `tool_param`
- `tool_output_field`

Required edges:

- `context_to_param`
- `param_to_step`
- `step_to_output_field`
- `output_to_context`

Required attrs:

- mapping concept IDs for semantic mappings.

#### A.2.4 `action_sequence`

Purpose: temporal sequence of action calls inside execution.

Required nodes:

- `state_entry`
- `action_call`
- `state_exit`

Required edges:

- `next_action`
- `transition_taken`

Action attrs:

- `action_id`
- `status`
- `duration_ms`
- `call_id|null`

Layout:

- direction `TB`
- chronological order from top to bottom.

#### A.2.5 `durable_instance_lifecycle`

Purpose: status machine for durable instances.

Required nodes:

- `pending`, `running`, `paused`, `completed`, `failed`, `cancelled`

Required edges:

- `claim` (`pending|paused` -> `running`)
- `complete` (`running` -> `completed`)
- `fail` (`running` -> `failed`)
- `cancel` (`pending|running|paused` -> `cancelled`)
- `retry_reset` (`failed` -> `pending`)
- optional lock-expiry recovery (`running` -> `paused`)

Validation:

- terminal states MUST be marked (`completed`, `failed`, `cancelled`).

#### A.2.6 `schedule_timeline`

Purpose: schedule trigger cadence and next-run logic.

Required nodes:

- `schedule`
- `run_event`
- `instance`

Required edges:

- `triggers`
- `creates_instance`
- `reschedules_to_next_run`

Required attrs:

- `schedule_type`
- `cron_expression|null`
- `interval_seconds|null`
- `run_at|null`
- `next_run_at|null`
- `last_run_at|null`

#### A.2.7 `event_binding_pipeline`

Purpose: event-driven workflow launch path.

Required nodes:

- `event_type`
- `binding`
- `input_mapping`
- `workflow`
- `instance_submission`

Required edges:

- `matched_by_event_type`
- `maps_inputs`
- `submits_workflow`

Validation:

- binding node MUST include `enabled` and `revision`.

#### A.2.8 `runnability_gate_pipeline`

Purpose: verified submission safety gates.

Required nodes:

- `preflight_verification`
- `instance_create`
- `postflight_verification`
- `accept_pending`
- `reject_preflight`
- `reject_postflight_mark_failed`

Required edges:

- `preflight_pass`
- `preflight_fail`
- `postflight_pass`
- `postflight_fail`

Required attrs:

- `conceptual_representation_success`
- `executable_registration_success`
- `runnable_verification_success`
- `error_code|null`

#### A.2.9 `metadata_validation_matrix`

Purpose: per-state metadata obligations and outcomes.

Required nodes:

- `state`
- `metadata_key`
- `validation_result`

Required edges:

- `requires`
- `validated_as`

Validation result attrs:

- `phase` (`pre_action|post_action`)
- `mode` (`enforce|warn|off`)
- `ok`
- `reason_code|null`

#### A.2.10 `runtime_deployment_view`

Purpose: long-term background operation topology.

Required nodes:

- `flask_app_startup`
- `durable_startup`
- `worker`
- `scheduler`
- `mongo_instance_store`
- `mongo_schedule_store`
- `action_registry`
- `definition_loader`

Required edges:

- `starts`
- `polls`
- `claims`
- `executes`
- `reads_writes`

Required attrs:

- startup gating env vars:
  - `VON_DURABLE_WORKFLOWS_ENABLE`
  - `VON_DURABLE_WORKFLOWS_BLOCKING_STARTUP`
  - `VON_DURABLE_WORKER_POLL_INTERVAL`
  - `VON_DURABLE_SCHEDULER_CHECK_INTERVAL`

### A.3 Rendering Conventions

Default style conventions:

- workflow nodes: rounded rectangle.
- step/state nodes: rectangle.
- condition nodes: diamond.
- terminal states: double-border.
- failures/errors: red edge or red node accent.
- unknown routes: amber edge.
- success routes: green edge.
- neutral control-flow: blue/grey edge.

Label conventions:

- Always render canonical IDs in attrs.
- Use human label for node text where available.
- Keep edge labels short (`on_failure`, `writes`, `maps`).

### A.4 Output Targets and Determinism

Diagram tools SHOULD support at least one of:

- Mermaid
- Graphviz DOT
- PlantUML
- SVG with explicit coordinates (`svg-json`)

Determinism requirements:

- stable node order sorted by semantic key (`step_id`, then `action_id`),
- stable edge ordering by `(from, kind, to)`,
- stable layout direction per diagram type default.

### A.5 Diagram Validation Rules

Before rendering, tool MUST run:

- schema validation of envelope,
- reference integrity checks (`edge.from`, `edge.to`),
- required node/edge kind presence checks per diagram type,
- conformance checks for mandatory attrs listed above.

If checks fail:

- output MUST include `validation.errors`,
- renderer MAY still emit partial diagram if explicitly requested.

### A.6 Minimal Example Payload (`workflow_topology`)

```json
{
  "spec_version": "vwl.diagram.v1",
  "diagram_type": "workflow_topology",
  "title": "Example VWL Topology",
  "purpose": "Show workflow and control flow",
  "source": {
    "workflow_id": "#V#example_workflow",
    "instance_id": null,
    "schedule_id": null,
    "generated_at_utc": "2026-03-01T00:00:00Z",
    "source_artifacts": ["vontology_loader.build_workflow_process_graph"]
  },
  "nodes": [
    {"id": "w1", "kind": "workflow", "label": "Example Workflow", "attrs": {"workflow_id": "#V#example_workflow", "initial_state": "#V#step_a", "source": "vontology"}},
    {"id": "sA", "kind": "step", "label": "Step A", "attrs": {"step_id": "#V#step_a", "action_id": "todo_refresh.fetch_gmail", "terminal": false}},
    {"id": "sB", "kind": "step", "label": "Step B", "attrs": {"step_id": "#V#step_b", "action_id": null, "terminal": true}}
  ],
  "edges": [
    {"id": "e1", "from": "w1", "to": "sA", "kind": "hasInitialStep", "label": null, "attrs": {}},
    {"id": "e2", "from": "w1", "to": "sA", "kind": "hasStep", "label": null, "attrs": {}},
    {"id": "e3", "from": "w1", "to": "sB", "kind": "hasStep", "label": null, "attrs": {}},
    {"id": "e4", "from": "sA", "to": "sB", "kind": "next", "label": "next_step", "attrs": {"reason": "next_step", "condition_kind": "always"}}
  ],
  "groups": [],
  "legend": [],
  "layout": {"engine": "dagre", "direction": "LR", "rank_separation": 120, "node_separation": 60},
  "render": {"target": "mermaid", "theme": "light", "font_family": "ui-sans-serif"},
  "validation": {"errors": [], "warnings": []}
}
```

---

End of VWL manual (implementation-aligned snapshot).
