# JVNAUTOSCI-1104 terminal outcome receipt acceptance proof

Date: 2026-07-12

Scope: JVNAUTOSCI-1104 under JVNAUTOSCI-2575

Authority: represented VWL/Vontology critic workflow and prompt; Python is limited to schema validation, persistence, redaction, read-back projection, telemetry, and benchmark aggregation.

## Acceptance result

JVNAUTOSCI-1104's terminal outcome receipt capability is accepted on the tested path. A represented critic produces the semantic outcome, causal explanation, evidence, effect, obligation, retry, recovery, and learning fields. The same validated receipt is projected without semantic reclassification through the Turn Execution Record, tool observation ledger, live progress, benchmark, and dashboard.

The proof deliberately includes both success and non-success. A turn does not need to succeed to be correctly handled; an interrupted or partial turn must retain a typed cause, evidence, remaining obligations, retryability, recovery affordances, and a learning candidate where appropriate.

## Real-path evidence

| Path | Request ID | Result |
| --- | --- | --- |
| Represented-selector positive replay | `jvnautosci-2422-6637b2af-7501-4fc0-b73f-3c838d8a9869` | AgentTest verdict `pass`; visible answer `PONG`; selected `#V#chat_narration_workflow`; task `completed`; valid `verified_success`; `cause_code=null`; no remaining obligations. |
| Interrupted/partial replay | `jvnautosci-2422-b94fb1da-bb4f-4e12-9a94-b8b49fe405ca` | Cancellation during critic execution persisted a valid represented `verified_partial` receipt with causal execution stage, typed cause, no claimed durable effects, remaining obligations, `after_external_change` retryability, recovery affordances, and a learning candidate. |
| Post-recovery reconciliation regression | `jvnautosci-2422-4c8f2ab1-3f06-41a5-a130-80b1ce5c6c7d` | Exposed the stale pre-recovery receipt. The represented conversation workflow now returns through `reconcile_recovery_outcome` and re-runs the represented critic after a recovery answer or follow-up. |

For the positive replay, the visible answer, task status, selected-workflow trace, top-level receipt, persisted Turn Execution Record receipt, and tool-ledger projection agree. Terminal live-progress read-back now reports the same `verified_success` receipt from `live_progress.persisted_turn_execution_record` when the bounded progress snapshot predates final persistence.

## Acceptance matrix

1. **Canonical cross-surface facts:** verified across the live-progress projection, persisted record, selected-workflow trace, tool ledger, benchmark, and dashboard.
2. **Real gateway coverage:** read/retrieval, file-copy ingestion, additive task mutation, and idempotent label synchronisation pass through `InternalMCPGateway.invoke()`.
3. **Fault matrix:** impacted tests cover invalid arguments, authentication/permission denial, unavailable tools/services, timeouts, empty or contradictory evidence, partial writes, wrong targets, read-back mismatch, and interruption/cancellation.
4. **Typed non-success:** the live benchmark reports 100% typed-cause coverage and 100% recovery-affordance coverage for the two represented non-success receipts in the filtered replay corpus.
5. **No text-based success:** success requires validated receipt evidence; response text or mere tool presence is insufficient.
6. **Duplicate/idempotent delivery:** idempotent reuse remains an explicit committed effect and authoritative postcondition evidence, with gateway regression coverage.
7. **Redaction:** receipt validation and projection retain redaction status and exclude secret-bearing diagnostic values.
8. **No primary English matching:** outcome semantics come from the represented critic; Python validates shape and projects represented fields.
9. **Missing evidence:** the represented default is an evidence-bearing `inconclusive` receipt, not success. Older records without receipts remain explicitly counted as missing.
10. **Opportunity preservation:** non-success receipts preserve inspect, retry, alternate-workflow, external-change, and represented-learning paths; the opportunity-preservation regression suite passes.
11. **AgentTest agreement:** the positive represented-selector replay has agreeing user answer, task completion, trace, record, ledger, and receipt.
12. **Durable documentation:** the workflow-language manual and real-path replay/telemetry guide document receipt authority, fields, validation, and cross-surface reconciliation.

## Validation results

- Impacted receipt, workflow, opportunity-preservation, critic, observability, benchmark, ledger, live-progress, required-effects, selected-workflow-output, and orchestrator suites: **220 passed**.
- Real internal gateway suites: **24 passed**.
- Focused represented-recovery benchmark regression: **2 passed** (including the existing hesitancy gateway case).
- Ruff on all modified Python and test files: **passed**.
- `git diff --check`: **passed**.
- Live filtered benchmark after the final projection change: 5 scanned; 0 false successes; 4 valid represented receipts; outcomes 2 `verified_success` and 2 `verified_partial`; non-success typed-cause coverage 100%; recovery-affordance coverage 100%; retry-signal coverage 100%.
- Live filtered dashboard: false-success card 0%/pass; represented retry guardrail pass; no active regression.

## Delimitation

This closes the canonical receipt and evidence-reconciliation capability in 1104. It does not claim that every historical Turn Execution Record has a receipt, that every workflow succeeds, or that the wider general-agent certification corpus is complete. Historical missing evidence remains visible rather than being reclassified. Broader cross-workflow certification and policy improvement belong to JVNAUTOSCI-2577 and the parent epic JVNAUTOSCI-2575.
