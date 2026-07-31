# Von Workflow Language (VWL) Manual

- **Kind:** Workflow-language manual
- **Lifecycle:** Active
- **Authority:** Canonical reference for VWL vocabulary, authoring rules, and
  runtime interfaces; consult the sections relevant to the workflow change
- **Live-authority boundary:** Live Vontology artefacts govern individual
  workflow definitions; current code, tests, and telemetry govern observed
  runtime behaviour
- **Created:** 2026-05-03
- **Last substantive content update:** 2026-07-25
- **Last reviewed:** 2026-07-25
- **Audience:** Human engineers and AI agents

## 1. Purpose and Scope

Von Workflow Language (VWL) is the executable workflow language used by Von for LLM-era agent behaviour orchestration. VWL is not a standalone parser language. It is a graph-native language represented in Vontology concepts and predicates, then compiled into runtime workflow definitions.

VWL workflow creation and modification are implementation activities, not
documentation-only activities. Once a capability slice has selected VWL as the
right authority surface, engineers and AI agents SHOULD create or update the
workflow directly in Vontology as part of the task rather than deferring its
authored behaviour to later bespoke code.

This manual is a reference, not a cover-to-cover prerequisite for every
workflow change. Sections 1--15 define current language and runtime semantics.
Section 16 records retired planning material; Appendix A contains optional
diagram contracts.

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

These keywords apply only after a capability has deliberately selected the
named VWL feature. They do not require every user job to use a workflow,
approval gate, idempotency policy, completion contract, or represented risk
class. A malformed policy that was explicitly selected may fail that route;
the existence of a policy mechanism does not justify selecting it, blocking
independently authorised alternatives, or treating the manual as a universal
controller design.

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
- For a capability that selects VWL as its authority surface, preferred
  implementation is to materialise the authored behaviour in Vontology and add
  the supporting code/tooling needed for execution, validation, and telemetry.
- Backend code SHOULD primarily supply reusable actions, validators, loaders,
  and telemetry for those workflows rather than embedding their task-specific
  orchestration in code.
- Repo-side workflow/template/prompt files are non-authoritative by default. They MAY exist only as migration seeds, generated snapshots, test fixtures, or exports unless an explicitly approved exception says otherwise.
- Replacing bespoke Python workflow builders with repo-side declarative files is
  not, by itself, enough when Vontology has been selected as the live authority.
- Workflow-related Jira work SHOULD record the capability slice, selected
  authority surfaces, and applicable validation tier from `AGENTS.md`.

### 2.1 Workflow Authority Model

When a workflow is selected as represented production authority, its canonical
definition, reusable authoring metadata, routing metadata, and governed prompt
metadata SHOULD be authored in Vontology-native graph/text/policy structures.

Normative authority rule:

- supporting code MAY compile, validate, publish, or export these artefacts;
- repo-side files MUST NOT be treated as canonical authored source for a
  capability whose selected live authority is Vontology;
- if a production path depends on repo-side files for workflow authority, that path is source-authority drift and should be treated as contract debt, not normal conformance.

### 2.2 Repo-side workflow artefacts

Repo-side workflow, template, and prompt artefacts may be migration seeds,
exports, generated review snapshots, or test fixtures. They are not live
production authority when a capability has deliberately selected a Vontology
workflow as that authority. Their role must be explicit, and live activation
and read-back must make the governing source unambiguous.

This distinction does not require a workflow, a repo seed, or a Vontology
materialisation for a capability that is better served by a direct tool,
function, model call, or other approved surface. Current seed services, bundle
paths, incident commands, and domain publication examples belong in code,
runbooks, or version history rather than in the language manual.

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
- Mechanically exact actions MAY remain explicit deterministic or control
  executions when that is the simplest adequate path. Existing execution mode
  is not proof that the surrounding stage or policy is necessary.

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
- `suppress_event_workflow_launches`: static boolean control for a
  workflow-owned mutation whose event-driven fan-out would duplicate the
  enclosing workflow's represented work. The control is not forwarded to the
  MCP tool, does not grant mutation authority or bypass access/write
  guardrails, and applies only for the request-local gateway invocation. When
  true, event-workflow launch and workflow-discovery cache invalidation are
  suppressed for that invocation. Workflows MUST use it only when the affected
  mutation cannot change workflow routing authority and the enclosing workflow
  performs explicit canonical read-back.

