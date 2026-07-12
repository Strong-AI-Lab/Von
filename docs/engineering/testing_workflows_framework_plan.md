Status: Planning document only
Date: 2026-03-18
Authoring context: Compiled by Codex from prior design exploration, current repo state, and current Jira state
Primary Jira task: `JVNAUTOSCI-1531`

# Testing Workflows Framework Plan

## 1. Purpose

This document defines a concrete planning framework for "Testing Workflows" in Von.

The goal is to let Von:

1. create bounded experimental theories or isolated benchmark worlds,
2. run workflows, tools, prompts, and learning policies against them,
3. collect structured evidence about what happened,
4. decide pass, fail, partial, or inconclusive outcomes,
5. promote only validated results into authoritative Vontology or workflow publication paths.

This is a planning artefact, not an implementation.

## 2. Executive Summary

Von now has most of the workflow-runtime substrate required for a testing framework:

1. Vontology-authoritative workflow execution and routing,
2. durable workflow instances, schedules, and event bindings,
3. structured turn-execution evidence,
4. workflow-gap recovery and candidate workflow testing patterns,
5. a heavyweight isolated benchmark harness.

The missing layer is not another workflow engine. The missing layer is a first-class theory and experiment model plus a safe isolation boundary for testing.

The clean architecture is two-tier:

1. Tier 1: ephemeral-theory tests for cheap, fast, bounded experiments.
2. Tier 2: isolated benchmark worlds for destructive or system-wide verification.

Testing Workflows themselves should be VWL/Vontology workflows. Python should provide only reusable theory, experiment, comparison, promotion, rollback, and telemetry primitives.

## 3. Why This Matters

This framework would let Von do several things it cannot yet do cleanly:

1. validate candidate workflows without leaking writes into canonical knowledge,
2. test existing workflows against synthetic or semi-synthetic data,
3. compare prompt, tool, and routing variants with structured evidence,
4. run bounded self-experiments on its own capabilities,
5. accumulate reusable experiment records that feed evaluation and learning loops.

The combination appears unusual:

1. ontology-backed provisional theories,
2. durable workflow execution over those theories,
3. explicit promotion gates into canonical knowledge or published workflows,
4. bounded self-experimentation by the same agentic system.

The literature found adjacent work, but not this exact combination.

## 4. Concrete Example Scenarios

### 4.1 Meeting-invitation example

User action:

1. paste a meeting invitation into chat,
2. ask Von to test developing, using, and validating a workflow for that meeting type.

The intended framework behaviour:

1. create or resolve an `experiment_spec` for "meeting invitation workflow derivation and validation",
2. create an ephemeral theory containing:
   - the invitation text,
   - candidate meeting-type hypotheses,
   - provisional assumptions about desired workflow outcomes,
   - test-local expected outputs,
3. derive or select a candidate workflow for that meeting kind,
4. execute the candidate workflow against the ephemeral theory,
5. validate:
   - whether the workflow classified the meeting appropriately,
   - whether it extracted the expected structure,
   - whether it produced the expected downstream actions or summaries,
   - whether it avoided forbidden writes or unsafe external actions,
6. record an explicit verdict and evidence bundle,
7. optionally promote the workflow or just retain the run as evidence.

Expected example assertions:

1. a recurring lab meeting should produce a recurring-meeting workflow candidate, not a one-off event workflow,
2. participant and organiser resolution should remain theory-local during testing,
3. no canonical calendar or task mutations should occur unless the test explicitly enters a promotion or execution gate.

### 4.2 Synthetic workflow-regression example

Operator action:

1. choose an existing workflow,
2. supply or generate a synthetic dataset of representative inputs,
3. ask Von to test the workflow against the dataset.

The intended framework behaviour:

1. resolve the target workflow and the synthetic fixture set,
2. create one theory slice per case or one benchmark world per suite, depending on risk,
3. run the workflow over each case,
4. compare observed versus expected effects,
5. aggregate suite metrics such as pass rate, failure modes, and mutation safety,
6. emit structured learning or benchmark signals,
7. retain failing cases for replay and debugging.

Good first synthetic targets:

1. file-copy upload classification and routing,
2. meeting or event interpretation,
3. workflow-selection and tool-routing decisions,
4. relation-elicitation or enrichment workflows,
5. workflow-gap recovery candidate tests.

### 4.3 Bounded self-experimentation example

System action:

