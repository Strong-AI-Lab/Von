# Testing Workflows and Ephemeral Theories

Status: Design only  
Date: 2026-03-18  
Audience: Human engineers and AI agents  
Related epics/tasks: `JVNAUTOSCI-833`, `JVNAUTOSCI-964`, `JVNAUTOSCI-936`, `JVNAUTOSCI-1517`

## 1. Purpose

This document designs a workflow-first framework for "Testing Workflows" in Von.

The intent is to let Von:

1. create bounded, non-authoritative experimental theories inside or alongside Vontology,
2. run workflows, tools, learning loops, and capability checks against those theories,
3. record structured evidence about what happened,
4. safely promote only validated outcomes into canonical Vontology when appropriate.

This is explicitly a design document. It does not propose implementation in this task.

## 2. Executive Summary

Von already has most of the runtime needed for a Testing Workflows framework:

1. durable VWL workflows,
2. structured execution evidence and completion gates,
3. workflow-gap recovery and candidate testing,
4. isolated benchmark execution using cloned databases,
5. some support for transient microtheory-like payloads.

The main missing layer is not more workflow engine. It is a first-class theory and experiment representation with a safe mutation boundary.

The clean design is a two-tier system:

1. `Ephemeral theory` tier: lightweight, bounded, non-canonical theory slices for rapid experiments, workflow checks, prompt/tool comparisons, and safe reasoning tests.
2. `Isolated benchmark world` tier: heavyweight DB-clone execution for destructive or system-wide experiments where theory-local isolation is not enough.

The critical architectural rule is that Testing Workflows themselves should be VWL/Vontology artefacts. Python should provide only reusable theory-slice, diff, rollback, promotion, and telemetry primitives.

## 3. Why This Matters

Von already wants to be workflow-first, ontology-backed, and capable of learning from its own execution. Testing Workflows would make that explicit and safer.

Potential outcomes:

1. workflow regression testing without contaminating canonical knowledge,
2. tool and prompt benchmarking grounded in real execution traces,
3. bounded self-experimentation on workflow choice, plan structure, and repair strategies,
4. reusable experiment records for measurement and later learning,
5. a principled path from "candidate idea" to "validated behaviour" to "published workflow or promoted knowledge".

This may be novel in the particular combination of:

1. ontology-backed theory representation,
2. durable workflow execution,
3. explicit promotion gates,
4. self-experimentation bounded by namespace and theory isolation.

The literature scan in this task found adjacent work, but not an exact match for that combination.

## 4. Existing Bases in the Repo

### 4.1 Workflow runtime

Von already has the core workflow runtime and durable execution model:

1. [von_workflow_language_manual.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/von_workflow_language_manual.md)
2. [durable_workflow_system_design.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/durable_workflow_system_design.md)
3. [models.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/workflows/durable/models.py)
4. [plan_state_runtime.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/workflows/plan_state_runtime.py)

Relevant consequences:

1. durable instances already exist,
2. checkpoints and resumes already exist,
3. subworkflows and control-flow actions already exist,
4. workflow publication and runnability gates already exist.

### 4.2 Transient microtheory substrate

There is already a notion of `transient_microtheory`, but only as a payload shape for renderer applicability:

1. [renderer_applicability_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/renderer_applicability_service.py)
2. [orchestrator.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/integrations/internal_mcp/orchestrator.py)
3. [renderer_applicability_vontology_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/renderer_applicability_vontology_service.py)

This is important because it shows the system already accepts an object kind that is neither a canonical concept nor a free-text blob. It is a strong seed for a more general ephemeral-theory design.

### 4.3 Structured execution evidence

Von already records structured execution evidence and benchmarkable failure signals:

1. [turn_execution_completion_schema_proposal.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/turn_execution_completion_schema_proposal.md)
2. [turn_execution_record_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/turn_execution_record_service.py)
3. `turn_execution_*` MCP tools in [catalogue.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/integrations/internal_mcp/catalogue.py)

This means Testing Workflows do not need to invent result evidence from scratch. They should extend this discipline to theory experiments.