Runtime semantics:

- workflow context bindings inside `tool_arguments` are resolved by the normal action-input resolver before invocation;
- authenticated namespace context is propagated into the MCP payload as `namespace` when available;
- the action returns the workflow-visible MCP payload under `result` and `mcp_result`, with `mcp_tool`, `mcp_requested_tool`, `mcp_resolved_tool`, and `mcp_duration_ms` diagnostics;
- the action reports `event_workflow_launch_suppression_requested` so traces
  distinguish an authored suppression request from ordinary tool execution;
- internal MCP advisory budgets are telemetry only: a handler that completes
  before its hard deadline remains successful even when the advisory budget was
  exceeded;
- a hard-deadline expiry is a terminal failed action for the current turn. The
  action preserves the typed timeout under `mcp_result` and the bounded
  transport facts under `mcp_transport`, including execution identity,
  queue/handler/transport timing, deadline, timeout phase, and the explicit
  `discard_from_turn` late-result policy;
- the transport applies the same remaining hard-deadline budget as a PyMongo
  client-side operation timeout around the handler. Nested Vontology database
  operations therefore release their bounded isolation worker at the deadline
  instead of continuing under a succession of independent driver timeouts. A
  small bounded part of that budget is reserved for caught database exceptions
  to unwind into the typed terminal outcome. Non-database handlers remain
  bounded by the fixed worker pool and cooperative cancellation scope;
- a late handler completion cannot rewrite the workflow action outcome. Reads
  expose represented recovery affordances such as bounded retry or alternate
  path selection; writes report an indeterminate mutation outcome and require
  state inspection before any retry;
- when the invoked tool has a represented tool-evidence projection, `result` and `mcp_result` carry that compact projected payload and projection telemetry instead of raw source-specific bulk data;
- `tool_output_context_mappings` should map fields from `result.<field>` or `mcp_result.<field>` into workflow context for downstream steps and subworkflows.

Validation semantics:

- `tool_name` MUST be statically declared, or the step MUST fail Workflow Studio contract validation;
- the named tool MUST resolve to a registered internal MCP method;
- gateway input-schema rejection is returned as a failed action with a bounded
  `mcp_result` (`error_code=schema_validation_failed`,
  `error_type=invalid_arguments`, `retryable=false`) so represented failure
  transitions can inspect and recover without exposing the raw validation
  message or user values;
- output-schema rejection is distinguished as
  `error_code=output_schema_validation_failed` and
  `error_type=invalid_tool_output`; do not treat a broken tool response as an
  argument error;
- read-only tools may be published without write metadata;
- tools whose effects can exceed the standing delegated and recovery envelope
  MUST expose enough capability/effect policy for the runtime to enforce that
  ceiling; bounded, observable, reliably recoverable effects should not acquire
  an approval workflow merely because they are labelled write or destructive;
- domain sequencing, extraction, filtering, and user-facing policy MUST remain in VWL, prompt, KB, or Vontology artefacts rather than in the generic action implementation.

### 3.4b Wrapper Workflow Boundaries for External and Low-Level Tools

When repeated source-specific acquisition, fallback, verification, or read-back
policy needs independent authoring and reuse, a wrapper workflow may own it.
Downstream workflows SHOULD consume that wrapper's represented outputs when the
extra layer demonstrably improves the capability. A direct tool call remains a
valid simpler path when it can satisfy the user job within the chosen
capability and recovery envelope.

Normative rules:

- a wrapper workflow is an optional represented policy boundary, not the source
  of the caller's authority and not a mandatory layer around every external
  tool;
- external MCP/tool failures SHOULD expose enough structured evidence for
  recovery; reusable fallback sequencing may remain in the wrapper when that
  is why the wrapper exists;
- do not hide fallback policy inside the low-level MCP tool merely because that
  tool is where the failure was observed;
