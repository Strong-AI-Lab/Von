# Workflow Self-Authoring and Intent Generation: Gap Analysis & Plan

**Status**: Draft
**Date**: 2026-04-05
**Related Epics**: `JVNAUTOSCI-833`, `JVNAUTOSCI-964`, `JVNAUTOSCI-1586`
**Related Tasks**: `JVNAUTOSCI-1531`, `JVNAUTOSCI-1654`, `JVNAUTOSCI-1574`

## 1. Purpose
This document examines the extent to which Von meets its goal of driving user interaction behaviours primarily through workflows that are self-authored following collaborative problem-solving sessions. It outlines the met goals, identifies the architectural gaps, and provides a concrete implementation plan.

## 2. Current State Assessment

### 2.1 Met Goals (The Substrate)
Following the recent workflow-first convergence (`JVNAUTOSCI-1517` through `1529`), Von has a robust foundation:
- **Vontology-Authoritative Execution**: The execution and routing of behaviours are now driven by VWL definitions and workflow instances, avoiding Python-based heuristics.
- **Durable Runtime**: Checkpoints, subworkflows, and verification gates are operational (`durable_workflow_system_design.md`).
- **Gap Discovery Seed**: `workflow_gap_recovery_workflow.py` provides a precedent for identifying when a required workflow is missing.
- **Testing-Workflow Substrate Exists**: The system is no longer starting from design documents alone. `testing_workflow_contracts.py`, `testing_theory_service.py`, `experiment_run_service.py`, MCP tool surfaces, and the current testing seed bundles already provide first-class IDs, schema versions, and partial execution pathways for `#V#ephemeral_theory`, `#V#experiment_spec`, `#V#experiment_run`, `#V#promotion_gate_workflow`, and related testing artefacts.
- **Workflow Studio Baseline Exists**: The Workflow Studio is no longer absent. There is now an independent Studio surface with catalogue/detail views, topology/decision/dataflow/operations inspection, and preview-first authoritative editing support. The remaining gap is candidate self-authoring integration and approval/provenance handling, not basic visualisation.

### 2.2 Unmet Goals (The Generation & Validation Gap)
While Von can run workflows, it cannot yet reliably and safely *author* them autonomously based on user interactions.
- **Missing Intent-to-Workflow Pipeline**: As noted in `JVNAUTOSCI-1654`, there is an explicit gap in generating workflows from user intent. The system lacks the prompt-programs and structured pathways to translate a collaborative chat session into a VWL definition.
- **Incomplete Safe Mutation Boundary**: The testing-theory substrate now exists, but the exact authoritative mutation-boundary story for theory-bounded workflow execution is still incomplete. The remaining issue is not "invent ephemeral theories" but proving and hardening theory-local write isolation, promotion semantics, and garbage collection across real MCP and workflow paths.
- **Missing Candidate Proposal Lifecycle**: Self-authored workflows need a first-class inspectable proposal/review state. Draft workflow lifecycle metadata exists, but the self-authoring plan still needs an explicit authoritative story for `pending_review`, `approved`, `rejected`, provenance, and rollback/supersession rather than leaving proposal state implicit in UI or workflow-local context.
- **Opaque Side-Effect Policy for Generated Workflows**: A generated workflow must not be allowed to synthesise arbitrary executable side effects. The current plan needs an explicit validator/allowlist story for dangerous tools, external mutations, schedules, bindings, and other high-impact actions.
- **Workflow Studio Integration Gap**: The missing UI work is not the baseline Studio itself. The gap is that session-derived candidate workflows are not yet presented as mixed-initiative proposals with validation evidence, provenance, and publication gating layered onto the existing Studio path.

## 3. Concrete Implementation Plan

To achieve full collaborative self-authoring, we must execute the following structured sequence, prioritising safe execution correctness and inspectability.

### Phase 1: Safe Sandboxing (Testing Workflows)
*Epic: `JVNAUTOSCI-964` / `JVNAUTOSCI-1531`*
Before Von can author workflows, it must be able to test its own drafts.
1. Complete and harden the existing `#V#ephemeral_theory`, `#V#experiment_spec`, and `#V#experiment_run` substrate rather than creating a parallel second testing system.
2. Develop the `capability_test_execution_workflow` to execute candidate self-authored workflows against theory-local data using the current testing-theory and experiment-run surfaces.
3. Ensure that outputs from these runs produce structured `experiment_run.v1` evidence rather than mutating canonical Vontology.
4. Verify the mutation boundary on the exact production pathways that matter: workflow execution, MCP-mediated writes, promotion helpers, and theory garbage collection.

