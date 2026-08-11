# Workflow Authoritative Publication Process

> **Document status: Dated implementation note from 20 February 2026.** The
> Vontology-authority invariant remains applicable. The named bootstrap and
> publication functions below are historical implementation anchors, not the
> exclusive current authoring procedure. Follow the current VWL manual, live
> Vontology tooling, and authority read-back for new work.

- **Kind:** Implementation note with a retained authority invariant
- **Lifecycle:** Frozen
- **Authority:** Advisory implementation detail; the authority invariant is
  governed by `AGENTS.md`

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

For explicit Vontology-authored workflow families such as paper/talk
representation, use `publish_canonical_chat_workflow_graphs(...)` with explicit
publication specs/definitions/purposes rather than creating temporary
`source="built_in"` runtime registrations. The runtime registry must consume the
published Vontology authority; it must not materialise those families on the
production registry path.

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

Parity enforcement defaults to `fail`, not `warn`, for executable-registry and
authority defects: registry-only or Vontology-only workflow IDs, incomplete
represented process graphs, missing authority concepts or types, and purity
counters that identify a workflow source or policy violation. The deferred
parity policy marks those defects failed and raises within its worker unless a
developer explicitly relaxes `VON_WORKFLOW_PARITY_ENFORCEMENT` for bounded
local diagnostics. The current deferred worker catches and records that
failure; it does not retroactively make the already-running server unready.

Two kinds of engineering debt are reported but are not runtime-readiness
failures:

- `synthesized_launch_contract_count`, including the exact sorted workflow IDs;
- `monolith_line_count_*` source-size ratchets.

An otherwise healthy registry with only those findings completes parity work as
`completed_with_findings` and emits a `workflow_parity_advisory` warning. This
keeps the debt visible without making a runnable workflow registry unavailable
because of code-maintenance metrics. The standalone purity gate remains strict
and returns non-zero for the authority and policy counters above; callers may
still choose a warn-only integration policy.

The runtime registry must also keep bootstrap-only workflow families separate
from production registration authority. Python definitions that remain for
bootstrap/test support, such as the file-copy family during convergence, may be
used to publish or repair authoritative Vontology graphs, but they must not be
counted as discoverable production registrations.

Capability indexing now also fails closed for routing: the workflow capability
index should only index `source=vontology` workflows with non-empty
authoritative narrative text. Non-authoritative registrations and textless
workflows must be skipped rather than receiving guessed fallback routing prose.

The checked-in CI baseline lives at:

- `tests/backend/fixtures/workflow_purity_baseline.json`

The checked-in baseline is repository-observable and may be generated without
a populated runtime registry. It therefore does not record an artificial zero
for registry-dependent measurements such as synthesised launch contracts, nor
does it use them as a historical ratchet. A complete live non-zero observation
is reported separately as present advisory debt, with exact workflow IDs; an
incomplete or empty observation is merely incomparable. Neither case is
misreported as a static-baseline regression.

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
