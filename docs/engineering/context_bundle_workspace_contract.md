# Context Bundle Workspace Contract

**Status**: implementation note  
**Primary Jira tasks**: `JVNAUTOSCI-254`, `JVNAUTOSCI-1563`, `JVNAUTOSCI-1564`, `JVNAUTOSCI-1565`  
**Date**: 2026-04-05

## 1. Purpose

This note records the bounded reconstructed-workspace contract implemented for
context bundles and reconstructed dossiers.

The intent is to keep long-horizon work inspectable, resumable, and bounded.
This is not a license to accumulate raw history into one ever-growing prompt.

## 2. Authoritative Artefacts

Canonical Vontology artefacts used by this contract:

- `#V#context_bundle`
- `#V#workflow_context_bundle`
- `#V#concept_context_bundle`
- `#V#context_facet`
- `#V#context_dossier`
- `#V#workflow_report_revision`
- `#V#evidence_receipt`

Canonical predicates:

- `#V#has_context_bundle`
- `#V#context_bundle_extends`
- `#V#has_context_facet`
- `#V#has_context_dossier`
- `#V#has_report_revision`
- `#V#cites_evidence_receipt`

Hybrid dossier state should reuse `#V#testing_theory` and theory-local
assertion semantics rather than inventing a disconnected branch store.

## 3. Standard Workspace Keys

The reconstructed workspace shape is standardised around these keys:

- `workspace_question`
- `workspace_task`
- `effective_context_bundle_ids`
- `effective_context_facet_ids`
- `context_dossier_id`
- `context_dossier`
- `report_revision_id`
- `report_revision`
- `immediate_context`
- `open_questions`
- `evidence_receipts`
- `interaction_budget`
- `termination_status`
- `search_history`
- `workspace_reconstruction_telemetry`

These keys are intended to be reusable across repo-document, paper-analysis,
ontology-refinement, and chat-compatible long-horizon workflows.

## 4. Telemetry Contract

`workspace_reconstruction_telemetry` should remain compact and machine-readable.
The current implementation records:

- reconstruction round number
- compression decisions
- guardrail or fail-closed events
- promotion attempts
- branch transitions
- dropped-context counts
- bundle-resolution diagnostics
- update timestamp

Telemetry is part of the operational contract. It is not optional polish.

## 5. Boundedness Rules

The workspace contract should prefer:

- explicit open-question lists over raw transcript replay
- evidence receipts over rediscovering the same inputs each round
- bounded immediate context over mono-context accumulation
- report revision history over destructive overwrite
- explicit branch records for theory-local or counterfactual work

When the runtime prunes or compresses context, the dropped counts and decisions
should remain visible in telemetry.

## 6. Evaluation Surface

The implementation includes an ablation-ready seed benchmark that compares:

- `bundles_only`
- `bundles_plus_dossier_reconstruction`
- `bundles_plus_dossier_reconstruction_plus_report_revision`

It currently includes representative cases for:

- repo-document analysis
- paper analysis
- ontology refinement
- chat-compatible long-horizon follow-up

The evaluation output should retain representative replay cases for later
experience-maintenance and learning-loop work.