- collection-processing workflows MAY fan out through the wrapper when per-item
  isolation or reuse helps the user job;
- downstream workflows SHOULD use durable artefact identifiers produced by a
  wrapper when later reading, linking, or verification requires stable
  reference;
- downstream workflows MAY invoke the low-level tool directly when that is the
  smallest adequate path and does not evade a concrete capability ceiling or
  necessary reusable policy;
- step output contracts such as `writes_context_keys` MUST be declared only on
  steps that actually produce the context key on that execution path. Decision,
  pass-through, or routing steps MAY map optional pre-existing values forward,
  but MUST NOT advertise those values as newly produced artefacts.

This allows repeated policy to remain inspectable without turning every
transient tool defect or third-party quirk into another compulsory orchestration
layer. Python remains suitable for execution, telemetry, validation, and
generic tool bridging.

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

Prefer an existing reusable authoring primitive when it fits. Add a new
primitive only when a concrete capability demonstrates reuse and the primitive
is simpler than direct canonical authoring; do not block useful authoring while
constructing a generic meta-workflow.

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
- `direct_equivalent_capability_names`: optional ordered list of registered
  capabilities that are represented as producing the same material outcome and
  evidence as the workflow with lower orchestration cost

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

Ordinary-turn capability selection treats direct calls, small compositions,
and represented workflows as plans on one bounded frontier. The adaptive model
owns semantic adequacy: it may prefer a workflow when composition, verification,
durability, governance, or recovery is material, but representedness alone is
not a quality signal. Actor-visible registered tools declared by a matched
workflow are exposed as plausible simpler component plans without claiming that
one component is equivalent to the whole workflow.

Workflow component projection includes actor-visible MCP tools named by
represented step actions and by represented LLM-step tool policy
(`required_tools`, `conditional_required_tools`, and `allowed_tools`). Engine
actions such as `llm.action`, workflow control, generic workflow MCP dispatch,
and subworkflow invocation are not themselves projected as direct MCP
alternatives. Required-tool metadata retains its stricter execution-contract
meaning; the broader component set is only a set of plausible simpler plans.
Component candidates inherit evidence from the matched workflow and are kept
visible ahead of unrelated direct retrieval hits so pagination cannot erase
the cheaper path. Persisted workflow-capability manifests carry the routing
projection producer schema; a schema change invalidates and rebuilds the
namespace instead of silently serving the previous metadata shape.

Schema discovery remains an optional ordinary capability, not a compulsory
stage. When the catalogue surfaces it for represented-knowledge work, the model
may use it to retrieve predicates, types, inverse directions, or reified
relationship shapes before a relation-bearing read.
Direction-neutral relation capability hints are similarly local to the
retrieved capability: when the exact represented predicate and argument
direction are unknown, they advise incidence or relation reads across any
argument position before a complete negative is claimed. This is not a global
conversation-stage requirement and does not activate on unrelated uses of
words such as “list” or “find”.

Mechanical dominance is deliberately narrower. A read-only workflow may be
removed from the active frontier only when its authoritative routing profile
explicitly names an actor-visible read-only direct equivalent. The workflow
remains addressable by exact name and visible in the complete catalogue for
inspection. Do not use `direct_equivalent_capability_names` for an approximate
shortcut, a component that omits material evidence, or merely because a
workflow is slow; those cases remain semantic decisions and evaluation inputs.

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
- a parity or provenance check MAY expose drift when a represented workflow is
  expected to govern a capability, but it is not a universal purity gate and
  must not block an independently authorised direct path;
- selector prompts are represented when their independent governance is
  material. Their absence does not require inventing a selector stage or
  suppressing a simpler capable route.

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

### 7.1 Conversation-turn policy is out of scope

Thinking-card rendering, workflow discovery, turn routing, completion policy,
and multi-turn repair are consumers of VWL, not VWL language semantics. Their
current behaviour belongs in the affected code, represented artefacts, and
architecture-neutral outcome evaluation. Do not add controller stages or
incident-specific UI contracts to this manual.

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

### 8.1 Progress projection metadata

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