1. compare two candidate workflow-routing policies or prompt variants,
2. run both against a bounded suite of test cases,
3. determine whether one policy improves outcomes without increasing unsafe behaviour,
4. surface a recommendation, not a silent self-patch.

The framework must ensure that:

1. candidate policies are not published directly from test results,
2. learning signals are explicit and inspectable,
3. promotion remains a separate gated workflow.

## 5. Existing Foundations Now Present

### 5.1 Workflow-authoritative runtime

The recent workflow-first convergence sweep substantially changed the starting point for this plan. The following are now done:

1. `JVNAUTOSCI-1517`
2. `JVNAUTOSCI-1518`
3. `JVNAUTOSCI-1519`
4. `JVNAUTOSCI-1520`
5. `JVNAUTOSCI-1521`
6. `JVNAUTOSCI-1523`
7. `JVNAUTOSCI-1524`
8. `JVNAUTOSCI-1525`
9. `JVNAUTOSCI-1526`
10. `JVNAUTOSCI-1527`
11. `JVNAUTOSCI-1528`
12. `JVNAUTOSCI-1529`

Important consequences:

1. production workflow authority is now Vontology-first,
2. legacy selector fallback is gone,
3. capability override fallbacks are gone,
4. persisted workflow bindings and authoritative workflow descriptions are now the norm.

This matters because Testing Workflows can now rely on an authoritative workflow substrate rather than inheriting hybrid runtime ambiguity.

### 5.2 Workflow runtime and durable execution

Relevant existing bases:

1. [von_workflow_language_manual.md](von_workflow_language_manual.md)
2. [durable_workflow_system_design.md](durable_workflow_system_design.md)
3. [models.py](../../src/backend/workflows/durable/models.py)
4. [plan_state_runtime.py](../../src/backend/workflows/plan_state_runtime.py)

What these already give us:

1. durable instances,
2. checkpoints and resumes,
3. subworkflows,
4. verification and completion gates,
5. publication and runnability concepts.

### 5.3 Structured execution evidence

Relevant existing bases:

1. [turn_execution_completion_schema_proposal.md](turn_execution_completion_schema_proposal.md)
2. [turn_execution_record_service.py](../../src/backend/services/turn_execution_record_service.py)
3. `turn_execution_*` internal MCP tools.

This gives Testing Workflows a model for:

1. structured evidence capture,
2. expected versus observed effects,
3. benchmarkable failure categories.

### 5.4 Heavyweight benchmark harness

Relevant existing bases:

1. [kb_clone_benchmark_harness.md](kb_clone_benchmark_harness.md)
2. [kb_clone_benchmark_service.py](../../src/backend/services/kb_clone_benchmark_service.py)

This is already a good Tier 2 runner. It should be treated as the heavyweight backend of the testing framework, not a separate evaluation universe.

### 5.5 Candidate testing and learning loops

Relevant existing bases:

1. [workflow_gap_recovery_workflow.py](../../src/backend/workflows/durable/workflow_gap_recovery_workflow.py)
2. [workflow_selection_experience.py](../../src/backend/services/workflow_selection_experience.py)
3. [workflow_selection_policy_service.py](../../src/backend/services/workflow_selection_policy_service.py)
4. [workflow_episode_service.py](../../src/backend/services/workflow_episode_service.py)

These already suggest the right shape:

1. test candidates explicitly,
2. record episode-like evidence,
3. emit learning-compatible signals.

### 5.6 Existing seed for transient theories

There is already a `transient_microtheory` payload idea in renderer applicability work. It is not yet a full theory model, but it is a strong seed for a broader ephemeral-theory design.

## 6. What Is Still Missing

### 6.1 First-class theory model

Von does not yet have a complete, canonical theory model for testing contexts.

Missing pieces:

1. a type hierarchy for theory, ephemeral theory, hypothesis, claim, assertion, and observation,
2. theory inclusion and composition semantics,
3. theory-local provenance and expiry,
4. canonical-versus-theory diff semantics,
5. promotion semantics from theory-local state to canonical state.

### 6.2 First-class experiment model

Von has turn-execution records and benchmark harnesses, but not a general-purpose experiment model.

Missing pieces:

1. `experiment_spec`,
2. `experiment_run`,
3. variables and controls,
4. expected versus observed outcomes,
5. pass/fail/partial/inconclusive contracts,
6. suite aggregation and replay.

### 6.3 Theory-safe mutation boundary

The current namespace and access-control model is not yet sufficient for safe theory inclusion and promotion.