### 4.4 Existing benchmark harness

Von already has a guarded end-to-end benchmark harness:

1. [kb_clone_benchmark_harness.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/kb_clone_benchmark_harness.md)
2. [kb_clone_benchmark_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/kb_clone_benchmark_service.py)

This is already suitable for destructive integration tests and system-wide experiments. It is too heavy for rapid internal experimentation, but it should become Tier 2 of the Testing Workflows design rather than a parallel subsystem.

### 4.5 Workflow-gap candidate testing

Von already has the pattern "identify a workflow gap, create a candidate, optionally test it, then decide whether to use it":

1. [workflow_gap_recovery_workflow.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/workflows/durable/workflow_gap_recovery_workflow.py)
2. [workflow_gap_workflow_contracts.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/workflows/workflow_gap_workflow_contracts.py)
3. [workflow_creation_contracts.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/workflows/workflow_creation_contracts.py)

That is already very close in spirit to Testing Workflows.

### 4.6 Learning and episode signals

Von already has learning-oriented workflow selection and episode history machinery:

1. [workflow_selection_experience.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/workflow_selection_experience.py)
2. [workflow_selection_policy_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/workflow_selection_policy_service.py)
3. [workflow_episode_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/workflow_episode_service.py)

Testing Workflows should consume and emit signals compatible with those services.

## 5. What Is Missing

### 5.1 First-class theory representation

Current `transient_microtheory` support is payload-only. It is not yet a first-class Vontology theory model with canonical types, predicates, inclusion, or promotion semantics.

Missing capabilities:

1. canonical type for ephemeral or testing theories,
2. canonical representation for assumptions, hypotheses, assertions, theorems, and observations inside a theory,
3. explicit provenance, expiry, and promotion metadata,
4. a standard way to compare theory-local state with canonical state.

### 5.2 First-class experiment representation

Von has turn records and benchmark manifests, but not a general `experiment_run` model for theory testing.

Missing capabilities:

1. experiment specification artefact,
2. experiment run record,
3. variable and control representation,
4. expected versus observed outcomes,
5. formal pass/fail/partial/inconclusive result contract,
6. experiment suite aggregation.

### 5.3 Theory-safe mutation boundary

The safety model is not complete enough yet for theory inclusion and self-experimentation.

Relevant evidence:

1. [security_considerations.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/security_considerations.md) explicitly says namespace is intended to interact with future microtheory and theory-inclusion work.
2. [effective_namespace_contract.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/effective_namespace_contract.md) already defines one authoritative namespace-resolution path, but not theory inclusion semantics.

Missing capabilities:

1. theory inclusion and exclusion rules bound to namespace,
2. theory-local write isolation,
3. explicit promotion from theory-local to canonical ontology,
4. rollback or garbage collection of ephemeral theories.

### 5.4 Theory operation control surface

There is no reusable action surface for theory operations such as:

1. create ephemeral theory,
2. clone a theory slice from canonical context,
3. assert or retract theory-local claims,
4. compute canonical-versus-theory diff,
5. execute a workflow against a specified theory,
6. promote validated results,
7. discard or expire theory-local state.

### 5.5 Lightweight, frequent testing harness

The DB-clone harness is too expensive for rapid, repeated self-experiments. A theory-slice harness is needed between "full clone" and "no isolation at all".

### 5.6 Introspection/tooling reliability

During this design task, `workflow_list_definitions` and `workflow_list_event_bindings` timed out through the Vontology MCP surface. This is not the main design blocker, but it is a practical tooling gap for agents trying to inspect the workflow/testing surface reliably.

## 6. Design Principles