Current compatibility semantics for policies a workflow has actually selected:

These clauses describe how the present loader/runtime interprets existing
metadata. They do not require a replacement controller or a new workflow to use
templates, approval, mutation classes, checkpoints, completion gates,
terminal-success contracts, or typed route maps.

- invalid policy payloads MUST fail workflow loading with deterministic error codes;
- retry and idempotency policies currently apply only to single-action states;
- when an approval gate has been deliberately selected for a concrete
  consequential effect, it MUST not silently pass without its required approval
  context;
- no workflow is required to add an approval gate solely because an operation
  has a generic write, send, mutation, or delete label;
- an approval-gated state SHOULD preserve a useful alternate or blocked route
  when the selected gate is not satisfied;
- a mutation-authority policy, when selected, is one optional way to confine the
  maximum effect of a step. Actor and environment capability ceilings remain
  outside the model; a global mutation-class taxonomy is not required;
- record enough decision and effect evidence to explain a material restriction
  or recovery. Do not require a universal guardrail event lattice for every
  mutation;
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

### 8.3 Terminal outcome receipt metadata

A workflow MAY select `terminal_outcome_receipt.v1` when a typed record of
outcome, evidence, committed effects, outstanding obligations, recovery
affordances, or provenance materially helps the user job. The runtime may
validate, redact, persist, project, and expose such a receipt.

The schema does not require a separate critic, completion gate, recovery stage,
or promotion workflow. A model, direct tool path, function, or workflow may
produce the relevant evidence. A missing receipt is an error only when the
selected workflow contract requires it; it must never erase committed effects
or safe recovery opportunities.

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
- `workflow_control.pause_at_checkpoint`

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

### 10.4 For Each Fan-Out

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
- `stop_on_error` (default `false`; requires sequential execution and
  `success_policy=all_must_succeed`)

Runtime semantics:

- items are processed in deterministic source order;
- each item executes the declared child workflow in an isolated child context;
- the child context receives the bound item and index keys;
- per-item results are returned in `iteration_results`;
- aggregate counts are returned in `for_each_success_count`, `for_each_error_count`, and `for_each_partial_success`;
- when `stop_on_error=true` in sequential mode, iteration stops after the first failed child and reports the attempted prefix plus the unattempted count;
- `stop_on_error=true` with `success_policy=allow_partial` fails before child lookup because partial success is incompatible with fail-fast outcome semantics;
- `stop_on_error=true` with effective concurrency greater than one fails before child execution rather than silently weakening fail-fast semantics;
- `all_must_succeed` returns failure when any child execution fails;
- `allow_partial` returns success while preserving structured failure details.

### 10.5 Context Set

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

### 10.6 Durable Checkpoint Pause

`workflow_control.pause_at_checkpoint` is the represented, cooperative
interruption primitive for a durable workflow. Put it in the state after whose
successful actions execution may stop. The executor selects the state's normal
transition, then atomically persists the successor state, accumulated context,
manual pause hold, and typed pause receipt under the live worker claim. This
means resume continues the same instance from the named successor checkpoint;
the interrupted state and its side effects are not replayed.

Optional input:

- `reason_code`: a non-empty represented reason of at most 160 characters.

Runtime semantics:

- direct or synchronous execution fails closed because it has no fenced
  durable claim;
- launch inputs and restored workflow context cannot forge pause requests or
  manager-owned pause/resume receipts;
- a manually held checkpoint is not worker-claimable until
  `workflow_resume_instance` releases it;
- resume changes the same instance from `paused` to `pending`, preserving its
  checkpoint state and step index, and emits a typed receipt linked to the
  pause receipt;
- status/read-back surfaces expose the checkpoint, receipt digests, and
  available recovery operations, but never a raw worker claim token;
- the legacy HTTP `pause` endpoint is an immediate administrative status
  control and is not evidence of deterministic checkpoint interruption. Use
  the represented action and explicit resume for replay or certification.

The v1 checkpoint attestation binds saved state. If pre-pause model-produced
authority output is resumed under a new worker claim, the executor correctly
marks exact-authority eligibility as degraded until producer lineage across
claims is itself represented. Workflows that need exact interruption evidence
should therefore place this action before the authority-producing LLM step.

