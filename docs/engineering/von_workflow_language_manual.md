# Von Workflow Language (VWL) Manual

Status: Draft (current implementation-aligned)
Last updated: 2026-03-03 (Pacific/Auckland)
Audience: Human engineers and AI agents

## 1. Purpose and Scope

Von Workflow Language (VWL) is the executable workflow language used by Von for LLM-era agent behaviour orchestration. VWL is not a standalone parser language. It is a graph-native language represented in Vontology concepts and predicates, then compiled into runtime workflow definitions.

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
- `src/backend/workflows/durable/worker.py`
- `src/backend/workflows/durable/scheduler.py`
- `src/backend/workflows/durable/workflow_instance_submission_service.py`
- `src/backend/server/utils_flask.py`
- `src/backend/integrations/internal_mcp/catalogue.py`

## 3. VWL Ontology Vocabulary

### 3.1 Core Type Concepts

Canonical concepts (confirmed via Vontology MCP):

- `#V#ai_workflow`: structured machine-interpretable workflow plan type.
- `#V#durable_workflow`: workflow subtype with persistent execution/checkpointing semantics.
- `#V#workflow_step`: type for workflow states/steps.
- `#V#workflow_context_key`: type for deterministic context key symbols.
- `#V#workflow_stage`: type for model-policy stage scoping.
- `#V#workflow_model_policy`: type for stage-aware model selection policy.

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
- `nextStep`
- `onTrueNextStep`, `onFalseNextStep`, `onFailureNextStep`, `onUnknownNextStep`
- `onBreakNextStep`, `onContinueNextStep`
- `hasPrecondition`, `hasEffect`
- `readsVariable`, `writesVariable`
- `hasInputMap`
- `workflowStepMapsContextKeyToToolParam`
- `workflowStepWritesContextKey`
- `workflowStepMapsToolOutputFieldToContextKey`

## 4. VWL Program Model

A VWL program is a workflow concept graph:

- Workflow node (concept ID, usually an instance of `#V#ai_workflow` or `#V#durable_workflow`).
- Step nodes linked via `hasStep`.
- One initial step via `hasInitialStep` (or inferred first step if absent).
- Per-step invocation target:
  - tool/action (`invokesAction` or `workflow_step_invokes_tool`), or
  - subworkflow (`invokesWorkflow`).
- Per-step control flow links:
  - `next`
  - `on_true`, `on_false`
  - `on_failure`, `on_unknown`.
  - `on_break`, `on_continue`.
- Optional metadata and mapping contracts.

## 5. Condition Language (Transition Expressions)

Transition condition specs are JSON-like objects normalised by runtime code.

Supported kinds:

- `always`
- `context_flag`
- `context_value_equals`
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
   - resolve control-flow edges,
   - capture metadata and mapping references.
3. Compile into runtime state machine (`load_workflow_definition_from_vontology`):
   - create `WorkflowStateSpec` objects,
   - create `WorkflowActionInvocation` records,
   - compile transition condition specs,
   - attach metadata contracts.

Important deterministic ordering:

- Generated branch precedence for implicit branch links is:
  - `on_failure`
  - `on_unknown`
  - `on_break`
  - `on_continue`
  - `on_true`/`on_false`
  - `next_step`

`on_failure` and `on_unknown` compile to context-flag conditions (`last_action_failed`, `last_action_unknown`).
`on_break`/`on_continue` compile to control-signal conditions (`last_control_signal == break|continue`).

## 7. Execution Semantics

Runtime execution model (`WorkflowExecutor`):

1. Enter current state.
2. Run pre-action metadata validation (unless validation mode disables enforcement).
3. Execute actions in state order.
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

### 7.1 Thinking-Card Progress Terminal Precedence (JVNAUTOSCI-1316)

For chat progress payloads consumed by the thinking card:

- Terminal status MUST be authoritative over liveness when rendering run-state badges.
- Terminal detection MUST check both `status` and `orchestrator_status` (and treat values such as `completed|done`, `failed|error`, `cancelled|aborted|canceled`, `terminated` as terminal).
- If any terminal status is present, the badge MUST render terminal state immediately (never remain `Active`/`Waiting`/`Stalled` because of `liveness_state`).
- `liveness_state` remains informational for metadata (for example "Last activity"), not authoritative for terminality.
- Progress polling SHOULD stop once terminality is detected (except explicit user-triggered refresh flows).

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
- `subworkflow_contract`
- `invokes_workflow`
- `loop_scope_id`
- `fork_id`
- `join_fork_id`

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

