# Chat-Turn Workflow Control Plane Analysis

> **Document status: Dated diagnostic snapshot; not a current implementation
> guide.** Present-tense implementation claims and source line numbers describe
> evidence available on 7 April 2026. Revalidate them against current code,
> live Vontology, telemetry, and Jira before acting.

- **Kind:** Diagnostic analysis
- **Lifecycle:** Frozen
- **Authority:** Evidence only; not a specification
- **State as of:** 2026-04-07
- **Author:** Analysis prepared for Michael Witbrock
- **Scope:** Intended chat-turn and workflow hierarchy versus the implementation
  observed at that date

---

## 1. Abstract

Von's intended architecture is a layered neuro-symbolic control system in which the top-level chat turn is interpreted by an LLM, durable task policy is carried by KB-stored workflows, and tools and future skills sit beneath those workflows as subordinate execution surfaces. That doctrine is explicit in `AGENTS.md` and in the VWL manual: workflow, prompt, KB, and Vontology artefacts should hold durable policy, while Python should primarily provide reusable execution, validation, tooling, and telemetry support.

The current codebase only partially realises that vision. Workflow-definition authority has moved significantly toward Vontology, and the arXiv path is genuinely represented as a KB-authored stepwise workflow rather than as one giant hidden Python workflow. However, the actual control plane for a user turn remains substantially Python-led. Route-level code still owns the top-level turn lifecycle. The orchestrator still decides routing, execution mode, launchability, override policy, and response rendering in Python. Presenter backfill layers still transform the final user-visible response after workflow execution. Semantically heavy Python domain logic still performs important represented-state mutations beneath workflow steps.

The result is an architecture that looks workflow-centred in ontology and telemetry, but still behaves route-centred in execution control and renderer-centred in user experience. This document describes the intended hierarchy, why it is a good idea, what is already correct in the current implementation, and the concrete Python control surfaces that still subvert the design.

---

## 2. The Intended Hierarchy

The intended execution hierarchy is:

1. `CHAT TURN WORKFLOW`
2. `LLM analysis`
3. `Initial dispatch`
4. `Workflow instantiation`
5. `Step-by-step workflow execution under KB-stored VWL definition`
6. `LLM use, tool use, and future skill use inside workflow steps`

This hierarchy is the right one for Von.

At the top level, the user is not talking directly to an inert workflow engine. The user is talking to an LLM-supervised assistant that interprets intent, decides whether a direct answer is appropriate, whether a workflow should be selected, whether tools can be used directly, and eventually whether a future skill should be invoked. That top-level layer is conversational, adaptive, and context-sensitive.

Beneath that, durable policy is supposed to move into workflows. Once a workflow has been selected, the workflow definition should govern the step graph, branching rules, contracts, and postconditions. The LLM should still be usable within steps, but the workflow should determine what kind of step is being executed and what comes next. This gives Von explicit state, inspectable policy, resumability, postcondition checking, and a stable surface for revision and evaluation.

Below workflows sit tools and future skills. These are subordinate execution capabilities, not the main carrier of durable task policy. They should be invoked within the semantic frame of the workflow, rather than replacing workflow authority.

This is consistent with the repo's governing doctrine:

- `AGENTS.md` says workflow-first and KB-authoritative is the default doctrine, and durable policy should live in workflow, prompt, KB, or Vontology artefacts rather than Python.
- `AGENTS.md` also says Python should usually provide reusable support surfaces rather than hidden task policy.
- `docs/engineering/von_workflow_language_manual.md` says Vontology workflow graphs, mappings, schedules, and event bindings are first-class implementation artefacts, and backend code should primarily supply reusable actions, validators, loaders, and telemetry rather than task-specific orchestration that VWL can already express.

---

## 3. Why This Hierarchy Is a Good Idea

This architecture is good for five main reasons.

### 3.1 It keeps the LLM where it is strongest

The LLM belongs at the interpretive and reasoning layers: understanding user intent, deciding whether a workflow is needed, choosing among workflow candidates, handling ambiguity, and reasoning inside those workflow steps that are genuinely open-ended. That is where the model's flexibility is valuable.

### 3.2 It keeps durable policy explicit and inspectable

Workflow graphs, prompt contracts, action contracts, context mappings, completion gates, and postcondition checks are not incidental implementation details. They are operational policy. Storing them in Vontology makes them inspectable, revisable, evaluable, and versionable in a way that Python conditionals are not.

### 3.3 It enables a properly neuro-symbolic architecture

The symbolic layer provides explicit state, typed entities, graph structure, durable memory, branching rules, and validation. The LLM provides interpretation, synthesis, planning, and conversational explanation. The combination is better than either alone.

### 3.4 It improves observability and truthfulness