1. Workflow-first: Testing Workflows themselves should be authored in VWL/Vontology, not encoded as bespoke Python orchestration.
2. Canonical protection: testing writes must not silently leak into canonical Vontology.
3. Explicit promotion: promotion is a separate gated workflow, never an incidental side effect.
4. Evidence first: every run produces structured evidence, not only narrative conclusions.
5. Fail closed: missing prompts, theory slices, or workflow contracts block the experiment rather than triggering silent fallback.
6. Bounded lifetime: ephemeral theories must expire or be garbage-collected unless deliberately retained.
7. Reproducibility: experiment specs and theory inputs must be sufficient to replay a run.
8. Safety over convenience: self-experimentation may tune prompts, workflow choice, or candidate graphs, but should not directly self-patch authoritative behaviour without explicit promotion gates.

## 7. Proposed Core Model

## 7.1 Two-tier testing architecture

### Tier 1: Ephemeral theory tests

Use for:

1. prompt comparison,
2. tool-plan comparison,
3. workflow-routing comparison,
4. theory-local reasoning checks,
5. safe provisional assertions,
6. candidate workflow or model-policy evaluation.

Properties:

1. cheap,
2. fast,
3. theory-local,
4. bounded lifetime,
5. not authoritative.

### Tier 2: Isolated benchmark worlds

Use for:

1. destructive workflow tests,
2. ingestion and indexing workflows,
3. multi-turn and multi-workflow integration tests,
4. experiments that require real persistence semantics across many collections.

Properties:

1. heavy,
2. slower,
3. DB-clone based,
4. stronger isolation,
5. suited for final verification and regression suites.

## 7.2 New artefact families

The framework should add four main artefact families.

### A. Testing theory

Purpose: non-canonical, bounded theory slice for experimentation.

Suggested concept IDs and types:

1. `#V#testing_theory` as a subtype of a future `#V#theory` or temporary first-class type,
2. `#V#ephemeral_theory` as a subtype of `#V#testing_theory`,
3. `#V#canonical_promotion_candidate` for validated theory outputs.

Core fields or relations:

1. source namespace,
2. included canonical concepts,
3. theory-local assertions,
4. assumptions,
5. hypotheses,
6. observations,
7. expiry timestamp,
8. provenance,
9. promotion policy,
10. experiment suite linkage.

### B. Experiment specification

Purpose: declarative definition of what is being tested and how success is measured.

Suggested concept or schema family:

1. `#V#experiment_spec`
2. `#V#testing_workflow_spec`
3. `#V#capability_test_spec`

Key contents:

1. target capability or workflow,
2. isolation mode (`theory_slice` or `clone_db`),
3. fixtures and inputs,
4. required tools and workflows,
5. expected outcomes,
6. evaluation rubric,
7. allowable side effects,
8. promotion eligibility rules.

### C. Experiment run

Purpose: durable record of what actually happened.

Suggested concept or schema family:

1. `experiment_run.v1`
2. `experiment_suite_run.v1`

Key contents:

1. run ID,
2. linked experiment spec,
3. linked theory or clone environment,
4. workflow execution evidence,
5. tool invocation evidence,
6. expected versus observed diffs,
7. safety and policy decisions,
8. final verdict,
9. candidate learning signals,
10. promotion recommendations.

### D. Testing workflow

Purpose: VWL workflow that consumes an experiment spec plus a theory or clone environment and produces an experiment run.

Suggested families:

1. `#V#testing_workflow`
2. `#V#theory_slice_test_workflow`
3. `#V#clone_benchmark_test_workflow`
4. `#V#promotion_gate_workflow`

## 8. Suggested Ontology Additions

The exact ontology naming should be decided carefully, but the design likely needs types and predicates in this general shape.

### 8.1 Candidate types

1. `#V#theory`
2. `#V#testing_theory`
3. `#V#ephemeral_theory`
4. `#V#experiment_spec`
5. `#V#experiment_run`
6. `#V#experiment_suite`
7. `#V#experiment_observation`
8. `#V#promotion_decision`

### 8.2 Candidate predicates

1. `#V#includes_theory`
2. `#V#derived_from_canonical_concept`
3. `#V#has_assumption`
4. `#V#has_hypothesis`
5. `#V#has_observation`
6. `#V#tests_capability`
7. `#V#tests_workflow`
8. `#V#uses_isolation_mode`
9. `#V#has_expected_outcome`
10. `#V#has_observed_outcome`
11. `#V#has_experiment_verdict`
12. `#V#promotion_requires`
13. `#V#promotes_to_canonical_relation`
14. `#V#expires_at`
15. `#V#belongs_to_experiment_suite`

