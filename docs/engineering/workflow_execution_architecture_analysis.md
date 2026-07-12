# Von Workflow Execution Architecture: Intended Design vs Current Reality

> **Document status: Dated diagnostic snapshot; not a current implementation
> guide.** Present-tense claims, source line numbers, workflow inventories, and
> Jira conclusions describe evidence available on 7 April 2026. Architectural
> questions may remain useful, but revalidate every factual claim against
> current code, live Vontology, telemetry, and Jira before implementation.

- **Kind:** Diagnostic analysis
- **Lifecycle:** Frozen
- **Authority:** Evidence only; not a specification
- **State as of:** 2026-04-07
- **Author:** Analysis prepared for Michael Witbrock
- **Trigger:** Observed failure-to-communicate in a live arXiv paper ingestion
  turn (session `d57ab88b`, request `51a65483`), where the system silently
  created a file copy and a paper concept without telling the user

---

## 1. Executive Summary

Von's intended execution architecture is a layered, LLM-supervised, KB-authoritative workflow system. The user's chat message enters a conversation turn workflow; an LLM analyses the intent and selects an appropriate VWL workflow from Vontology; that workflow is instantiated and executed step by step, with each step guided by the Vontology-authored workflow definition; and within each step the system may use LLM reasoning, MCP tool calls, or (in the future) skill invocations. At every layer, the LLM remains a reasoning participant, able to narrate, explain, and communicate what is happening.

The current implementation subverts this vision in several important ways. At the orchestrator level, a dispatch decision (line 22306 of `orchestrator.py`) silently promotes discovered workflows to a "custom workflow" execution mode, handing them off to a deterministic Python state machine that runs the entire workflow without LLM supervision between steps. The result is that whole categories of user-facing work — paper ingestion, entity representation, talk/meeting materialisation — execute as opaque Python pipelines. The user sees a narration of the *output* but has no visibility into what the system did, no confirmation that concepts or file copies were created, and no opportunity to guide the process.

This paper documents the intended design hierarchy, the current implementation reality, every identified point where Python monolithic code subverts the vision, and what the architectural consequences are.

---

## 2. The Intended Execution Hierarchy

The intended architecture is a five-layer hierarchy. Each layer has a clear role, and the LLM is present at the top level and available within any layer that requires reasoning.

### Layer 1: Chat Turn Workflow

The top-level construct. A user message arrives at `/von/generate`. The system enters a KB-defined conversation turn workflow (`#V#conversation_turn_execution_workflow`). This workflow governs the entire turn lifecycle: context building, workflow discovery, dispatch, execution, response composition, narration.

**Authority**: This workflow is defined in Vontology and represented as a VWL workflow concept. Its stages and transitions are authored KB artefacts, not hardcoded Python logic.

### Layer 2: LLM Analysis and Dispatch

Within the conversation turn workflow, an LLM analyses the user's message. It may:

- **Answer directly** — when no workflow is needed (a question, a greeting, simple factual recall).
- **Select an initial workflow** — discover a matching VWL workflow from the capability index, evaluate its fitness, and dispatch to it.
- **Use tools directly** — invoke MCP tools without a formal workflow, for ad hoc one-off actions.
- **Invoke a skill** (future) — delegate to a reusable structured skill definition.

The key property of this layer is that the LLM is the decision-maker. It reasons about the user's intent, evaluates candidates, and chooses a path. This reasoning is visible in telemetry and can produce explanatory output to the user.

### Layer 3: Workflow Instantiation

When the LLM selects a workflow, that workflow is loaded from Vontology via the VWL loader (`vontology_loader.py`). The workflow definition — its steps, transitions, preconditions, effects, prompt contracts, action contracts — all come from the KB. The definition is compiled into an executable state machine (`WorkflowDefinition` with `WorkflowStateSpec` objects).

This instantiation should be visible. The user should know that a workflow was selected, what it is called, and what it intends to do.

### Layer 4: Step-by-Step Workflow Execution

Each step in the instantiated workflow executes according to its authored execution mode:

- **`llm` mode**: The step uses an LLM to reason, generate, or decide. The LLM receives the step's prompt contract (from Vontology) and the current workflow context. It may call tools, produce outputs, or branch the workflow.
- **`deterministic` mode**: The step executes a reliable, pre-defined action (e.g., a database write, an API call, a validation check). No LLM is needed because the operation is mechanically reliable.
- **`control` mode**: Flow control primitives — branching, looping (`for_each`), forking, joining.
- **`subworkflow` mode**: Delegates to a child workflow.

The critical design property: **the workflow graph (which steps, in what order, with what transitions) is authored in Vontology. The LLM does not choose the next step — the VWL definition does.** But within each step, the LLM may be the actor (for `llm`-mode steps), and between steps the system should be able to communicate progress.

### Layer 5: Tool and Skill Use Within Steps

Within any step (whether LLM-driven or via a registered action handler), the system may:

- Call MCP tools via the internal gateway.
- Use LLM generation for reasoning or text production.
- (Future) Invoke skill definitions for structured multi-step sub-procedures.

### Why This Hierarchy Is a Good Idea

This hierarchy is architecturally sound for several reasons:

1. **KB-authoritative workflow definitions** mean that the organisation's actual operational processes are represented explicitly, versionable, inspectable, and modifiable without changing Python code. New workflows can be authored, published, validated, and retired entirely through knowledge engineering.

2. **LLM supervision at the top** means the system can understand user intent in natural language, select appropriate workflows dynamically, handle edge cases, and communicate. The LLM is not replaced by the workflow — it *supervises* the workflow.

3. **Deterministic execution for reliable steps** means that once a workflow step is known to be mechanically reliable (e.g., "normalise this arXiv URL", "write this metadata to the database"), it does not need to waste an LLM call. The workflow definition distinguishes reasoning from routine.

4. **LLM-mode steps within workflows** mean that when a step genuinely requires reasoning (e.g., "assess whether this paper's metadata is complete", "decide whether to create a new author concept or link to an existing one"), the LLM can be brought in at exactly the right points, scoped to exactly the right prompt contract.

5. **Visibility and narration** are natural consequences. Because the LLM is present at the top level, it can narrate what the workflow did. Because the workflow steps are declared with names, descriptions, and output contracts, the system has the vocabulary to explain what happened.

6. **The separation of authored policy from execution support** is the fundamental AGENTS.md principle (`§4`). Workflows, prompts, routing profiles, and discovery exemplars are policy artefacts authored in Vontology. Python provides the execution engine, validators, telemetry, and tool implementations. When this separation holds, the system is genuinely a neuro-symbolic agentic architecture rather than a Python application with some metadata attached.

---

## 3. How It Currently Works: The Reality

### 3.1 The Custom Workflow Bypass

The single most consequential deviation from the intended architecture occurs at line 22306 of `orchestrator.py`:

```python
selector_requests_custom_workflow = bool(
    has_explicit_workflow_routing
    and selected_workflow_id_text
    and not selected_prefers_direct_response
    and not selected_uses_tool_pipeline_contract
)
```

When this flag is `True`, the orchestrator bypasses the LLM-driven tool-calling path entirely and dispatches directly to the workflow engine:

```python
# Line ~27708
if (
    selector_requests_custom_workflow
    and selected_workflow_id_text
    and not selected_uses_tool_pipeline_contract
):
    wf_result = self.execute_workflow(
        selected_workflow_id_text,
        data=_build_custom_workflow_dispatch_data(),
        ...
    )
```

This means: **any workflow that has a VWL definition with registered Python action handlers will be executed as a closed, deterministic pipeline without LLM participation between steps.**

The conditions for this bypass are broadly met. Any workflow that:
- was discovered by the capability index (which sets `has_explicit_workflow_routing`),
- has a non-empty concept ID (which sets `selected_workflow_id_text`),
- does not explicitly opt into tool-pipeline mode,
- does not explicitly prefer a direct response,

...will be dispatched as a custom workflow. This is the default path for all materialisation workflows (papers, entities, talks), evaluation workflows, and many utility workflows.

### 3.2 The Deterministic State Machine

Once dispatched as a custom workflow, execution enters `WorkflowExecutor.run()` in `engine.py` (line 1251). This executor is a **purely deterministic state machine**:

1. It starts at the workflow's initial state.
2. For each state, it executes all declared actions sequentially.
3. It evaluates transition conditions to determine the next state.
4. It loops until a terminal state or the maximum transition count.