### Phase 2: Intent Generation and Repair Surface
*Epic: `JVNAUTOSCI-833`*
1. **Workflow Generation Prompt Programs**: Author Vontology-native prompt concepts designed to take session dossiers and user intents, outputting compliant VWL graphs.
2. **Automated Verification**: Route the generated VWL through Phase 1's capability tests.
3. **Repair Loop**: If the candidate workflow fails (e.g., prohibited mutations, missing dataflow mappings), trigger a secondary repair workflow to ingest the structured failure evidence and patch the candidate VWL.
4. **Reuse Existing Generic Authoring Surfaces**: This phase should build on `#V#workflow_repair_or_create_workflow`, `#V#workflow_authoring_repair_workflow`, `#V#von_workflow_creation_workflow`, and Vontology-authored template/profile metadata rather than creating a bespoke prompt-to-graph bypass path.
5. **Prompt-Evaluation Discipline**: Generation prompts are operational policy. They need replay cases, baseline prompt lineage, promotion thresholds, and rollback conditions, not only "works on one example" acceptance.

### Phase 3: Inspectable Collaboration (Workflow Studio)
*Epic: `JVNAUTOSCI-1586` / related design tasks `JVNAUTOSCI-1587`, `JVNAUTOSCI-1588`*
The user must remain the final arbiter of newly authored behaviour policies.
1. Reuse the existing Workflow Studio baseline (topology, dataflow, conditional logic, preview-first editing) rather than rebuilding it.
2. Introduce the **Mixed-Initiative Proposal Layer**: Instead of silent application, Von presents the session-derived candidate workflow as a diff/preview in the Workflow Studio with test evidence and provenance.
3. Allow the user to collaboratively tweak edge conditions, context mappings, and schedule bindings in the Studio UI.
4. Keep proposal state authoritative and inspectable. The Studio must not become the hidden source of approval state or draft lifecycle policy.

### Phase 4: Promotion and Publication
*Epic: `JVNAUTOSCI-833` / `JVNAUTOSCI-1531`*
1. Extend the existing `promotion_gate_workflow` and workflow publication lifecycle machinery so a validated, user-approved candidate workflow can move from ephemeral testing state to canonical Vontology.
2. Bind the new workflow to the relevant intent triggers, routing profile metadata, and discovery/publication surfaces so the collaboratively solved behaviour becomes operational only when explicitly published.
3. Ensure promotion has a first-class rollback/supersession story. Self-authored workflows should be demotable or replaceable without leaving unclear routing residue.

## 4. Jira Task Index and Interpretive Notes

### 4.1 Task Index

