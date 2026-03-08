# JVNAUTOSCI-1399 Bare arXiv URL Integration Test Design

This note defines the authoritative integration-test plan for the user-visible
chat path that starts at `/von/generate` and is expected to drive
workflow-governed scholarly-paper representation from arXiv URLs.

It exists because adjacent coverage already passes while the real route can
still fail before any tool executes, as shown by `JVNAUTOSCI-1394`. It also
takes account of the recent provider-aware dispatch fix in `JVNAUTOSCI-1396`
and the deterministic targeted-lane pytest workflow established by
`JVNAUTOSCI-1400`.

## Linked Work

- `JVNAUTOSCI-1394`: implementation bug for arXiv URL representation turns
- `JVNAUTOSCI-1369`: required-effects and completion-gate groundwork
- `JVNAUTOSCI-1396`: provider-aware workflow-dispatch fix already merged
- `JVNAUTOSCI-1400`: deterministic targeted and aggregate lane strategy;
  validation should follow `docs/engineering/pytest_lane_strategy.md`

## Authoritative Behaviour Decision

Two cases must be separated.

### 1. Bare canonical arXiv URL

Prompt shape:

```text
https://arxiv.org/abs/2510.06248
```

Authoritative behaviour:

- This **is** sufficient permission for `download_paper` and additive scholarly
  representation.
- The turn must enter the workflow/tool pipeline rather than collapsing into a
  read-only or plain-response path.
- The turn must carry a paper representation contract with
  `artefact_source="url"` and must require `download_paper`.
- Successful completion requires real tool execution plus verified scholarly
  representation postconditions.

Why:

- A canonical arXiv abstract URL is evidence-backed, low-risk input for
  additive representation work.
- Minimal imposition means not making the user add extra phrasing such as
  "download", "store", or "represent" when the intended low-risk additive
  action is already clear from the supplied canonical source.
- This is the primary positive mutation path that `JVNAUTOSCI-1394` must fix.

Equivalent positive prompts MAY also include explicit phrasing such as:

```text
https://arxiv.org/abs/2510.06248 represent that paper
```

but that wording is no longer the sole authorising form.

### 2. URL plus explicit denial

Prompt shape:

```text
Do not download or store this: https://arxiv.org/abs/2510.06248
```

Authoritative behaviour:

- Explicit user denial must block mutation.
- The route may still answer read-only, but it must not invoke
  `download_paper`, persist artefacts, or claim successful representation.

This explicit-denial case is the canonical negative regression for
`JVNAUTOSCI-1394`, because it preserves fail-closed behaviour where the user
has provided direct contrary instruction.

## Runtime Boundary To Test

The authoritative entry point is the Flask route:

- `src/backend/server/routes/von_routes.py`
- route: `/von/generate`

The integration test must not stop at direct orchestrator or handler calls.
It should exercise:

1. `/von/generate`
2. real `InternalMCPChatOrchestrator`
3. real `InternalMCPGateway.invoke(...)`
4. real internal `download_paper` tool handler path
5. ontology writes/materialisation
6. completion gate
7. returned `llm_debug`
8. persisted `turn_execution_record`

Reason:

- The observed failure in `JVNAUTOSCI-1394` occurs before tool execution, so
  downstream-only tests are insufficient.
- Route-level `llm_debug` and turn-record persistence are part of the user- and
  operator-visible contract.

## Workflow Authority

For fresh tool-seeking chat turns, the authoritative execution wrapper is:

- `#V#tool_calling_workflow`

Evidence:

- The selector logic in `src/backend/integrations/internal_mcp/orchestrator.py`
  maps `tool_seeking` verdicts to `#V#tool_calling_workflow`.
- Existing selector routing tests assert that tool-seeking turns resolve to the
  tool-calling wrapper rather than directly to a domain-specific workflow ID.

The domain-specific scholarly authority still matters, but one layer down:

- the paper representation contract is sourced from
  `#V#representation_contract_profile_paper`
- the Vontology concept `#V#scholarly_paper_representation_workflow` exists and
  encodes the canonical paper-specific step structure, including
  `resolve_scholarly_authors`

So the current architecture is:

- route-level workflow selection: `#V#tool_calling_workflow`
- domain-specific mutation requirements: paper required-effects contract
- canonical paper process model: `#V#scholarly_paper_representation_workflow`

Important distinction:

- Upload-event routing in
  `src/backend/workflows/durable/file_copy_upload_classification_workflow.py`
  still defaults to `#V#integration_scholarly_paper_representation_workflow`.