**The executor never asks the LLM "what should I do next?"** The sequence is entirely pre-determined by the Vontology workflow definition. This is correct — the VWL manual specifies that workflow structure is authored, not improvised. But the consequence is that the *entire workflow* runs without any LLM-mediated communication to the user.

### 3.3 Registered Python Action Handlers

Each workflow step's action is resolved by looking up a registered Python handler in the `ActionRegistry`. The `_register_durable_action_modules()` function in `registry_factory.py` (line 1122) registers handlers from **20 workflow-family modules**:

| Module | Action IDs | LLM Usage |
|--------|-----------|-----------|
| `paper_representation_workflow.py` | 9 actions (`scholarly_paper.*`, `arxiv.*`) | None |
| `entity_representation_workflow.py` | 1+ actions | None |
| `talk_representation_workflow.py` | 3 actions (`talk.*`) | None |
| `entity_identity_resolution_workflow.py` | Multiple | None |
| `file_copy_typing_workflow.py` | 2 actions | None |
| `file_copy_upload_classification_workflow.py` | Multiple | None |
| `file_copy_upload_handler_workflow.py` | Multiple | None |
| `file_copy_interpretation_workflow.py` | Multiple | None |
| `jira_task_incremental_import_workflow.py` | 1 action | None |
| `jira_task_full_reconciliation_workflow.py` | Multiple | None |
| `rag_sync_workflow.py` | 4 actions | None |
| `workflow_creation_workflow.py` | Multiple | None (metadata tracks `llm_policy`) |
| `workflow_gap_recovery_workflow.py` | Multiple | None |
| `workflow_introspection_maintenance_workflow.py` | Multiple | None |
| `episode_evaluation_workflow.py` | Multiple | None |
| `parent_specificity_concept_dossier_workflow.py` | Multiple | None |
| `parent_specificity_rumination_workflow.py` | Multiple | None |
| `planning_workflow.py` | 4 actions | **Yes** — `planning.infer_plan` calls LLM |
| `enrichment_workflow.py` | 4 actions | **Yes** — `process_batch` calls LLM |
| `rumination_workflow.py` | Multiple | **Yes** — Tracks LLM call budget |

Of the 20 workflow families, **only 3 use LLM calls within their action handlers**. The other 17 are entirely deterministic Python code. This means the vast majority of custom-dispatched workflows run as pure Python pipelines with no LLM reasoning, no user communication, and no opportunity for the user to observe or guide the process.

### 3.4 The MCP Fallback Handler

There is a design escape hatch: `_durable_mcp_fallback_action()` in `registry_factory.py` (line 280). When a workflow step references an action ID that has no registered Python handler, it falls back to invoking the equivalent MCP tool through the gateway. This allows Vontology-authored workflows to reference MCP tools without explicit Python wiring.

This fallback is architecturally correct — it means that new workflow steps can be authored in Vontology and immediately executable by referencing existing MCP tools. But it is rarely exercised for the established workflow families, which all have dedicated Python handlers.

### 3.5 The Response Assembly Gap

After a custom workflow completes, the orchestrator attempts to extract user-visible text from the workflow result via `_render_custom_workflow_response_text()` (line 3773). This function looks for fields like `response_text`, `summary`, or `final_response` in the workflow's output data. It then optionally feeds this text through a narration workflow (`#V#chat_narration_workflow`) to produce spoken audio.

The problem: **the response extraction is a best-effort scrape of the workflow's output dictionary**. Most materialisation workflows do not produce a `response_text` field designed for the user. They produce structured data (concept IDs, metadata dictionaries, status flags). The narration stage receives whatever text was scraped and renders it for TTS. The *process* — what the workflow did, what concepts were created, what relationships were established — is lost.

In the observed failure: the arXiv paper representation workflow successfully created a file copy, a scholarly paper concept, author concepts, and metadata relationships. But the user was told only the paper's abstract. Nothing about the file copy. Nothing about the concept. Nothing about the authors. The workflow succeeded technically but failed communicatively.

### 3.6 The Bootstrap Pattern

