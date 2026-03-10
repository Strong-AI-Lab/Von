# Von Workflow Language (VWL) Manual

Status: Draft (current implementation-aligned)
Last updated: 2026-03-10 (Pacific/Auckland)
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
- `src/backend/workflows/durable/worker.py`
- `src/backend/workflows/durable/scheduler.py`
- `src/backend/workflows/durable/workflow_instance_submission_service.py`
- `src/backend/server/utils_flask.py`
- `src/backend/integrations/internal_mcp/catalogue.py`

Authoring policy:

- Vontology workflow graphs, mappings, schedules, and event bindings are first-class implementation artefacts.
- When behaviour can be expressed in VWL, preferred implementation is to materialise it in Vontology and then add only the supporting code/tooling needed for execution, validation, and telemetry.
- Backend code SHOULD primarily supply reusable actions, validators, loaders, and telemetry for workflows, rather than embedding task-specific orchestration that VWL could already express.

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

## 4. VWL Program Model

A VWL program is a workflow concept graph:

- Workflow node (concept ID, usually an instance of `#V#ai_workflow` or `#V#durable_workflow`).
- Step nodes linked via `hasStep`.
- One initial step via `hasInitialStep` (or inferred first step if absent).
- Per-step invocation target:
  - tool/action (`invokesAction` or `workflow_step_invokes_tool`), or
  - subworkflow (`invokesWorkflow`).
- Optional per-step prompt contract link:
  - `workflow_step_uses_llm_prompt` (legacy aliases accepted for compatibility).
- Per-step control flow links:
  - `next`
  - `on_true`, `on_false`
  - `on_failure`, `on_unknown`.
  - `on_approval_required`.
  - `on_break`, `on_continue`.
- Optional metadata and mapping contracts.

## 5. Condition Language (Transition Expressions)

Transition condition specs are JSON-like objects normalised by runtime code.

Supported kinds:

- `always`
- `context_flag`
- `context_value_equals`
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
  - `on_approval_required`
  - `on_break`
  - `on_continue`
  - `on_true`/`on_false`
  - `next_step`

`on_failure` and `on_unknown` compile to context-flag conditions (`last_action_failed`, `last_action_unknown`).
`on_approval_required` compiles to a context-flag condition (`approval_required == true`).
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

### 7.2 Thinking-Card Workflow Visibility Contract (JVNAUTOSCI-1378)

For live chat progress payloads used by workflow-aware thinking-card rendering:

- the payload SHOULD include `workflow_stage_path` derived from the canonical conversation-turn stage model, not just raw internal event/status labels;
- `workflow_stage_path.workflow_id` SHOULD represent the mapped execution-stage consensus when the observed stage path yields one unambiguous workflow, and SHOULD fall back to the selected-route hint only when the stage path itself carries no workflow membership;
- `workflow_stage_path.observed_workflow_ids` SHOULD expose the workflow IDs actually observed in the mapped stage path so route selection (`selected_workflow_id`) and execution-stage membership can be compared without ambiguity;
- workflow discovery SHOULD emit an explicit completion payload even when zero workflows match, so the UI can render a visible "no applicable workflow found" step rather than silently omitting discovery outcome;
- live workflow-dispatch progress SHOULD expose `selected_workflow_id` and SHOULD expose a human-readable `selected_workflow_name` when available;
- selector metadata such as `workflow_selector_verdict` and `workflow_selector_source` SHOULD be preserved in the live payload so the UI can explain why a workflow route was chosen;
- tool history and tool success/failure/pending counters SHOULD be derived from concrete tool lifecycle events (`tool_call_start` / terminal tool events) rather than route-selection metadata such as `workflow_task`;
- thinking-card step labels SHOULD prefer canonical workflow-stage labels (for example `Workflow discovery`, `Workflow dispatch`, `Plan tool calls`) over transport/internal labels such as `orchestrator_start`.

### 7.3 Completion-Gate Terminal Semantics (JVNAUTOSCI-1380)

For workflow-governed conversation turns:

- completion safety MUST be determined by completion-gate outputs, not by whether a response text was produced;
- if `completion_gate_safe_to_claim_completion=false`, the orchestrator MUST NOT surface the turn as `completed`;
- unresolved required effects or inconclusive mutation/representation verification MUST produce `follow_up_required` or `failed`, with explicit blocking effect IDs and failure codes preserved in diagnostics;
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
- `subworkflow_contract`
- `invokes_workflow`
- `retry_policy`
- `approval_gate`
- `idempotency_policy`
- `checkpoint_policy`
- `loop_scope_id`
- `fork_id`
- `join_fork_id`

### 8.1 Runtime Policy Metadata

Canonical workflow-step runtime policy payloads are stored as singleton text relations on the step concept:

- `#V#hasWorkflowStepRetryPolicyJson`
- `#V#hasWorkflowStepApprovalGateJson`
- `#V#hasWorkflowStepIdempotencyPolicyJson`
- `#V#hasWorkflowStepCheckpointPolicyJson`

Workflow-level long-horizon policy payloads are stored as singleton text relations on the workflow concept:

- `#V#hasWorkflowPlanStatePolicyJson`
- `#V#hasWorkflowCompletionGateJson`

Current schema versions:

- `workflow_step_retry_policy.v1`
- `workflow_step_approval_gate.v1`
- `workflow_step_idempotency_policy.v1`
- `workflow_step_checkpoint_policy.v1`
- `workflow_plan_state_policy.v1`
- `workflow_completion_gate.v1`

Normative semantics:

- invalid policy payloads MUST fail workflow loading with deterministic error codes;
- retry and idempotency policies currently apply only to single-action states;
- approval gates MUST fail closed when the required approval context key is absent or falsey;
- destructive or otherwise high-risk approval-gated states SHOULD provide an explicit `on_approval_required` route to a blocked terminal or escalation state;
- checkpoint policies update the shared runtime plan-state artefact rather than introducing workflow-specific Python persistence logic;
- plan-state items support `pending|in_progress|blocked|done` statuses, bounded checkpoint history, periodic summary snapshots, resumable cursor snapshots, and resume telemetry;
- completion gates MUST fail closed before terminal success when required plan items or required context keys are not satisfied.

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
- `workflow_control.for_each`

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
- Operators should resolve obsolete `file_copy.uploaded` routes through workflow binding governance (`workflow_list_event_bindings`, `workflow_set_event_binding_enabled`, `workflow_delete_event_binding`) rather than by adding Python-side routing switches.

Current implementation caveat (JVNAUTOSCI-1415):

- the upload-classification scholarly default still points at `#V#integration_scholarly_paper_representation_workflow` in code (`src/backend/workflows/durable/file_copy_upload_classification_workflow.py`);
- that ID is not currently a Vontology concept, while `#V#scholarly_paper_representation_workflow` does exist as a Vontology workflow concept;
- until `JVNAUTOSCI-1415` aligns these identities, treat the upload scholarly target as an implementation split rather than a clean single-source workflow authority.

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

- `file_copy` source: required tool set MUST include `interpret_file_copy`.
- `url` source (including arXiv URLs/IDs): required tool set MUST include `download_paper`.
- `mixed` source: required tool set SHOULD include both `download_paper` and `interpret_file_copy`.

Intent boundary for URL inputs:

- A canonical arXiv abstract URL or ID on its own **does** authorise low-risk additive `download_paper` execution and scholarly representation, unless the user explicitly denies mutation.
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

- `download_paper` and `finalise_cached_paper` SHOULD attempt scholarly representation materialisation for authenticated file-copy registrations.
- Tool responses SHOULD expose `scholarly_representation` diagnostics (`attempted`, `verified`, `paper_concept_id`, `metadata_source`, `metadata_available`, and explicit error/fallback fields when not verified).

Turn-execution gate expectation:

- Tool invocations whose payload explicitly reports `success=false` MUST be treated as failed execution for required-effect evaluation and completion gating.

Current implementation caveat (JVNAUTOSCI-1415):

- `#V#scholarly_paper_representation_workflow` currently exists as a loadable Vontology workflow concept, but its present graph is legacy/incomplete: most steps invoke `workflow_creation.emit_marker`, with only author resolution represented as a domain-specific action;
- the current arXiv and upload pathways therefore still depend primarily on tool-handler materialisation (`download_paper`, `finalise_cached_paper`, `interpret_file_copy`) plus representation-contract and completion-gate semantics, not yet on a fully expressive domain workflow family;
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

- `#V#rumination_workflow` relation-completion dispatch reads its knowledge-acquisition policy from the linked profile concept.

Normative policy semantics:

- workflows MUST retrieve existing context/evidence first before asking the user for new input;
- low-risk defaults MAY be auto-applied only when the linked profile policy allows it and confidence/evidence thresholds are met;
- high-risk or ambiguous changes MUST require explicit user confirmation;
- relation-completion runs SHOULD ask at most one focused clarification question per run when machine-side evidence is insufficient;
- if the linked acquisition profile cannot be resolved, the workflow MUST fail closed with explicit `knowledge_acquisition_profile_unavailable` diagnostics rather than falling back to ad hoc prompting.

Minimal-imposition mutation semantics:

- human interruption is an exception path, not the default operational posture for ordinary Von workflows;
- low-risk additive Vontology writes SHOULD default-allow when the workflow has evidence-backed inputs and there is no explicit user denial;
- non-delete mutations of existing state MAY proceed when the user has clearly requested them;
- destructive mutations MUST branch through explicit `on_approval_required` confirmation/escalation states rather than relying on blanket pre-emptive hesitation;
- approval gates are targeted risk controls for destructive/high-risk actions, not a default doctrine for all mutations.

Persistence semantics:

- uncertain or provisional relation proposals SHOULD be stored through the canonical uncertain-assertion pathway, not legacy side channels;
- acquisition workflows SHOULD preserve provenance (`source`, interaction identifier, evidence count, confidence score) for later promotion/audit.

### 10.12 Representation and Acquisition Profiles as Workflow Contracts (JVNAUTOSCI-1380)

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
- unavailable tools or agent profiles follow the declared validation policy (`warn` or `fail`),
- stable merged prompt state is carried in workflow-state metadata as `prompt_contract`,
- runtime diagnostics are attached to action inputs as `__prompt_resolution_diagnostics`,
- fail-policy violations reject workflow loading with deterministic `workflow_prompt_contract_invalid` errors.

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