If the workflow is the true control surface, the system can explain what workflow it selected, what step it is on, what tool it used, what artefact it created, and what still remains uncertain. The user should not have to infer structural state changes from telemetry after the fact.

### 3.5 It reduces long-term technical debt

When domain policy is encoded in Python orchestration paths, it becomes difficult to inspect, evaluate, and evolve. When policy sits in KB-authored workflows and prompts, Python can remain a reusable substrate rather than becoming the hidden home of business semantics.

---

## 4. What the Current Code Gets Right

It would be incorrect to say that the system is simply ignoring workflows or that all workflow logic still lives in Python.

### 4.1 Production workflow definition authority is moving toward Vontology

`src/backend/workflows/durable/registry_factory.py` explicitly says Python-defined workflows are no longer registered onto the production registry path:

- `_register_python_defined_workflows(...)` is effectively a no-op for production runtime registration.

This matters. It means the current production registry is not supposed to treat Python-authored workflow definitions as the authoritative workflow source.

### 4.2 The stage model is already derived from workflow metadata

`src/backend/workflows/conversation_turn_stage_model.py` says formal stage entries are derived from the authoritative workflow registry so state-to-stage mapping follows Vontology-authored workflow definitions rather than a duplicated static state catalogue.

That is exactly the right direction.

### 4.3 The tool-calling workflow is represented as a real workflow

In `src/backend/workflows/repo_seed_bundles/canonical_workflow_publication_seed_bundle.json`, `#V#tool_calling_workflow` is represented as:

- `tool_calling.preflight_requirements`
- `tool_calling.respond`
- `turn_execution.critic`
- `turn_execution.completion_gate`

This is aligned with the intended layered design: an LLM/tool step embedded inside a workflow, followed by critic and completion-gate machinery.

### 4.4 The arXiv paper path is genuinely a workflow, not one hidden monolith

The strongest inaccurate reading would be: "the system bypassed the workflow layer and ran one hard-coded Python arXiv ingestion pipeline directly." That reading is too strong.

The represented workflow `#V#arxiv_paper_representation_workflow` in `src/backend/workflows/repo_seed_bundles/paper_representation_workflow_seed_bundle.json` is a real stepwise workflow. It includes:

- `arxiv.normalise_source`
- `get_paper_metadata`
- `arxiv.decide_acquisition_mode`
- `finalise_cached_paper`
- `download_paper`
- `workflow_invoke_subworkflow` to `#V#scholarly_paper_representation_workflow`
- `scholarly_paper.verify_representation`

So the workflow layer is real here.

---

## 5. The Central Architectural Failure

The main problem is not that workflows do not exist. The main problem is that the top-level chat turn is still not actually controlled by the chat-turn workflow.

### 5.1 The route still owns the turn

In `src/backend/server/routes/von_routes.py`:

- `_submit_conversation_turn_instance()` submits a durable instance for `#V#conversation_turn_execution_workflow`.
- But immediately afterwards, the route calls `orchestrator.run(...)`.
- Later, `_finalise_conversation_turn_instance(...)` persists runtime payloads and marks the instance completed or failed.

This means the conversation-turn workflow instance is currently more like sidecar telemetry and post-hoc state capture than the real control plane of the request.

### 5.2 The represented conversation-turn workflow is currently thin and transitional

`docs/engineering/turn_execution_completion_schema_proposal.md` explicitly says the intended responsibilities of `#V#conversation_turn_execution_workflow` are:

- derive required effects
- execute tool plan
- hand off to critic workflow
- invoke completion gate workflow

But the same document also says:

- do not bind these new workflows yet
- only migrate `message.direct_created` after executable definitions and parity tests pass

That transitional state is visible in the current seed bundle. In `canonical_workflow_publication_seed_bundle.json`, the current `#V#conversation_turn_execution_workflow` is effectively just:

- `turn_execution.critic`
- `turn_execution.completion_gate`
- `completed`

So the intended top-level controller exists conceptually, but the real request path is still largely living in route and orchestrator code.

---

## 6. Where Python Still Subverts the Intended Design

Not all Python here is wrong. Reusable action handlers, validators, loaders, telemetry surfaces, and deterministic wrappers are legitimate support surfaces. The problem is specifically that Python is still carrying too much control policy and too much domain semantics.

### 6.1 Python still decides routing and execution mode

In `src/backend/integrations/internal_mcp/orchestrator.py`, the orchestrator computes:

- whether the selected workflow uses the tool-pipeline contract
- whether it uses the narration contract
- whether it prefers direct response
- whether the selector is requesting a custom workflow

This is all control-plane policy in Python.

That means the top-level turn is not being governed by an authored chat-turn workflow. It is being governed by Python logic that inspects workflow metadata and makes dispatch decisions before any durable top-level workflow instance truly owns the request.