Missing pieces:

1. theory-aware visibility and inclusion rules,
2. explicit isolation boundaries for test-local writes,
3. promotion workflows that preserve auditability,
4. rollback and garbage collection semantics for ephemeral theories.

### 6.4 Theory and experiment control surfaces

Testing Workflows need reusable generic actions. These do not yet exist as a coherent family.

Needed generic actions:

1. `theory.create_slice`
2. `theory.import_canonical_context`
3. `theory.assert_local_claim`
4. `theory.compute_diff`
5. `theory.rollback_local_writes`
6. `theory.promote_validated_claims`
7. `experiment.record_observation`
8. `experiment.compute_verdict`
9. `experiment.emit_learning_signal`

### 6.5 Lightweight frequent test harness

The benchmark clone harness is too heavy for every experiment. A theory-slice runner is needed between:

1. "mutate nothing and hope",
2. "clone a full database every time".

### 6.6 Reliable inspection surface

The earlier exploration hit practical MCP introspection issues for workflow listing and binding inspection. This is not the main conceptual blocker, but it is a real implementation friction point for agent-driven testing.

## 7. Proposed Architecture

## 7.1 Two-tier testing architecture

### Tier 1: Ephemeral theory tests

Use for:

1. prompt comparisons,
2. routing comparisons,
3. tool-plan comparisons,
4. meeting-invitation workflow derivation and validation,
5. theory-local reasoning checks,
6. safe provisional assertions,
7. candidate workflow or policy evaluation.

Properties:

1. cheap,
2. fast,
3. bounded lifetime,
4. theory-local,
5. non-authoritative by default.

### Tier 2: Isolated benchmark worlds

Use for:

1. destructive or multi-collection workflows,
2. ingestion/indexing workflows,
3. integration tests across many services,
4. final verification before promotion or publication,
5. regression suites that need real persistence semantics.

Properties:

1. slower,
2. heavier,
3. DB-clone based,
4. stronger isolation,
5. more faithful for end-to-end verification.

## 7.2 Core artefact families

### A. Testing theory

Purpose:

1. represent a bounded, non-canonical experimental theory or context slice.

Should include:

1. source namespace,
2. included canonical concepts,
3. theory-local assertions,
4. assumptions,
5. expected observations,
6. expiry and retention policy,
7. promotion policy,
8. provenance.

### B. Experiment specification

Purpose:

1. define what is being tested, against what fixtures, with what expected outcomes.

Should include:

1. target workflow, prompt, tool contract, or policy,
2. fixture source,
3. theory setup instructions,
4. expected effects,
5. allowed side effects,
6. verdict rules,
7. retention and replay policy.

### C. Experiment run

Purpose:

1. record the actual execution and verdict.

Should include:

1. the theory or benchmark world used,
2. resolved workflow and candidate set,
3. tool history,
4. observed outputs and mutations,
5. comparison against expectations,
6. verdict,
7. metrics,
8. replay handles.

### D. Testing workflow

Purpose:

1. orchestrate test setup, execution, comparison, and retention or promotion.

Recommended families:

1. `theory_slice_setup_workflow`
2. `capability_test_execution_workflow`
3. `workflow_regression_test_workflow`
4. `tool_contract_test_workflow`
5. `learning_signal_harvest_workflow`
6. `promotion_gate_workflow`
7. `ephemeral_theory_gc_workflow`

## 8. Safety Model

### 8.1 Canonical protection

Testing Workflows must default to writing only into:

1. theory-local state,
2. isolated benchmark clones,
3. experiment-run evidence stores.

Canonical Vontology writes must occur only in an explicit promotion workflow.

### 8.2 Namespace and theory inclusion

Required policy:

1. a testing theory inherits namespace and organisation scope,
2. theory inclusion cannot widen visibility,
3. cross-namespace theory inclusion is disallowed unless future access control explicitly allows it,
4. promotion into canonical space requires policy, provenance, and an audit trail.

### 8.3 Allowed self-experimentation

Allowed targets should likely include:

1. workflow selection policy,
2. prompt variants,
3. tool sequencing,
4. candidate workflow graphs,
5. reasoning strategies inside isolated theory space.

Not allowed without stronger gates:

1. direct self-editing of authoritative runtime code,
2. direct publication of canonical workflows from a test verdict alone,
3. direct canonical mutation without an explicit promotion workflow.

### 8.4 Expiry and garbage collection

Ephemeral theories should have TTL semantics:

1. discard by default after successful non-promoting tests,
2. retain failures when needed for debugging,
3. retain promoted theories only as auditable history or condensed provenance.

## 9. Evidence, Metrics, and Success Criteria

Each experiment run should capture:

1. candidate set,
2. selected workflow or policy,
3. expected effects,
4. observed effects,
5. tool and mutation history,
6. prohibited side-effect checks,
7. verdict,
8. replay handle.

Useful aggregate metrics:

1. pass rate,
2. partial and inconclusive rates,
3. false-success rate,
4. prohibited-write rate,
5. benchmark-world escalation rate,
6. promotion acceptance rate,
7. learning-signal quality or reuse rate.

Framework success criteria for an MVP:

1. one meeting-invitation-derived workflow test runs end to end in Tier 1,
2. one existing workflow regression suite runs over synthetic fixtures,
3. at least one failing case is replayable,
4. no canonical writes occur outside promotion workflows,
5. experiment outputs are structured enough to feed dashboards or learning loops.

## 10. Related Jira and Dependency Map

### 10.1 Existing foundations now largely complete

Completed work that lowers risk:

1. `JVNAUTOSCI-1517` and its convergence subtasks,
2. `JVNAUTOSCI-1523` strict Vontology-authoritative runtime mode,
3. `JVNAUTOSCI-1524` workflow purity scoreboard and non-regression gates.

### 10.2 Directly relevant open work

Evaluation and measurement:

1. `JVNAUTOSCI-964` `To Do`: umbrella evaluation and measurement epic
2. `JVNAUTOSCI-1274` `To Do`: benchmark-driven evaluation framework for VQL reasoning
3. `JVNAUTOSCI-1057` `To Do`: episode log analysis for tool learning feedback loops

Theory, context, and ontology semantics:

1. `JVNAUTOSCI-936` `In Progress`: representationally rich Vontology and inference engine
2. `JVNAUTOSCI-254` `In Progress`: context bundles and reconstructed dossiers
3. `JVNAUTOSCI-1266` `To Do`: context semantics, entailment profiles, capability negotiation
4. `JVNAUTOSCI-343` `To Do`: theory, claim, assertion, and hypothesis definitions
5. `JVNAUTOSCI-296` `To Do`: concept-level access control

Agentic behaviour context:

1. `JVNAUTOSCI-833` `In Progress`: agentic behaviours in Von

### 10.3 Interpretation

This testing framework should not wait for all of those issues to finish before starting. But the implementation plan should explicitly absorb or depend on them where appropriate:

1. use `JVNAUTOSCI-964` as the umbrella for evaluation semantics,
2. use `JVNAUTOSCI-936`, `JVNAUTOSCI-343`, and `JVNAUTOSCI-1266` to define theory semantics,
3. use `JVNAUTOSCI-296` for access and visibility safety,
4. use `JVNAUTOSCI-1057` and related episode infrastructure for learning signals.

## 11. Recommended Delivery Sequence

### Phase 0: Planning and contracts

Deliverables:

1. this planning document,
2. a canonical problem statement and success criteria,
3. agreement on Tier 1 versus Tier 2 boundaries.

Exit criteria:

1. the framework scope is stable enough to create implementation tasks without re-arguing the architecture.

### Phase 1: Theory and experiment ontology

Deliverables:

1. ontology vocabulary for theory, experiment spec, experiment run, observation, verdict, promotion candidate,
2. initial theory and experiment schemas,
3. context and inclusion rules.

Exit criteria:

1. we can represent a theory and an experiment run in Vontology without inventing ad hoc fields each time.

### Phase 2: Generic theory and experiment control surfaces

Deliverables:

1. generic reusable actions for theory create/import/assert/diff/rollback/promote,
2. experiment observation, verdict, and learning-signal actions,
3. theory TTL and garbage-collection mechanisms.

Exit criteria:

1. no task-specific test orchestration code is required for the first real Testing Workflows.

### Phase 3: First real Testing Workflows

Deliverables:

1. a meeting-invitation testing workflow,
2. a synthetic-data workflow-regression test workflow,
3. a promotion-gate workflow,
4. initial replay and debug retention policy.

Exit criteria:

1. at least two example scenarios run end to end with structured evidence.

### Phase 4: Benchmark-world integration

Deliverables:

1. clean escalation from Tier 1 to Tier 2,
2. suite-level reporting across benchmark worlds,
3. heavyweight verification path for destructive workflows.