Each monolithic workflow family has a corresponding `*_workflow_vontology_service.py` file that bootstraps its VWL definition into Vontology at startup. For example, `paper_representation_workflow_vontology_service.py` calls `bootstrap_repo_seed_workflow_bundle()` with a JSON seed bundle. These services are explicitly documented as "startup-only seed publication/repair entry points" (§2.2 of the VWL manual), but their practical effect is to ensure that the monolithic Python-handled workflows are always present in the capability index and always discoverable.

The seed bundles contain the complete VWL workflow definitions (steps, transitions, action contracts, discovery exemplars, routing profiles). The definitions are structurally correct — they are genuine VWL workflows authored in Vontology-native graph structures. The problem is not the definitions. The problem is that every step in these definitions is bound to a registered Python handler through `invokesAction` directives, and every handler is a deterministic Python function, so the resulting execution is a closed pipeline.

---

## 4. Catalogue of Subversions

The following is an exhaustive list of code-level patterns that subvert the intended KB-authoritative, LLM-supervised workflow architecture.

### 4.1 The `selector_requests_custom_workflow` Flag (Orchestrator)

**Location**: `orchestrator.py`, line 22306-22312

**What it does**: Any discovered workflow with a routing match is promoted to custom (deterministic) dispatch by default, unless it explicitly opts into `tool_pipeline` mode or `direct_response` mode.

**Why this subverts the vision**: The intended hierarchy has the LLM as a supervisory participant throughout execution. This flag creates a binary fork where the LLM is either fully in control (tool-calling mode) or entirely absent (custom workflow mode). There is no middle ground — no "LLM-supervised workflow execution" path.

**What should happen**: The dispatch decision should not be a binary choice between "LLM does everything" and "Python does everything". A discovered workflow should be instantiated and executed step-by-step with the LLM as the narrator and communicator, able to report what each step did, even when the steps themselves are deterministic.

### 4.2 Monolithic Action Handlers Without Communication

**Location**: All 17 deterministic workflow families in `src/backend/workflows/durable/`

**What they do**: Each handler is a pure Python function that performs a data operation (normalise inputs, materialise concept, enrich metadata, verify postconditions) and returns a result dictionary. None of these handlers emit any user-facing text, progress indication, or explanatory message.

**Why this subverts the vision**: In the intended hierarchy, even deterministic steps exist within a workflow that the LLM supervises. The LLM should be able to say "I'm normalising the arXiv identifier", "I've created a scholarly paper concept", "I'm resolving author identities". The current handlers are invisible operations.

**What should happen**: Either (a) action handlers should produce structured result payloads that include human-readable summaries of what was done, which the orchestrator can surface to the user; or (b) the workflow execution engine should emit step-completion events that the top-level LLM can narrate; or (c) LLM-mode wrapper steps should bracket each deterministic step to provide before/after communication.

### 4.3 The `execute_workflow()` Method as a Black Box

**Location**: `orchestrator.py`, line 18741

**What it does**: Calls `WorkflowExecutor.run()` and waits for the entire workflow to complete, then extracts a response from the result.

**Why this subverts the vision**: The workflow runs as a single synchronous call from the orchestrator's perspective. There is no streaming of intermediate results, no per-step progress events visible to the user, and no opportunity for the LLM to interject explanations between steps.

**What should happen**: Workflow execution should be observable from the top level. Each step completion should produce a progress event that the orchestrator can forward to the user (either as streaming text, as structured progress indicators, or as queued narration points).

### 4.4 `_render_custom_workflow_response_text()` as Emergency Scraping

**Location**: `orchestrator.py`, line 3773-3850

**What it does**: After a workflow completes, it attempts to find human-readable text in the result dictionary by checking a prioritised list of field names: `response_text`, `summary`, `final_response`, then various fallback structures.

**Why this subverts the vision**: This function exists because the custom workflow path does not produce user-facing communication as a first-class output. It scrapes whatever the workflow left behind. For paper representation, it finds the paper's abstract (because the workflow stores metadata including a summary field). It does not find anything about what the workflow *did* — the concepts created, the relationships established, the file copies stored.

**What should happen**: Workflow results should carry explicit, structured communication payloads: a list of "things I did" with human-readable descriptions, alongside any content outputs. The response assembly logic should surface both "what was done" and "what the result contains".

### 4.5 Narration as Paraphrase Rather Than Report

**Location**: `orchestrator.py`, narration action handlers (lines 1861-1884)