- That ID does not currently resolve as a Vontology concept via the current
  concept-access surface.
- `JVNAUTOSCI-1399` should therefore treat the upload-event workflow constant as
  separate from the authoritative fresh-chat execution wrapper.

Implication for the future implementation task:

- `JVNAUTOSCI-1394` should make explicit URL-first chat turns reach the
  tool-calling wrapper with the correct paper contract and tool requirements.
- If the architecture later evolves to direct custom workflow dispatch for this
  domain, the integration assertions can be updated then; 1399 should reflect
  the current real execution path.

## Required-Effects Contract To Assert

The canonical paper profile in
`src/backend/services/representation_contract_vontology_service.py` already
defines:

- `profile_id = "paper"`
- `target_entity_class = "scholarly_paper"`
- `effect_type = "scholarly_representation"`
- `required_tools_by_source["url"] = ["download_paper"]`

The VWL manual also states:

- `url` source for scholarly paper representation must require `download_paper`

Therefore the positive integration test must assert:

- `llm_debug.turn_execution_record.execution.required_effects_contract.schema_version == "required_effects_contract.v1"`
- `...domain_profile_id == "paper"`
- `...target_entity_class == "scholarly_paper"`
- `...artefact_source == "url"`
- `llm_debug.turn_execution_record.required_effects[*].required_tools` includes
  `download_paper`

## Authoritative Telemetry Fields

The test should assert against both route-level debug and the canonical turn
record, because both are user-visible operational interfaces.

### Route-level workflow routing

Assert:

- `body["llm_debug"]["workflow_routing"]["workflow_id"]`
- `body["llm_debug"]["workflow_routing"]["verdict"]`
- `body["llm_debug"]["workflow_routing"]["source"]`

For the bare-URL positive case, the expected selected workflow ID is:

- `#V#tool_calling_workflow`

The paper-specific behaviour should be asserted via the required-effects
contract and tool/postcondition evidence, not by expecting direct selection of
the scholarly workflow concept.

### Canonical turn-execution record

Assert:

- `body["llm_debug"]["turn_execution_record"]["workflow_selection"]["selected_workflow_id"]`
- `...["workflow_selection"]["selector_verdict"]`
- `...["workflow_selection"]["selector_source"]`
- `...["execution"]["required_effects_contract"]`
- `...["completion_gate"]["decision"]`
- `...["completion_gate"]["safe_to_claim_completion"]`
- `...["completion_gate"]["requires_follow_up"]`

### Stage-path telemetry

Assert that
`body["llm_debug"]["turn_execution_diagnostics"]["workflow_stage_path"]["path"]`
contains, at minimum, these stage IDs for the positive case:

- `workflow_dispatch`
- `tool_plan`
- `tool_execute`
- terminal stage `completed`

The regression must fail if the route again collapses into a `screen_backfill`
/ `response_finalising` path with zero tool execution.

### Tool evidence

Assert both:

- `body["llm_debug"]["turn_execution_diagnostics"]["tool_history"]` contains a
  `download_paper` entry
- `body["llm_debug"]["turn_execution_record"]["execution"]["tool_invocations"]`
  contains a successful `download_paper` invocation

The positive test should fail if completion is claimed without that tool
evidence.

## Ontology Postconditions

The authoritative postconditions should match the deterministic arXiv
materialisation helper in
`src/backend/services/arxiv_paper_link_service.py`.

For the positive bare-URL case, assert:

- a stable paper concept exists
- the paper concept is an instance of `#V#scholarly_article`
- the paper is linked to the produced file-copy concept via
  `#V#propositional_information_thing_has_computer_file`
- the paper has canonical name text including the arXiv ID and title
- the paper has a summary in `hasDescription`
- the paper has `#V#authored_by` links to author concepts
- the paper has `#V#about` links to topic concepts when categories are present

Author handling decision:

- Author representation is **not** a separate workflow for this test.
- It is an effect inside the scholarly-paper representation flow.
- The chat workflow concept explicitly includes `resolve_scholarly_authors`, and
  the deterministic materialisation helper resolves or creates person concepts
  and writes `#V#authored_by` relations.

So the integration test should assert authorship as a postcondition of the same
paper-representation turn, not as dispatch to a separate author workflow.

## Recommended Harness Design

### Flask app

Use a real Flask app with `von_bp` registered at `/von`.

### Authentication/session

Use an authenticated session so the write path is permitted to register file
copies and scholarly representation:

- `user_concept_id`
- `organisation_concept_id` if needed by the route setup
- deterministic `session_id`

### Orchestrator

Use the real `InternalMCPChatOrchestrator`.

Patch only external seams:

- selector/planner/final-response LLM calls return deterministic scripted
  responses
- any network-facing arXiv/PDF retrieval dependency
- blob-store backend

Do not stub out the orchestrator’s routing, contract, or completion logic.

### Gateway

Use a real `InternalMCPGateway` with the default catalogue and transport.

Patch external dependencies under the `download_paper` path rather than
replacing the gateway itself. This keeps the regression on the real
`gateway.invoke(...) -> tool handler -> tool payload` boundary.

### Metadata fixture

Use a deterministic arXiv metadata fixture for `2510.06248` with:

- title
- summary
- authors
- categories

The existing upload integration test for `2502.14996` is the right model for
this kind of deterministic metadata fixture.

## Fixture and Database Strategy

The canonical strategy is a per-test cloned database, not reuse of the shared
`test_von_db`.

Recommended setup:

1. clone the ontology slice from `test_von_db` into a fresh per-test clone DB
2. point `VON_DB_NAME` at the clone
3. preflight-assert that the clone contains:
   - `#V#scholarly_paper_representation_workflow`
   - `#V#representation_contract_profile_paper`
   - required core types/predicates for scholarly materialisation

Why clone instead of global/shared fixtures:

- the route writes more than ontology concepts, including history and execution
  projections
- dropping one temporary DB is more deterministic than trying to surgically
  delete concepts after partial failure
- this avoids visibility confusion around user/org-scoped versus global objects

## Rollback and Failure Handling

The canonical rollback is:

1. create a uniquely named clone DB for the test
2. run the full route-level scenario there
3. always drop the clone DB in `finally`
4. clean any fake/local blob-store artefacts used by the test harness

This is preferable to relation-by-relation cleanup because the route can create:

- ontology concepts and text relations
- chat history rows
- interaction sessions
- turn execution records
- workflow runtime artefacts

Within the test body, still record created IDs for diagnostics:

- paper concept ID
- file-copy concept ID
- author concept IDs
- workflow instance IDs if any appear
- request ID

These IDs should be emitted in failure messages to support debugging, but the
authoritative cleanup mechanism should remain clone-DB deletion.

## Proposed Targeted Test Matrix

### A. Positive regression case

`test_von_generate_bare_arxiv_url_auto_represents_paper`

Prompt:

```text
https://arxiv.org/abs/2510.06248
```

Assert:

- selected workflow is `#V#tool_calling_workflow`
- stage path includes dispatch, plan, execute, completed
- `download_paper` executed through the gateway/tool path
- required-effects contract says `artefact_source="url"`
- required-effects contract domain is `paper`
- completion gate is `completed` and `safe_to_claim_completion=True`
- scholarly ontology postconditions exist

This is the regression that should fail on the broken `JVNAUTOSCI-1394`
behaviour.

### B. Negative safety case

`test_von_generate_bare_arxiv_url_with_explicit_denial_stays_non_mutating`

Prompt:

```text
Do not download or store this: https://arxiv.org/abs/2510.06248
```

Assert:

- no `download_paper` invocation
- no completion claim that implies successful representation
- the turn remains non-mutating despite the canonical URL being present

This protects the explicit-denial boundary.

## Validation Scope

Per `JVNAUTOSCI-1400` and `docs/engineering/pytest_lane_strategy.md`,
validation for the eventual implementation should stay targeted:

- the new route-level arXiv integration test module
- adjacent required-effects/turn-record tests only if the implementation
  changes those shared pathways

Do not make the fix depend on a monolithic full-suite pytest run.

## Implementation Sequence For JVNAUTOSCI-1394

1. Add the positive bare-URL route-level integration test.
2. Add the explicit-denial negative regression.
3. Fix the URL-first routing/tool-requirement gap until the positive test passes.
4. Only then tighten any telemetry assertions that depend on
   `JVNAUTOSCI-1397`.

## Open Architectural Note

The current upload classification constant
`#V#integration_scholarly_paper_representation_workflow` appears to be code-only
from the present Vontology surface, while the chat workflow
`#V#scholarly_paper_representation_workflow` is a real Vontology concept with a
published process graph.

That discrepancy does not block `JVNAUTOSCI-1399`, but the eventual
implementation and cleanup work should keep the two paths intentionally aligned
or intentionally separate rather than assuming they are already the same thing.
