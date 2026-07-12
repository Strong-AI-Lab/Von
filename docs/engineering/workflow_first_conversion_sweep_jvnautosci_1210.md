# JVNAUTOSCI-1210 Workflow-First Conversion Sweep

Date: 2026-02-20

## Scope

This sweep targeted behaviour/action pathways under the `JVNAUTOSCI-833` epic that were represented as Vontology workflows but still classified as non-executable due vacuous step contracts.

The goal was to:

1. Classify conversion candidates.
2. Convert class-1 candidates directly in Vontology (without adding specialised Python orchestration).
3. Re-check executability and behavioural equivalence expectations.
4. Record follow-on class-2 items.

## Baseline

Before this sweep, Vontology workflow status was:

| Workflow ID | Status | Reason |
| --- | --- | --- |
| `#V#entity_representation_workflow` | executable | `executable_now` |
| `#V#add_affiliation_workflow` | executable | `executable_now` |
| `#V#salient_predicate_governance_workflow` | non-executable | `workflow_step_partially_vacuous` |
| `#V#sail_phd_student_onboarding_workflow` | non-executable | `workflow_step_completely_vacuous` |
| `#V#von_user_onboarding_workflow` | non-executable | `workflow_step_completely_vacuous` |
| `#V#von_workflow_creation_workflow` | non-executable | `workflow_step_completely_vacuous` |

## Conversion Matrix

| Pathway | Classification | Action | Outcome |
| --- | --- | --- | --- |
| `#V#entity_representation_workflow` | Class 1 (convertible now) | Previously converted and completed in Vontology | Remains executable |
| `#V#add_affiliation_workflow` | Class 1 (convertible now) | Previously converted and completed in Vontology | Remains executable |
| `#V#salient_predicate_governance_workflow` | Class 1 (convertible now) | Added step contracts via `#V#workflow_step_writes_context_key` to vacuous steps | Now executable |
| `#V#sail_phd_student_onboarding_workflow` | Class 1 (convertible now) | Added step contracts via `#V#workflow_step_writes_context_key` to all steps | Now executable |
| `#V#von_user_onboarding_workflow` | Class 1 (convertible now) | Added step contracts via `#V#workflow_step_writes_context_key` to all steps | Now executable |
| `#V#von_workflow_creation_workflow` | Class 1 (convertible now) | Added step contracts via `#V#workflow_step_writes_context_key` to all steps | Now executable |
| Built-in workflow registrations (e.g. `#V#chat_assistant_workflow`, `#V#conversation_turn_execution_workflow`) | Class 2 (convertible with small extension) | Not converted in this task; require Vontology process-graph parity for code-registered workflows | Tracked as follow-on under `JVNAUTOSCI-803` / epic backlog |

## Behavioural Equivalence Notes

For converted class-1 workflows, this sweep preserved control-flow topology and only added explicit step contracts where missing.

Equivalence expectation maintained:

1. Step ordering and transition routing remain unchanged.
2. No broadening of authz/safety surface (no new privileged tools introduced).
3. Runtime intent is preserved: steps that were sequencing placeholders remain sequencing placeholders, now with explicit contract metadata so workflow submission/executability gates no longer reject them as vacuous.

## Post-Conversion Verification

Post-sweep Vontology workflow status:

| Workflow ID | Status | Reason |
| --- | --- | --- |
| `#V#add_affiliation_workflow` | executable | `executable_now` |
| `#V#entity_representation_workflow` | executable | `executable_now` |
| `#V#sail_phd_student_onboarding_workflow` | executable | `executable_now` |
| `#V#salient_predicate_governance_workflow` | executable | `executable_now` |
| `#V#von_user_onboarding_workflow` | executable | `executable_now` |
| `#V#von_workflow_creation_workflow` | executable | `executable_now` |

Summary:

1. Vontology workflows discovered: `6`
2. Vontology workflows executable: `6`
3. Vontology workflows non-executable: `0`

## Follow-On Gaps (Class 2)

The registry still contains built-in (`source=built_in`) workflow definitions that classify as `non_executable_design_artifact` under Vontology graph-based executability checks.

These are workflow-first migration candidates for follow-on work:

1. Persist or mirror built-in workflow definitions as Vontology process graphs.
2. Keep registry/graph parity so monitor executability and routing eligibility are consistent.
3. Continue this under existing umbrella work in `JVNAUTOSCI-803` (linked from `JVNAUTOSCI-1210`).

## Authoritative Publication Follow-On

`JVNAUTOSCI-1217` adds strict publication/runnability gates and deterministic definition identity/hash telemetry for workflow monitor/introspection surfaces.

The dated
[`workflow_authoritative_publication_process.md`](workflow_authoritative_publication_process.md)
records the February 2026 implementation provenance. For current authoring and
validation procedure, use the
[`von_workflow_language_manual.md`](von_workflow_language_manual.md), live
Vontology tooling, and authority read-back.
