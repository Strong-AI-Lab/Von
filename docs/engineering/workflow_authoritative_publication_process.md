# Workflow Authoritative Publication Process

Date: 2026-02-20  
Related: `JVNAUTOSCI-803`, `JVNAUTOSCI-1217`

## Authority Rule

Workflow definition authority is the Vontology graph.

Runtime registries, traces, and monitor payloads are execution reflections of that authority, not a parallel source of truth.

## Canonical Publication Path

Use the existing canonical pathway in `src/backend/workflows/workflow_concept_authority_service.py`:

1. `bootstrap_workflow_concepts(...)`
2. `publish_canonical_chat_workflow_graphs(...)`

`publish_canonical_chat_workflow_graphs(...)` now applies strict post-publication validation before a workflow is counted as published.

## Strict Validation Gates

`validate_workflow_definition_contract(...)` in `src/backend/workflows/workflow_definition_identity_service.py` enforces:

1. Non-vacuous step contracts.
2. Control-flow completeness for action-bearing states.
3. Transition target resolvability.
4. Input/output/context mapping validity (including unresolved/invalid mapping specs).
5. Action resolvability when supported-action sets are supplied.

If any gate fails, publication is rejected for that workflow with actionable reason codes under:

- `errors_by_workflow_id`
- `validation_failures_by_workflow_id`

## Runtime Gate

Durable instance submission preflight/postflight (`verify_workflow_runnable(...)`) now includes strict contract validation and definition identity payloads.

Unrunnable workflows are rejected with structured errors and no optimistic success status.

## Definition Identity in Introspection

Definition identity payloads (`workflow_definition_identity.v1`) are exposed on:

1. REST definitions list/detail:
   - `/api/workflows/definitions`
   - `/api/workflows/definitions/<workflow_id>`
2. Internal MCP:
   - `workflow_list_definitions`
3. Execution telemetry:
   - workflow episode metadata
   - turn execution runtime snapshots
   - workflow traces (when trace storage is enabled)

Identity payloads include version, source, and deterministic hash fields for traceability and drift detection.

## CI Drift Check

Canonical workflow runtime-vs-authority drift is enforced by tests in:

- `tests/backend/test_workflow_concept_authority_service.py`

The CI check asserts:

1. Canonical workflow registrations are `source=vontology`.
2. Runtime definition hash matches authoritative Vontology-loaded definition hash.

Any mismatch fails CI and must be resolved by updating the Vontology graph/publication path, not by adding fallback code paths.

## Workflow Purity Scoreboard

`JVNAUTOSCI-1524` extends the parity inventory with a structured
`workflow_purity` report that tracks the remaining hybrid authority surface.

The report is attached to the existing `parity_inventory` payloads and logged at
startup under the `workflow_purity` logger key. The counters currently include:

1. `built_in_registration_count`
2. `remaining_python_workflow_family_count`
3. `direct_instance_create_callsite_count`
4. `env_event_binding_count`
5. `legacy_selector_mode_count`
6. `builtin_capability_override_count`
7. `non_vontology_discoverable_workflow_count`

The checked-in CI baseline lives at:

- `tests/backend/fixtures/workflow_purity_baseline.json`

To emit the current report locally:

```powershell
pdm run python scripts/workflow_purity_report.py
```

To refresh the baseline deliberately after an approved convergence change:

```powershell
pdm run python scripts/workflow_purity_report.py --refresh-baseline
```

Refreshing the baseline is a conscious workflow-first migration step. Do it only
when the new counter values reflect intended authority reduction or an approved
re-baselining decision, and commit the updated JSON in the same change as the
code that justified the shift.