### 6.2 Python still decides launchability and override policy

The orchestrator also:

- builds custom-workflow dispatch data
- probes custom-workflow launchability
- evaluates override policy
- may replace the selected workflow based on launchability and override rules

This is a major subversion of the intended architecture. The question of whether a workflow should remain selected, be overridden, or fall through to another execution mode is currently being decided by Python control logic, not by the authored top-level workflow or by an explicit represented routing policy surface that fully governs the turn lifecycle.

### 6.3 Workflow execution is still an in-process Python call

`orchestrator.execute_workflow(...)` ultimately hands the workflow definition to `self._workflow_executor.run(...)`.

That is not inherently wrong; some execution engine must exist somewhere. But it matters architecturally because the durable workflow instance is not yet the real owner of the user turn. The durable instance is created, but the effective control plane is still "route calls orchestrator, orchestrator calls executor" rather than "top-level workflow instance governs the entire turn and all subordinate workflow invocations".

### 6.4 Python still renders the user-visible response after workflow execution

This is one of the most important practical problems.

After a custom workflow completes, the orchestrator calls `_render_custom_workflow_response_text(...)`. That renderer mainly looks for:

- `response_text`
- `summary`
- `final_response`
- `verdict`
- experiment-style metadata

If none of those are present, it falls back to generic status text.

Crucially, it does not generically surface durable artefacts such as:

- `file_copy_concept_id`
- `paper_concept_id`
- other created or materialised concept identifiers

So the workflow may have done exactly the right represented-state work, but the renderer still collapses the result into an opaque summary.

### 6.5 The system already knows about workflow side-effects, but only in telemetry

The same orchestrator file contains logic to:

- collect durable workflow side-effects
- count them
- include them in workflow execution summaries

So the system is already capable of recognising that a workflow created or materialised durable artefacts. The failure is not ignorance. The failure is that this information is not treated as a first-class user-visible outcome on the main response path.

### 6.6 Presenter backfill layers further detach visible output from actual workflow events

In `von_routes.py`, after orchestrator execution the route applies:

- screen backfill modes
- narration workflow invocation
- buttonify workflow invocation

These are not necessarily bad capabilities. But because they are layered on after the main workflow execution, they can further separate what the user sees and hears from what the workflow actually did.

This helps explain the observed failure mode: structurally important side-effects occurred, but the user got a polished summary rather than an operationally faithful account.

---

## 7. The arXiv Turn: What Actually Happened

The arXiv turn should be described precisely.

### 7.1 What is false

It is false that the system simply bypassed the KB-stored workflow layer and ran one giant hidden "native Python arXiv ingestion workflow" instead.

The represented workflow is real, and its step graph is real.

### 7.2 What is true

It is true that the workflow's steps bottom out in dedicated Python handlers and tool implementations.

In `src/backend/workflows/durable/paper_representation_workflow.py`, there are dedicated action handlers for:

- `scholarly_paper.normalise_inputs`
- `scholarly_paper.materialise_from_file_copy`
- `scholarly_paper.enrich_from_metadata`
- `scholarly_paper.resolve_authors`
- `scholarly_paper.verify_representation`
- `arxiv.normalise_source`
- `arxiv.decide_acquisition_mode`

These are registered as reusable actions in the workflow action registry.

In `src/backend/integrations/internal_mcp/catalogue.py`, the arXiv acquisition tools are also concrete Python handlers:

- `_get_paper_metadata`
- `_download_paper`
- `_finalise_cached_paper`

So the accurate description is:

- the workflow layer was used
- but several of the workflow steps are implemented by dedicated Python domain logic
- and the user-facing response path subsequently hid the workflow's side-effects

### 7.3 Why this still subverts the intended design

Some of this Python is legitimate reusable support. For example:

- extracting a canonical arXiv ID from inputs
- inspecting cache state
- wrapping an MCP tool
- validating representation postconditions

Those can reasonably be seen as reusable deterministic primitives beneath the workflow.

But other parts are semantically heavy. In `src/backend/services/arxiv_paper_link_service.py`, the scholarly-paper materialisation helpers:

- create and type concepts
- attach names and descriptions
- assert publication dates
- create or resolve author concepts
- create or resolve topic concepts
- link file copies to papers
- verify the resulting representation
- request recommendation-refresh downstream effects

This is more than a thin execution primitive. It is domain semantics. The helper even describes itself as "intentionally deterministic and tool-free so workflow-driven upload pipelines can assert strong postconditions without relying on chat heuristics".

That determinism is understandable. But architecturally it means that semantically rich represented-state construction still lives substantially in Python. The workflow orchestrates the sequence, but Python still carries much of the actual scholarly-paper semantics.

### 7.4 There is also direct repository mutation below the service layer