### 8.3 Assertion model choice

One unresolved question is the canonical unit of theory content:

1. free-text claims,
2. binary or n-ary assertions,
3. uncertain relationship assertions,
4. a mixed model.

Current recommendation:

1. use structured assertions wherever possible,
2. allow attached free text for rationale and notes,
3. reuse uncertain-relationship style provenance fields when the theory contains provisional relation claims.

Relevant existing substrates:

1. [uncertain_relationship_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/uncertain_relationship_service.py)
2. [relation_elicitation_service.py](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/src/backend/services/relation_elicitation_service.py)

## 9. Workflow Architecture

The cleanest flow is:

1. create or resolve experiment spec,
2. create theory slice or clone world,
3. materialise fixtures and candidate assertions,
4. execute the target workflow or tool plan,
5. capture structured evidence,
6. compare expected versus observed outcomes,
7. decide `pass`, `fail`, `partial`, or `inconclusive`,
8. optionally run a promotion gate,
9. either discard the theory, retain it for debugging, or promote validated artefacts.

### 9.1 Recommended workflow families

1. `theory_slice_setup_workflow`
2. `capability_test_execution_workflow`
3. `workflow_regression_test_workflow`
4. `tool_contract_test_workflow`
5. `learning_signal_harvest_workflow`
6. `promotion_gate_workflow`
7. `ephemeral_theory_gc_workflow`

### 9.2 Recommended reusable actions

Python should provide only generic actions such as:

1. `theory.create_slice`
2. `theory.import_canonical_context`
3. `theory.assert_local_claim`
4. `theory.compute_diff`
5. `theory.rollback_local_writes`
6. `theory.promote_validated_claims`
7. `experiment.record_observation`
8. `experiment.compute_verdict`
9. `experiment.emit_learning_signal`

These should be reusable control surfaces, not task-specific orchestration.

## 10. Safety Model

## 10.1 Canonical Vontology protection

Testing Workflows must default to writing only into:

1. theory-local state, or
2. isolated benchmark clones.

Canonical ontology writes should occur only in a separate promotion workflow.

## 10.2 Namespace and theory inclusion

Namespace must become the main access-safety primitive for theory inclusion and promotion. This is already anticipated in [security_considerations.md](/c:/Users/witbr/Documents/Programming/Strong-AI-Lab/Von/docs/engineering/security_considerations.md).

Required policy:

1. theory inherits namespace and organisation scope,
2. theory inclusion cannot widen visibility beyond its namespace policy,
3. promotion into canonical space requires explicit policy and audit trail,
4. cross-namespace theory inclusion is disallowed unless a future access-control policy explicitly allows it.

## 10.3 Expiry and garbage collection

Ephemeral theories should carry TTL semantics.

Recommended outcomes:

1. discard by default after successful non-promoting tests,
2. retain on failure when debugging is requested or policy requires it,
3. retain promoted theories only as auditable history or condensed provenance, not as active hidden memory.

## 10.4 What self-experimentation is allowed

Allowed targets should likely include:

1. workflow selection policy,
2. prompt variants,
3. tool sequencing,
4. candidate workflow graphs,
5. reasoning strategies inside isolated theory space.

Not allowed without stronger gates:

1. direct self-editing of authoritative runtime code,
2. direct publication of new canonical workflows without validation,
3. theory-to-canonical promotion without explicit promotion workflow success.

## 11. Integration With Current Systems

### 11.1 Turn execution records

Testing Workflows should reuse the same evidence discipline as turn execution:

1. candidate set,
2. selected workflow,
3. required effects,
4. tool history,
5. postcondition checks,
6. completion or test verdict.

The natural extension is an `experiment_run.v1` schema parallel to `turn_execution_record.v1`.

### 11.2 Benchmark harness