### 10.4 File-Copy Upload Routing Workflows (JVNAUTOSCI-1309)

Built-in workflow IDs:

- `#V#file_copy_upload_classification_workflow`
- `#V#file_copy_upload_handler_workflow`
- `#V#file_copy_interpretation_workflow` (baseline safe path)

Classification outputs (persisted and propagated to the handler) include:

- `route_key` (`scholarly|cv|business_card|interpret|noop`)
- `route_mode` (`specialised|interpret|fail_closed|noop`)
- `route_confidence`, `route_reasons`, `target_workflow_id`, `target_workflow_available`
- `unsupported_specialised_route` and `unsupported_route_reason` when a mutation-class route was selected but no specialised workflow is available.

Safety semantics:

- Low-confidence mutation routes MUST fail closed (`route_mode=fail_closed`) rather than invoking mutation workflows.
- When specialised CV/business-card workflows are not available, classification MUST emit explicit unsupported-route diagnostics (`unsupported_specialised_route=true`) and route to non-mutation handling (`interpret` or `noop` per fallback policy).
- Handler outcome persistence (`#V#has_file_copy_upload_route_outcome_json`) records selected/effective route mode, success, reasons, and unsupported-route diagnostics for post-run inspection.

Event-launch semantics:

- `file_copy.uploaded` launches use a single selected workflow strategy (`#V#file_copy_upload_handler_workflow` by default) to avoid duplicate uncontrolled launches.
- Default event bindings are bootstrapped idempotently; conflicting bindings are not overwritten.

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
- `enabled`
- revision/actor metadata.

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

### 14.2 Instance Control

- `workflow_create_instance`
- `workflow_list_instances`
- `workflow_get_instance`
- `workflow_cancel_instance`
- `workflow_retry_instance`

### 14.3 Event Binding Control

- `workflow_bind_event`
- `workflow_list_event_bindings`

### 14.4 Schedule Control

- `workflow_create_schedule`
- `workflow_list_schedules`
- `workflow_get_schedule`
- `workflow_set_schedule_enabled`
- `workflow_delete_schedule`
- `workflow_trigger_schedule`

These tools are the default operational control surface for VWL runtime behaviour.

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

- VWL SHOULD support durable plan-state artefacts for multi-hour workflows.
- Recommended plan state includes step status (`pending|in_progress|blocked|done`), checkpoint timestamps, and resumable cursors.
- Long-running workflows SHOULD expose periodic summary hooks and completion gates that verify declared deliverables before terminal success.

### 16.4 Gate and Policy Semantics

- Workflow definitions SHOULD support explicit approval and escalation gates between analysis and mutation stages.
- LLM/tool steps SHOULD allow typed output contracts with deterministic validation failure branches.
- Retry semantics SHOULD include bounded retries, backoff policy, and terminal-failure routing.

### 16.5 Prompt Metadata Contract Extensions

For prompt-bearing workflow or prompt concepts, VWL planning SHOULD support metadata fields equivalent to modern prompt-file ecosystems.

Candidate predicate set (planning):

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

Conflict handling for prompt metadata SHOULD be deterministic, with documented precedence (for example prompt-level over agent defaults).

### 16.6 External SKILL Interoperability

VWL planning supports a dual interoperability direction for external SKILL artefacts:

1. direct execution of supported SKILL formats through a constrained adapter runtime, and
2. deterministic import/transpile of SKILL artefacts into VWL workflow definitions.

Both modes SHOULD enforce:

- capability and side-effect allowlists,
- provenance capture from source artefact to runtime instance,
- and canonical-task traceability across external projections.

For compatibility with the VS Code Agent Skills model, SKILL interoperability SHOULD also support:

- canonical `SKILL.md` YAML frontmatter ingestion and validation,
- required fields: `name`, `description`,
- optional fields: `argument-hint`, `user-invokable`, `disable-model-invocation`,
- directory/name consistency validation (`name` MUST match parent directory),
- and source-location provenance for project and personal skill search roots.

Candidate predicate set (planning) for skill concepts:

- `#V#hasSkillName`
- `#V#hasSkillDescription`
- `#V#hasSkillArgumentHint`
- `#V#isUserInvokable`
- `#V#disablesModelInvocation`
- `#V#hasSkillSourceScope` (`project|personal|extension|shared`)
- `#V#hasSkillDiscoveryLocation`

Skill loading SHOULD preserve progressive disclosure semantics:

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