| Key | Phase | Summary | Key Interpretation |
|-----|-------|---------|-------------------|
| JVNAUTOSCI-1720 | 1 — Sandboxing | Harden ephemeral theory isolation and prove mutation boundary on production paths | **Harden existing substrate**, not build from zero. `testing_theory_service.py` etc. already exist. |
| JVNAUTOSCI-1721 | 1 — Sandboxing | Harden experiment_spec and experiment_run evidence schema on production paths | **Verify and extend** existing `experiment_run_service.py` schema for repair-loop consumption. |
| JVNAUTOSCI-1722 | 1 — Sandboxing | Build capability_test_execution_workflow for sandboxed workflow testing | New VWL workflow composing the existing theory/experiment substrate. |
| JVNAUTOSCI-1723 | 1 — Sandboxing | Add mutation and side-effect scope enforcement for theory-bounded workflow execution | **Gate all side effects** (schedules, bindings, Jira/GitHub, MCP mutations), not just Vontology graph writes. Safety-critical. |
| JVNAUTOSCI-1724 | 2 — Generation | Author intent-to-VWL generation prompt programs as Vontology text relations | Prompts are policy. Must include replay corpus, lineage, promotion threshold, rollback condition. |
| JVNAUTOSCI-1725 | 2 — Generation | Build intent-to-workflow generation and verification pipeline | **Must reuse** `#V#workflow_repair_or_create_workflow`, `#V#von_workflow_creation_workflow`, etc. No bespoke prompt→graph bypass. |
| JVNAUTOSCI-1726 | 2 — Generation | Build VWL repair loop workflow for failed candidate generation | Extends existing `#V#workflow_authoring_repair_workflow`. Bounded retry with user escalation. |
| JVNAUTOSCI-1727 | 3 — Collaboration | Workflow Studio: integrate candidate workflow diff/preview with evidence and provenance | **Integrate into shipped Studio**, not rebuild. The baseline already exists. |
| JVNAUTOSCI-1728 | 3 — Collaboration | Mixed-initiative proposal layer for self-authored workflows | Extend `workflow_publication_lifecycle.v1` with `pending_review`/`rejected`/`superseded`. Approval state must be authoritative, not UI-local. |
| JVNAUTOSCI-1729 | 3 — Collaboration | Collaborative workflow parameter editing in proposal UI | User tweaks feed back to candidate VWL; re-validation required. |
| JVNAUTOSCI-1730 | 4 — Promotion | Extend promotion_gate_workflow and publication lifecycle with rollback/supersession support | **Extend existing** `#V#promotion_gate_workflow` and lifecycle model. Rollback/supersession are first-class. |
| JVNAUTOSCI-1731 | 4 — Promotion | Bind promoted workflows to intent triggers and update routing metadata | Include routing-eligibility checks; demotion removes eligibility cleanly. |
| JVNAUTOSCI-1732 | Cross-cutting | Add real-path performance tracking, degradation safeguards, and demotion for self-authored workflows | Nearest-real-path acceptance, replay corpus, selector regression detection. |

### 4.2 General Interpretive Rules

- Tasks that reference `#V#ephemeral_theory`, `#V#experiment_spec`, `#V#experiment_run`, or `promotion_gate_workflow` should be read as **complete, harden, and prove the current substrate**, not "start from zero".
- Tasks that describe Workflow Studio work should be read as **candidate self-authoring integration into the shipped Studio**, not as a second implementation of listing/topology/preview/editing.
- Intent-generation tasks must explicitly reuse the existing generic workflow-authoring primitives and template/profile pathways. A parallel raw prompt-to-graph path would be workflow-authority drift.
- Proposal/review state must be first-class and authoritative. Extend `workflow_publication_lifecycle.v1` rather than using UI-local or ad-hoc code-side state.
- Self-authored workflows need an explicit authoring-policy validator for dangerous or high-impact actions (schedules, bindings, external MCP mutations). Structural VWL validity alone is not sufficient.
- Promotion and degradation tasks must include rollback, supersession, and post-promotion routing eligibility checks rather than treating publication as a one-way event.

## 5. From-Scratch Codebase Audit (2026-04-06)

A fresh analysis of the actual codebase and Vontology state was performed to cross-check the above plan. The audit confirmed all 13 tasks (1720–1732) target genuine gaps, and uncovered six additional gaps not previously tracked.

### 5.1 Audit Method

- Deep exploration of execution engine, testing substrate, authoring workflows, side-effect boundaries, promotion lifecycle, and frontend Studio.
- MCP `concept_exists` checks against all 10 key concept IDs referenced by the self-authoring substrate.
- Vontology tree dump (154 concepts) to verify what actually exists.
- Code-level tracing of bootstrap, materialisation, and validation pathways.

### 5.2 Key Codebase Finding: Current Vontology State vs Implemented Bootstrap

The MCP audit correctly showed that the currently queried Vontology surface did **not** have the expected self-authoring concepts materialised at audit time. However, that should not be read as "these workflows or services do not exist in code".

The codebase already contains substantial bootstrap and publication paths:

- `_start_durable_workflow_system()` in `utils_flask.py` runs workflow-family bootstrap functions including `bootstrap_canonical_testing_workflows()`.
- Startup also runs `bootstrap_workflow_concept_identities()` in `workflow_concept_authority_service.py` to repair workflow identity/type drift before strict parity checks.
- Repo seed bundles already define canonical workflow concepts such as `#V#promotion_gate_workflow`, `#V#workflow_repair_or_create_workflow`, `#V#workflow_authoring_repair_workflow`, `#V#von_workflow_creation_workflow`, and `#V#workflow_discovery_gap_recovery_workflow`.

The accurate conclusion is therefore:

- the current queried Vontology/MCP surface is missing materialised self-authoring concepts right now;
- workflow-family bootstrap exists in code and can republish several of them at startup; but
- the parity/materialisation story is incomplete and inconsistent across workflow concepts vs testing type concepts.

This also means repeated `exists: false` results should not automatically be interpreted as "the codebase lacks the concept". They may instead indicate one of several operational states that currently are not easy to distinguish from the outside:

- the MCP surface is pointed at a fresh or test database;
- startup bootstrap has not run in the queried process;
- background parity/inventory publication is still pending;
- workflow-family bootstrap succeeded but testing type-concept parity did not;
- the queried environment is genuinely missing required authoritative artefacts.

### 5.3 Newly Identified Gaps

| ID | Gap | Severity | Evidence | Relationship to Existing Tasks |
|----|-----|----------|----------|-------------------------------|
| **A** | **Testing substrate type-concept bootstrap missing** | HIGH | `#V#ephemeral_theory`, `#V#experiment_spec`, and `#V#experiment_run` are used as `parent_concept_ids` in `testing_theory_service.py` and `experiment_run_service.py`, but unlike workflow concepts there is no dedicated bootstrap/seed parity path ensuring those type concepts exist up front. `create_concept()` does not repair missing parent types automatically. | Prerequisite for JVNAUTOSCI-1720/1721. Needs scope expansion or standalone task. |
| **B** | **No standalone MCP lint/validation surface for arbitrary candidate VWL definitions** | MEDIUM | Validation infrastructure already exists in `validate_workflow_definition_contract()`, Workflow Studio preview, and `workflow_authoring.validate_workflow_definition`. The gap is that there is no simple first-class MCP tool that accepts a candidate authoring spec/definition and returns structural validation results without routing through a larger authoring workflow. | Should be scoped into JVNAUTOSCI-1722 or standalone. Needed before generation pipeline (1724/1725) can lint candidates cleanly. |
| **C** | **No published machine-readable VWL authoring schema/contract surface** | MEDIUM | VWL structure is enforced by loader/validation code and policy schemas, but there is no single published JSON Schema or equivalent authoring contract surface that prompt programs and external tooling can target directly. `von_workflow_language_manual.md` remains narrative. | Prerequisite for JVNAUTOSCI-1724 (generation prompts need a stable reference contract to emit against). |
| **D** | **No bulk concept-parity audit MCP tool** | LOW | Cross-referencing code-referenced concept IDs against Vontology requires N sequential `concept_exists` calls. Created as **JVNAUTOSCI-1733**. | Independent observability improvement. Supports 1720/1721. |
| **E** | **No first-class rollout/lineage model for multiple published workflow revisions** | MEDIUM | `workflow_publication_lifecycle.v1` and `definition_identity` exist, but there is no explicit model for v1/v2 coexistence, canary routing, supersession chains, or gradual promotion/demotion across published revisions. | Should be scoped into JVNAUTOSCI-1730 (rollback/supersession) or standalone. |
| **F** | **Prompt authority health checks and failure signalling should be standardised across self-authoring prompt families** | LOW | The workflow-gap prompt path already aims to fail closed, and missing prompts surface explicit diagnostics in `workflow_gap_recovery_workflow.py`. The improvement is to make that behaviour consistent across self-authoring prompt families, expose health-check diagnostics, and prevent any future silent degradation. | Should be scoped into JVNAUTOSCI-1724 or a small cross-cutting prompt-authority task. |

### 5.4 Updated Task Summary

| Key | Phase | Summary | New Dependency |
|-----|-------|---------|----------------|
| JVNAUTOSCI-1720 | 1 | Harden ephemeral theory isolation | **Needs Gap A** resolved first (type bootstrap) |
| JVNAUTOSCI-1721 | 1 | Harden experiment schema | **Needs Gap A** resolved first |
| JVNAUTOSCI-1722 | 1 | Build capability_test_execution_workflow | Should absorb **Gap B** (VWL validation MCP tool) |
| JVNAUTOSCI-1724 | 2 | Intent-to-VWL generation prompts | **Needs Gap C** (published VWL schema for prompt reference) |
| JVNAUTOSCI-1730 | 4 | Promotion lifecycle with rollback | Should absorb **Gap E** (workflow versioning) |
| JVNAUTOSCI-1733 | Cross-cutting | Bulk concept-parity audit MCP tool | **New task** (Gap D) |