Exit criteria:

1. workflows that need stronger realism can be tested without inventing a separate framework.

### Phase 5: Learning-loop integration

Deliverables:

1. experiment-run signals feed workflow selection experience,
2. dashboards or experiment summaries,
3. policy-controlled recommendations from experiment outcomes.

Exit criteria:

1. experiment outputs are usable beyond human inspection.

### Phase 6: Bounded self-experimentation

Deliverables:

1. policy for allowed self-experiment targets,
2. gated workflow and policy comparisons,
3. explicit non-promotion default for self-experiment outcomes.

Exit criteria:

1. Von can compare its own workflow or routing strategies safely without silently self-modifying.

## 12. Recommended First Implementation Tasks

These are the first implementation tasks I would create after this planning task.

1. define the ontology vocabulary for testing theories and experiment runs,
2. implement a minimal Tier 1 theory-slice control surface,
3. implement `experiment_run.v1` evidence capture parallel to turn-execution records,
4. materialise a meeting-invitation testing workflow,
5. materialise a synthetic-data workflow-regression suite workflow,
6. add a promotion-gate workflow for validated outcomes,
7. integrate retained failing cases with replay and benchmark dashboards.

## 13. Hard Open Questions

These are real unresolved questions:

1. should ephemeral theories be persisted TTL concepts, a separate slice store, or both,
2. what is the canonical unit of assertion inside a testing theory,
3. how should theory inclusion compose with namespace and future access control,
4. what counts as safe self-experimentation versus unsafe self-modification,
5. should experiment promotion be able to publish workflows as well as knowledge,
6. how reversible must promotion be,
7. should theory-local execution be able to call all normal tools or only a safe subset,
8. how much of verdicting should be deterministic versus LLM-judged,
9. should experiment runs live primarily in Vontology, Mongo projections, or both,
10. how should the system decide when Tier 1 is insufficient and escalation to Tier 2 is required.

## 14. Literature Notes

The earlier exploration found adjacent work rather than a direct precedent.

Closest adjacent sources:

1. Agent Laboratory: <https://agentlaboratory.github.io/> and <https://arxiv.org/pdf/2501.04227>
2. AgentRxiv: <https://agentrxiv.github.io/> and <https://arxiv.org/html/2503.18102v1>
3. A Self-Improving Coding Agent: <https://arxiv.org/abs/2504.15228>
4. MLR-Bench: <https://github.com/chchenhui/mlrbench> and <https://neurips.cc/virtual/2025/poster/121719>
5. SUPER: <https://arxiv.org/abs/2409.07440> and <https://aclanthology.org/2024.emnlp-main.702.pdf>
6. ML-Bench: <https://ml-bench.github.io/> and <https://openreview.net/pdf?id=T2mtCFKIEG>

Classical contextual-knowledge pointer:

1. microtheory discussion poster: <http://www.cogsys.org/posters/2018/poster-2018-5.pdf>

Current conclusion:

1. there is clear adjacent work on autonomous research, benchmarked self-improvement, and sandboxed evaluation,
2. there is classical context-theory work,
3. the exact combination of ontology-backed provisional theories, durable workflow execution, explicit promotion, and bounded self-experimentation still appears underexplored.

## 15. Literature Review Plan For Follow-up

If a deeper literature review is needed, the next non-coding pass should search:

1. `LLM agent self-improvement benchmark`
2. `safe self-modification agents`
3. `microtheory knowledge representation context inclusion`
4. `sandboxed agent evaluation workflow`
5. `knowledge graph experiment provenance`
6. `autonomous research agent evaluation`

Databases:

1. arXiv
2. ACL Anthology
3. OpenReview
4. NeurIPS proceedings
5. Semantic Scholar
6. Google Scholar

For each paper, extract:

1. problem addressed,
2. experiment model,
3. isolation mechanism,
4. whether self-experimentation is involved,
5. whether knowledge representation is ontology-backed,
6. direct relevance to Von.

## 16. Bottom Line

Von is now in a strong position to build a real Testing Workflows framework.

The workflow substrate, routing authority, and heavy benchmark harness are already present. The main missing work is to make theory and experiment structure first-class, add reusable theory and experiment actions, and materialise the first real Testing Workflows around concrete scenarios such as meeting invitations and synthetic workflow-regression suites.

If implemented cleanly, this framework would not be a side utility. It would become part of how Von safely develops, validates, compares, and eventually improves its own behaviour.