### 10.7 Variable Declarations

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

### 10.8 Domain workflows are not language semantics

Domain-specific routing, representation, acquisition, completion, and
regression contracts do not belong in this language manual. Live Vontology
artefacts, current code and tool schemas, task-specific evaluation, and Jira
decision records govern those capabilities. Git history retains the retired
paper, person, company, meeting, file-copy, and self-improvement contract prose.

Do not add domain IDs, exact tool sequences, incident-specific failure codes,
or named regression files here. Add only reusable VWL syntax or runtime
semantics proved necessary by a capability.

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

## 12. Instance submission integrity

The current durable-instance runtime uses:

- preflight runnable verification,
- instance create,
- postflight runnable verification,
- fail-closed rejection or failure marking if checks do not pass.

This prevents false "started/running" claims for non-runnable workflows.
It does not require a replacement controller, direct tool path, or non-durable
workflow to adopt this submission pipeline.

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

### 14.5 Testing workflow tools

Testing and experiment tools are ordinary optional standard-library
capabilities. Their live schemas and behaviour are defined by the current tool
catalogue and code. This manual does not require a theory artefact, promotion
gate, meeting fixture, or exact experimental choreography for every
evaluation. Select only the smallest evidence and recovery surface justified by
the claim.

### 14.6 Prompt-contract interoperability

A prompt-bearing workflow step MAY link a KB-authored prompt with
`#V#workflow_step_uses_llm_prompt`; the accepted legacy aliases are
`#V#uses_prompt` and `#V#hasPromptTemplate`.

Prompt metadata may include name, description, argument hint, agent/model
preferences, allowed tools, scope, variables, source, and tool-resolution
priority. Step-local defaults may be supplied with `__prompt_defaults` and a
validation policy with `__prompt_validation_policy`. The current merge order is
prompt metadata, first resolved agent profile, then step-local defaults;
list-valued fields use the first populated source rather than an implicit union.

The compiled step exposes the resulting `prompt_contract`. A deliberately
selected contract may require prompt text, tools, profiles, or structured
output and may reject that route when its own declared validation fails. This
feature does not require every LLM call to have a represented prompt contract,
fixed stage, or fail-closed fallback.

### 14.7 Markdown skill interoperability

Compatible `SKILL.md` artefacts may be executed through
`skill.execute_markdown` or transpiled into a reusable VWL definition. The
supported frontmatter requires `name` and `description`, with optional
`argument-hint`, `user-invokable`, and `disable-model-invocation`; names use
lowercase kebab-case and match the parent skill directory.

The adapter supports instruction-body execution and deferred resource loading,
not arbitrary script execution outside the workflow runtime. Preserve source
scope and file/resource provenance. Skill discovery should load lightweight
name/description metadata first, the instruction body on relevance or explicit
invocation, and additional resources only on demand.

This interoperation is an optional import/execution primitive. It does not make
VWL the mandatory controller for every skill or require checkpoint and
completion-gate choreography.

## 15. Current loader compatibility

A definition intended for the current VWL loader must satisfy the structural
items it actually uses:

- it has a resolvable workflow concept,
- step graph is loadable and initial step is determinable,
- step invocation targets are unambiguous,
- transition conditions are valid,
- break/continue and fork/join control contracts are valid,
- metadata contracts are syntactically valid,
- required mappings are parseable,
- discovered actions are runnable in current registry,
- workflow typing satisfies canonical workflow authority rules, including subtype satisfaction for required workflow types,
- the chosen submission path accepts it.

Optional policy families are not conformance requirements when absent. This
compatibility list does not establish that VWL, the durable runtime, or any
particular metadata family is the right architecture for a new capability.

## 16. Retired planning material

The former March 2026 planning baseline and Jira-GitHub worked examples mixed
proposals, current inventory, domain policy, and language semantics. They are
retained in git history, not as active VWL requirements. Promote an individual
reusable syntax or runtime contract into Sections 3-15 only after live
implementation and a capability demonstrate its need.

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