**What it does**: The narration workflow takes whatever text was extracted by `_render_custom_workflow_response_text()` and asks an LLM to reformulate it for text-to-speech delivery. The narration prompt instructs: "Use short, clear sentences. Prefer plain language. Avoid code, Markdown, URLs."

**Why this subverts the vision**: The narration stage receives only the scrapped output text — typically the paper abstract. It has no access to the workflow's execution trace, the steps that ran, or the concepts that were created. It cannot narrate "I've stored this paper in your knowledge base and created author concepts" because it never received that information.

**What should happen**: The narration stage should receive a structured execution summary alongside the content output, enabling it to compose a response like: "I've ingested DINO: Grounding Multimodal Large Language Models from arXiv. I created a paper concept, linked three authors, and stored the PDF. Here's what the paper is about: [abstract]."

### 4.6 The Startup Bootstrap Pipeline

**Location**: Six `*_workflow_vontology_service.py` files in `src/backend/services/`

**What they do**: At application startup, each service loads a JSON seed bundle and publishes the corresponding VWL workflow family into Vontology. This ensures the capability index can discover these workflows.

**Why this subverts the vision**: The bootstrapping itself is not inherently subversive — importing seed data is a valid startup operation. The subversion is that these bootstrap services ensure the perpetual availability of workflows whose entire step inventory is bound to monolithic Python handlers. The VWL definitions are structurally correct but functionally opaque: every step says `invokesAction: "scholarly_paper.normalise_inputs"`, and that action is implemented as a Python function that runs silently.

**What should happen**: Bootstrapped workflows should either (a) have their step execution modes reconsidered — some steps that are currently `deterministic` might benefit from `llm` mode or from a hybrid approach where the deterministic handler runs but an LLM wrapper narrates the result; or (b) the execution engine should provide a progress-reporting surface that makes deterministic execution visible regardless of whether the step uses an LLM.

### 4.7 Missing `execution_mode: "llm"` on Judgement Steps

**Location**: Various seed bundles and Vontology workflow definitions

**What happens**: Steps that involve genuine judgement (e.g., `arxiv.decide_acquisition_mode` — deciding whether to download a PDF) are implemented as deterministic Python handlers rather than as LLM-mode steps. The handlers contain conditional logic that makes the decision based on heuristics (does the file already exist? is the URL reachable?).

**Why this subverts the vision**: The VWL manual states (§4): "Prompt-bearing reasoning steps MUST compile as `llm` execution-mode steps." While these specific steps do not currently carry prompt contracts, they perform decisions that could benefit from LLM reasoning — and more importantly, from LLM narration ("I already have this paper, so I'll skip downloading it" vs "I need to download the PDF from arXiv").

### 4.8 No User-Facing Step Completion Events

**Location**: `engine.py`, `WorkflowExecutor.run()` (line 1251)

**What happens**: The executor loops through states and actions, recording results in the execution trace. But the trace is stored internally for telemetry and debugging. No events are emitted to the user-facing response stream.

**Why this subverts the vision**: Even in a purely deterministic workflow, the user should see something: a progress indicator, a step-by-step log, or a real-time narration. The current executor provides none of these. The user's only view of the workflow is whatever text is scraped from the final result dictionary.

---

## 5. The Observed Failure: A Case Study

In session `d57ab88b`, the user pasted `https://arxiv.org/abs/2411.04983` into the chat. Here is what happened:

1. **Workflow Discovery** (24.8 seconds): The capability index matched `#V#arxiv_paper_representation_workflow` with confidence 1.0.

2. **Dispatch Preparation** (88.9 seconds): Eight pre-dispatch steps completed, including a 75-second ontology preflight. The selector chose the arXiv workflow.

3. **Custom Workflow Dispatch**: `selector_requests_custom_workflow = True`. The orchestrator handed the URL to `execute_workflow()`.

4. **Deterministic Execution**: The paper representation workflow ran through its steps:
   - `arxiv.normalise_source` — extracted the arXiv ID.
   - `arxiv.decide_acquisition_mode` — determined whether to download.
   - Downloaded the PDF (via subworkflow or inline).
   - `scholarly_paper.materialise_from_file_copy` — created the paper concept.
   - `scholarly_paper.enrich_from_metadata` — fetched and attached arXiv metadata.
   - `scholarly_paper.resolve_authors` — created/linked author concepts.
   - `scholarly_paper.verify_representation` — validated postconditions.