Some of the arXiv paper link code drops to repository-level mutation via `ConceptsRepository.mutate_relationship_edge(...)`.

Given the project's constitutional guidance to avoid direct DB access for Vontology-governed data, this is a particularly strong example of Python still carrying represented-state authority more directly than the intended architecture wants.

---

## 8. Why the User Was Not Told About the File Copy or Paper Concept

The best explanation is simple.

The workflow did real represented-state work. The telemetry path could detect that work. But the main user-visible output path did not treat those durable state changes as mandatory facts to surface to the user.

Instead, the system:

1. executed the selected workflow
2. obtained a workflow result
3. rendered that result into a compact text response
4. then further transformed the visible output through screen/narration presentation layers

Because `_render_custom_workflow_response_text(...)` does not generically surface durable artefact creation, the user got a content summary rather than an operationally faithful explanation.

The system therefore looked successful in telemetry while still failing the more important user-level truthfulness test: telling the user what durable state had been changed on their behalf.

---

## 9. What `dispatch_zero_execution` Actually Means

`dispatch_zero_execution` should not be overread.

The presence of a zero-tool-execution flag does not mean that nothing happened, nor that no workflow ran. In the current telemetry code, when the selected execution mode is `custom_workflow`, the system explicitly treats zero top-level tool invocations as expected if the custom workflow handled the turn.

So `dispatch_zero_execution` in this context means something like:

- the turn was handled by a custom workflow path
- no top-level free-form tool-call phase was needed

It does not mean:

- there was no workflow execution
- or that the system merely fabricated a success state

The problem is not that the workflow did not run. The problem is that the user-visible response path did not faithfully describe what the workflow actually did.

---

## 10. What the Warnings Mean

The warnings observed in telemetry come from the verified workflow submission path.

`src/backend/workflows/durable/workflow_instance_submission_service.py` emits exact messages when:

- a workflow is not runnable
- launch-input resolution fails

That explains the observed warnings.

### 10.1 `#V#conversation_turn_execution_workflow` is not runnable

This is consistent with the repo's own transitional design state. The conversation-turn workflow is intended, but the design note explicitly says it should not yet be bound until executable parity is in place.

So the system currently has:

- conceptual and representational support for this workflow
- telemetry and submission attempts involving it
- but not yet a clean end-to-end top-level execution path in which it actually governs the user turn

### 10.2 `#V#arxiv_paper_representation_workflow` had unresolved `prompt`

This also makes sense from the published workflow contract. The arXiv paper representation workflow declares `prompt` as a required launch input in its launch contract.

So when the system attempted a verified submission or probe path without satisfying that contract in the expected form, the warning was truthful: the workflow could not start because required launch inputs were unresolved.

These warnings are therefore not meaningless noise. They are evidence of a system in a transitional mixed state where represented workflow concepts, durable submission, and real request-path control have not yet fully converged.

---

## 11. The Correct Overall Diagnosis

The correct diagnosis is not "Von is secretly just a Python application with fake workflows". That is too crude and not fair to the real progress already made.

The correct diagnosis is also not "the workflow vision is already fully realised and only the UI wording needs improvement". That is much too optimistic.

The accurate diagnosis is this:

- workflow-definition authority has moved significantly toward Vontology
- important execution paths, including the arXiv path, are genuinely represented as workflows
- but the top-level chat-turn control plane is still materially Python-led
- Python still decides routing, launchability, override policy, and response rendering
- semantically heavy domain mutation logic still lives beneath workflow steps in Python
- the presentation layer still suppresses user-visible reporting of durable side-effects

So the system is best understood as a transitional architecture moving toward the intended hierarchy, but still subverting that hierarchy whenever Python remains the hidden home of control policy, domain semantics, or user-visible explanation.

---

## 12. Bottom Line

Your hierarchy is the right one:

- top-level LLM handling the chat turn
- workflow instantiation beneath that
- step-by-step workflow execution guided by KB-stored workflow definitions
- tools and future skills lying beneath workflow steps

That architecture is worth defending because it is the right way to build an inspectable, revisable, neuro-symbolic assistant with durable knowledge and truthful observability.

The current code does not reject that vision. It only partially realises it.

What is already correct should be preserved:

- Vontology-authored workflow authority
- real VWL workflow definitions
- reusable deterministic action surfaces
- critic and completion-gate machinery

What remains architecturally wrong is that Python still sits above the workflow layer too often, still carries semantically rich represented-state logic too often, and still controls the user-facing explanation of workflow outcomes too strongly.

Until the chat-turn workflow becomes the true top-level control plane, and until workflow side-effects are surfaced as first-class user-visible facts rather than being hidden in telemetry, Von will continue to look more workflow-authoritative in its internal representations than it feels in actual user experience.