The current KB-clone harness should become the Tier 2 runner, not a separate evaluation universe.

### 11.3 Workflow-gap recovery

Testing Workflows should absorb and generalise the existing gap-recovery candidate test pattern rather than duplicating it.

### 11.4 Learning loops

Experiment runs should emit structured learning signals consumable by the workflow-selection experience and policy services.

## 12. Missing Bases and Related Jira

The following pending Jira work appears directly relevant.

### 12.1 Workflow-first convergence

1. `JVNAUTOSCI-1527`: rewrite planning, rumination, parent-specificity, and gap-recovery workflows as Vontology-authored definitions.
2. `JVNAUTOSCI-1528`: rewrite support and maintenance durable workflows as Vontology-authored definitions.

Why relevant:

1. Testing Workflows should reuse planning, rumination, and gap-recovery patterns.
2. Introducing Testing Workflows before these migrate risks inheriting hybrid Python-defined orchestration.

### 12.2 Evaluation and measurement

1. `JVNAUTOSCI-964`: Von Experimental Evaluation and Measurement.
2. `JVNAUTOSCI-1274`: define benchmark-driven evaluation framework for VQL reasoning.
3. `JVNAUTOSCI-1057`: episode log analysis for tool learning feedback loops.

Why relevant:

1. experiment-run metrics belong under the evaluation epic,
2. theory testing should reuse the benchmark-driven evaluation mindset,
3. test outputs should feed learning loops rather than ending as isolated reports.

### 12.3 Theory and access-control foundations

1. `JVNAUTOSCI-296`: implement concept-level access control with `#V#accessible_to` relations.
2. `JVNAUTOSCI-254`: Vontology-first context bundles and reconstructed dossiers for concepts and workflows.
3. `JVNAUTOSCI-1266`: specify context semantics, entailment profiles, and capability negotiation.
4. `JVNAUTOSCI-343`: develop definitions and relationships for theory, claim, assertion, and hypothesis.

Why relevant:

1. ephemeral theories need safe visibility boundaries,
2. theory slices need richer context semantics,
3. testing theories need a stronger ontology for claims, assertions, and hypotheses than is currently obvious from concept search,
4. context bundles may become the natural input form for theory slices.

## 13. Recommended Delivery Sequence

This is the recommended incremental path.

### Phase 1: Represent theory and experiments

1. Define ontology vocabulary for theory, experiment spec, and experiment run.
2. Decide whether ephemeral theories are persisted TTL concepts, separate theory slices, or both.
3. Publish the schema and safety contract.

### Phase 2: Build generic theory actions

1. create theory slice,
2. assert local claim,
3. compare observed versus expected state,
4. compute diff,
5. promote validated output,
6. discard or expire theory.

### Phase 3: Materialise first testing workflows

1. workflow regression test,
2. tool contract test,
3. prompt comparison test,
4. candidate workflow publication test.

### Phase 4: Connect learning loops

1. feed experiment signals into workflow selection experience,
2. add experiment dashboards and summaries,
3. add policy-controlled promotion suggestions.

### Phase 5: Expand to bounded self-experimentation

1. permit workflow-choice experiments,
2. permit candidate workflow graph experiments,
3. keep publication and promotion behind explicit validation gates.

## 14. Hard Open Questions

These are real design questions, not placeholders.

1. Should ephemeral theories live as transient payloads, TTL concepts in main Vontology, or a separate theory-slice store?
2. How should theory inclusion compose with namespace and future access control?
3. What is the canonical unit of assertion inside a theory: free text, predicate triples, n-ary claims, uncertain assertions, or mixed?
4. What counts as safe self-experimentation versus unsafe self-modification?
5. Should experiment promotion produce canonical knowledge only, or may it also publish new workflow graphs and policies?
6. How reversible must promotion be?
7. Should theory-local execution be able to call all normal tools, or only a capability-scoped safe subset?
8. How much of experiment evaluation should be deterministic versus LLM-judged?
9. Should experiment runs be stored as Vontology artefacts, Mongo projections, or both?
10. How should an agent decide when to stay in Tier 1 versus escalate to Tier 2?