5. **Response Assembly**: `_render_custom_workflow_response_text()` found a `summary` field in the result — the paper's abstract.

6. **Narration**: The narration workflow paraphrased the abstract for TTS.

7. **User Experience**: The user heard a summary of the paper's abstract. **No mention of**:
   - A file copy being created.
   - A paper concept being created in Vontology.
   - Author concepts being created or linked.
   - The paper being added to the knowledge base.
   - Any persistent action having occurred.

The user reasonably concluded that nothing structural had happened — that Von had merely read the arXiv page and summarised it. This is the direct consequence of the custom workflow bypass: the LLM was not present during execution, so it could not report what changed.

---

## 6. The Warnings Explained

The telemetry also surfaced three warnings:

### Warning 1: `Workflow '#V#conversation_turn_execution_workflow' is not runnable; instance was not created.`

`#V#conversation_turn_execution_workflow` is a real VWL workflow concept in Vontology. It is defined in the seed bundle (line 1487 of `canonical_workflow_publication_seed_bundle.json`) and used to model the conversation turn lifecycle. However, the orchestrator's preflight engine currently treats it as a concept to potentially instantiate as a runnable workflow, discovers that it lacks concrete backend bindings for all its stages, and emits this warning.

**Root cause**: The conversation turn workflow is structurally defined in Vontology but its execution is actually hardcoded in the orchestrator's Python method sequence. The VWL definition exists as a planning/telemetry artefact, not as an actually-executable workflow. This is itself a manifestation of the problem: the top-level chat turn execution is Python-native rather than VWL-driven.

### Warning 2: `Workflow '#V#arxiv_paper_representation_workflow' could not start because required launch inputs were unresolved: prompt.`

The arXiv workflow's launch contract (defined in the seed bundle) declares `prompt` as a required input. During one of the orchestrator's parallel preflight probes, the system attempted to check launchability but failed to map the raw user message to the `prompt` variable. A later mechanism (the dispatch data builder at `_build_custom_workflow_dispatch_data()`) resolves this mapping correctly and the workflow launches fine.

**Root cause**: The launch-input resolution logic has two code paths that run at different times. The early probe fails; the later dispatch succeeds. The warning is harmless but noisy — it indicates a preflight/dispatch asymmetry in the input resolution pipeline.

### Warning 3: `Workflow '#V#chat_buttonify_workflow' is not runnable; instance was not created.`

`#V#chat_buttonify_workflow` is a post-processing workflow concept that converts text elements into interactive UI buttons. Like `#V#conversation_turn_execution_workflow`, it is defined as a Vontology concept but its execution is embedded in the orchestrator's Python code rather than expressed as a runnable VWL program.

**Root cause**: Same pattern as Warning 1 — VWL concepts exist in Vontology as planning/telemetry artefacts, but their actual execution is hardcoded Python.

---

## 7. The Gap Between Vision and Code

### 7.1 What the vision promises

The AGENTS.md doctrine (§11) states:

> Von is not mainly a Python application with some prompts attached. It is a neuro-symbolic agentic system in which enduring knowledge matters, workflow and prompt authority matter, model portfolios and learned policy matter, explicit representation matters, evaluation and observability matter, and Python exists to support those things rather than replace them.

The VWL manual (§2) states:

> Backend code SHOULD primarily supply reusable actions, validators, loaders, and telemetry for workflows, rather than embedding task-specific orchestration that VWL could already express.

### 7.2 What the code actually does

The code provides two execution paths:

1. **Tool-calling path** (LLM-driven): The LLM is fully in control. It receives the user message, reasons about what to do, calls MCP tools one at a time, narrates its progress, and composes a response. This path satisfies the vision but does not leverage authored VWL workflows — it is pure LLM improvisation.

2. **Custom workflow path** (Python-driven): A VWL workflow is loaded from Vontology and executed by the deterministic state machine. Python action handlers do the work. The LLM is entirely absent from execution. After the workflow finishes, a response is scraped from the output. This path leverages VWL definitions for *structure* but not for *communication* or *LLM supervision*.

Neither path implements the intended hierarchy. The vision calls for a third path:

3. **Supervised workflow path** (intended but unimplemented): A VWL workflow is loaded from Vontology and executed step by step. The LLM supervises the execution — it can narrate each step, communicate with the user, and reason when steps require judgement. Deterministic steps run their handlers but emit structured results that the LLM can report. LLM-mode steps use prompt contracts from Vontology. The user sees the workflow in action.

### 7.3 Why the gap exists

The custom workflow path was likely built as a reliability optimisation. Paper ingestion, entity materialisation, and similar operations require a specific sequence of steps that must happen reliably, in order, with proper error handling. Delegating this to an LLM — which might hallucinate steps, skip steps, or botch the sequence — was risky. Building deterministic handlers and a state machine was the safe engineering choice.

This reasoning was locally correct. LLM tool-calling is unreliable for complex multi-step sequences. The problem is that the solution was *too* complete: it removed the LLM entirely rather than using the LLM as a supervisor over the deterministic execution.

### 7.4 What the gap costs

1. **User trust**: The user does not know what Von did. Silently creating concepts and file copies without reporting them damages the user's ability to verify, correct, or build on the system's actions.

2. **Minimal imposition violation**: The minimal imposition principle (§6.2) requires exposing uncertainty and making actions visible. A silent pipeline is maximally opaque.

3. **Observability debt**: The telemetry records `dispatch_zero_execution: true` and `tools_completed: 0`, creating the misleading impression that the workflow did nothing. In reality, it performed substantial mutations — they just did not occur through the MCP tool layer.

4. **Workflow authority erosion**: Because the custom workflow path "works" silently, there is no pressure to improve it. The Python handlers accumulate, new workflow families copy the pattern, and the proportion of user-facing work that runs as opaque pipelines grows.

---

## 8. Structural Fixes Required

This paper does not prescribe code changes, but identifies the architectural capabilities that are missing.

### 8.1 Step-level progress emission

The workflow executor (`WorkflowExecutor.run()`) should emit structured step-completion events to a progress channel that the orchestrator can subscribe to. Each event should carry: step name, action ID, execution mode, human-readable summary of what was done, and key outputs.

### 8.2 Workflow execution summary as a first-class result field

Workflow results should include a `execution_summary` field: an ordered list of completed steps with their names, descriptions, and salient outputs. This replaces the current scraping approach.

### 8.3 LLM-supervised dispatch mode

The orchestrator should support a third dispatch mode: "supervised workflow". In this mode, the workflow executes step by step (deterministic steps run their handlers), but after each step the orchestrator feeds the step result to the LLM for narration and optional user communication. This preserves the reliability of deterministic execution while restoring LLM participation.

### 8.4 Conversation turn workflow as an executable VWL program

`#V#conversation_turn_execution_workflow` should be a genuinely executable VWL workflow, not just a telemetry label. The top-level chat turn logic in the orchestrator should be a VWL execution, with the current Python method sequence refactored into VWL steps with action handlers.

### 8.5 Audit of execution modes on judgement steps

Steps like `arxiv.decide_acquisition_mode` that make substantive decisions should be audited for whether they are correctly marked as `deterministic` or should be `llm`-mode steps with prompt contracts.

---

## 9. Conclusion

Von's workflow architecture has the right conceptual design. The five-layer hierarchy — chat turn, LLM dispatch, workflow instantiation, step-by-step execution, and within-step tool/LLM use — is well motivated and well represented in the VWL manual and AGENTS.md doctrine. The VWL language, the Vontology-native workflow definitions, the condition language, the publication lifecycle, the routing profiles, and the discovery exemplar system are genuinely sophisticated and well-engineered authority surfaces.

The problem is that the execution layer short-circuits the hierarchy. The custom workflow dispatch path converts authored, KB-authoritative workflow definitions into opaque Python pipelines by removing the LLM from the execution loop. The result is a system where the knowledge engineering is correct but the user experience is that of a silent, unexplained backend process.

The fix is not to remove the deterministic execution capability — that capability is valuable for reliability. The fix is to restore LLM supervision *over* deterministic execution, so that the user sees what the system did, why, and what changed. The workflow definitions already carry the names, descriptions, and contracts needed for this narration. The infrastructure to deliver it — step-level progress events, execution summaries, supervised dispatch — needs to be built.