## 15. Literature Notes

This task included a bounded literature pass. The most relevant material found was adjacent rather than exact.

### 15.1 Closest adjacent work

1. Agent Laboratory: structured multi-agent research workflow spanning literature review, experimentation, and report writing.  
   Source: <https://agentlaboratory.github.io/>  
   Paper: <https://arxiv.org/pdf/2501.04227>
2. AgentRxiv: collaborative autonomous research with cumulative access to prior generated research.  
   Source: <https://agentrxiv.github.io/>  
   Paper page: <https://arxiv.org/html/2503.18102v1>
3. A Self-Improving Coding Agent: self-editing agent loop using benchmark-driven improvement.  
   Source: <https://arxiv.org/abs/2504.15228>
4. MLR-Bench: open-ended machine-learning research benchmark with staged evaluation and automated judging.  
   Source: <https://github.com/chchenhui/mlrbench>  
   Poster summary: <https://neurips.cc/virtual/2025/poster/121719>
5. SUPER: benchmark for setting up and executing tasks from research repositories.  
   Source: <https://arxiv.org/abs/2409.07440>  
   ACL PDF: <https://aclanthology.org/2024.emnlp-main.702.pdf>
6. ML-Bench: repository-scale end-to-end execution benchmark for LLMs and agents.  
   Source: <https://ml-bench.github.io/>  
   OpenReview PDF: <https://openreview.net/pdf?id=T2mtCFKIEG>

### 15.2 Classical microtheory background

The direct modern literature search here was weak on "microtheories" specifically, but the classical idea is clearly relevant: compartmentalised knowledge contexts with controlled inheritance and inclusion. One accessible pointer found during this task is a Cognitive Systems poster discussing Cyc-style microtheories and their use as cases:

1. Poster discussing microtheories as cases and contextual bins of facts: <http://www.cogsys.org/posters/2018/poster-2018-5.pdf>

### 15.3 Literature conclusion

The scan did not find an exact precedent for:

1. ontology-backed ephemeral theories,
2. durable workflow execution over those theories,
3. explicit promotion gates into canonical knowledge,
4. safe self-experimentation by the same agentic system on its own workflow behaviour.

That does not prove novelty, but it does suggest the precise combination is at least underexplored in currently visible agent literature.

## 16. Literature Review Plan for a Non-Coding Agent

If a deeper literature review is needed, use this plan.

### Search themes

1. `LLM agent self-improvement benchmark`
2. `autonomous research agent evaluation benchmark`
3. `microtheory knowledge representation context inclusion`
4. `safe self-modification agents`
5. `sandboxed agent evaluation workflow`
6. `scientific discovery agent benchmark`
7. `ontology-backed experimentation` or `knowledge graph experiment provenance`

### Databases

1. arXiv
2. ACL Anthology
3. NeurIPS proceedings
4. ICLR / OpenReview
5. Google Scholar
6. Semantic Scholar

### Questions to answer

1. What is the closest work to safe self-experimentation on agent workflows?
2. How do existing systems represent provisional theories, hypotheses, or contexts?
3. What safety mechanisms are used when agents modify or evaluate themselves?
4. What experiment record schemas and evaluation standards already exist?
5. Are there prior systems that separate provisional theory from canonical knowledge with explicit promotion gates?

### Output format

For each paper:

1. problem addressed,
2. experiment model,
3. isolation mechanism,
4. whether self-improvement or self-experimentation is involved,
5. whether knowledge representation is ontology-backed,
6. direct relevance to Von.

## 17. Bottom Line

Von is already closer to Testing Workflows than it may appear.

The workflow engine, benchmark harness, execution evidence, and candidate-testing patterns mostly exist. The missing base is a first-class theory and experiment layer with safe inclusion, isolation, and promotion semantics.

If that layer is added cleanly, Testing Workflows can be built mostly by composing existing workflow, benchmark, and learning primitives rather than by inventing a new subsystem.
