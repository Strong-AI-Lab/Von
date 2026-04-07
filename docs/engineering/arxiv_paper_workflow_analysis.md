# Arxiv Paper Workflow: Comparative Architectural Analysis

**Status**: Comparative Analysis
**Date**: 2026-04-07
**Subject**: Analysis of `#V#arxiv_paper_representation_workflow` and the Von Control Plane
**Sources**: 
- [Doc A: Chat-Turn Workflow Control Plane Analysis](docs/engineering/chat_turn_workflow_control_plane_analysis.md)
- [Doc B: Workflow Execution Architecture Analysis](docs/engineering/workflow_execution_architecture_analysis.md)
- [Reference: Von Workflow Language Manual](docs/engineering/von_workflow_language_manual.md)

---

## 1. Executive Summary

This document synthesises a comparative analysis of two engineering papers examining Von's workflow-first architecture. Both papers agree that the system is in a transitional "neuro-symbolic" state where Vontology-authored workflows are structurally present but often subverted by legacy Python control logic. The primary failure mode identified is the **"communicative gap"**: the system performs successful represented-state mutations (creating concepts, linking authors) but fails to report these durable changes to the user because the response-rendering path collapses rich workflow telemetry into opaque summaries.

---

## 2. Core Points of Agreement

The two source documents reach a consensus on the following architectural foundations:

### 2.1 The 5-Layer Intended Hierarchy
Both documents endorse the same five-layer execution model as the correct vision for Von:
1. **Chat Turn Workflow**: The top-level lifecycle controller.
2. **LLM Analysis & Dispatch**: Intent interpretation and capability selection.
3. **Workflow Instantiation**: Loading authoritative VWL definitions from the KB.
4. **Step-by-Step Execution**: Managed by the VWL engine.
5. **Tool & Skill Use**: Subordinate execution surfaces within workflow steps.

### 2.2 Python's Support Role
There is total agreement that Python code should strictly provide **reusable execution support** (action handlers, validators, telemetry) rather than carrying "hidden" task policy or hardcoded orchestration that VWL could already express.

### 2.3 The arXiv Workflow Reality
Both documents confirm that `#V#arxiv_paper_representation_workflow` is a **genuine VWL workflow** in Vontology, not a monolithic Python script. However, they agree it currently functions as a "black box" because:
- Its steps bottom out in deterministic Python handlers.
- The LLM is effectively excluded from the execution loop once the workflow starts.

### 2.4 The Response Rendering Gap
Both papers identify the main user-facing failure as a **lack of operational truthfulness**. The system:
1. Executes the workflow successfully.
2. Detects side-effects (file copies, concept creation) in telemetry.
3. **Suppresses** these side-effects during response assembly, favouring a compact content summary (e.g., the paper's abstract) over an account of what was actually done.

---

## 3. Key Differences in Emphasis

While the documents agree on the "what," they differ slightly on the "why" and where the primary corrective pressure should be applied:

| Feature | Doc A (Control Plane Analysis) | Doc B (Execution Architecture) |
| :--- | :--- | :--- |
| **Primary Culprit** | **Lifecycle Ownership**: The Python route (`von_routes.py`) still owns the turn; the workflow is "sidecar" telemetry. | **Execution Bypass**: The `selector_requests_custom_workflow` flag in `orchestrator.py` skips LLM supervision entirely. |
| **Focus** | Top-down control and routing policy. | Bottom-up execution modes and "monolithic" action handlers. |
| **Root of Silence** | Layered presentation backfill (narration/buttonify) detaches output from actual events. | The deterministic state machine in `engine.py` never "asks" the LLM to narrate progress between steps. |
| **Turn Workflow** | Emphasises that `#V#conversation_turn_execution_workflow` is still thin/transitional. | Highlights that its execution is currently hardcoded in Python method sequences. |

---

## 4. Empirical Verification (Fact-Check)

An investigation of the Vontology and codebase confirms the technical claims made in both documents:

### 4.1 Vontology Findings
- **`#V#conversation_turn_execution_workflow`**: Confirmed as a "thin" concept in the KB. It contains only `critic` and `completion_gate` steps and explicitly carries a "capability-gap note" stating it is currently code-backed.
- **`#V#arxiv_paper_representation_workflow`**: Confirmed as a structurally complete multi-step workflow. However, it also carries a maintenance note admitting it relies on dedicated Python handlers because VWL lack certain source-specific acquisition primitives.

### 4.2 Codebase Findings
- **The Bypass Flag**: The existence of `selector_requests_custom_workflow` (L22306 of [orchestrator.py](../../src/backend/integrations/internal_mcp/orchestrator.py)) is verified. This flag does indeed trigger a path that bypasses the standard tool-calling loop in favour of a direct `execute_workflow()` call.
- **Scraping Logic**: The `_render_custom_workflow_response_text()` method (L3773 of [orchestrator.py](../../src/backend/integrations/internal_mcp/orchestrator.py)) was confirmed. It performs a "best-effort" scrape of the workflow's output dictionary (looking for `response_text`, `summary`, etc.), which explains why non-textual side-effects like concept creation are omitted from the chat.
- **Turn Lifecycle**: Ownership of the turn lifecycle in [von_routes.py](../../src/backend/server/routes/von_routes.py) (L8414) is verified, confirming the "sidecar" status of the durable instance.

---

## 5. Strategic Conclusion

The two analyses provide a coherent roadmap for architectural repair:
1. **Restore LLM Supervision**: The "Custom Workflow" path must be evolved into a **Supervised Workflow** path where the LLM can narrate progress between deterministic steps.
2. **Move Control to the Turn Workflow**: The top-level request lifecycle in [von_routes.py](../../src/backend/server/routes/von_routes.py) and [orchestrator.py](../../src/backend/integrations/internal_mcp/orchestrator.py) must be refactored into steps of a truly executable `#V#conversation_turn_execution_workflow`.
3. **Operational Reporting**: The response rendering logic must be upgraded to treat **durable side-effects** (concept IDs, file copies) as first-class user-visible facts rather than optional telemetry.

Von is not failing its vision, but it is currently "hiding" its success inside silent Python pipelines.

---

## 6. Additional Fact-Checked Comparison

### 6.1 Summary

The two source papers agree on the main architectural diagnosis: Von's intended design is workflow-first, Vontology-authoritative, and LLM-supervised, but the current top-level chat-turn path is still materially controlled by Python route/orchestrator code. They also agree that the key user-visible failure in the observed arXiv turn was not that "nothing happened", but that durable side-effects were not surfaced faithfully to the user.

They disagree mainly on how far the current system has already progressed toward that vision. The stronger disagreement is about whether parts of the current runtime are already genuinely workflow-executed or are better understood as workflow-labelled Python pipelines. Live inspection of the current workflow registry and Vontology surfaces shows that the present system is more workflow-real than the harsher paper suggests, but still more Python-led and communication-poor than either paper ultimately wants.

### 6.2 Further points of agreement

Both papers agree on all of the following.

1. The intended architecture is a layered hierarchy of chat-turn control, LLM interpretation/dispatch, workflow instantiation, stepwise workflow execution, and subordinate tool or skill use. That matches the authority doctrine in `AGENTS.md` and the VWL manual.
2. Python is still above the workflow layer too often. In the live route path, `von_routes.py` submits a conversation-turn instance, then immediately calls `orchestrator.run(...)`, then later finalises the turn instance after the fact.
3. The custom-workflow branch is still selected in Python via `selector_requests_custom_workflow`, and then handed to `execute_workflow(...)`.
4. The user-visible response path still hides durable effects. `_render_custom_workflow_response_text(...)` prefers `response_text`, `summary`, and `final_response`, rather than generically surfacing created artefacts such as file copies and concept IDs.
5. The arXiv turn did real represented-state work. Live workflow trace inspection showed outputs including a `computer_file_copy_concept_id`, a `paper_concept_id`, author concept IDs, topic concept IDs, and successful verification.

### 6.3 Further points of disagreement

The main disagreements are about degree rather than direction.

#### 6.3.1 How real the workflow layer already is

`chat_turn_workflow_control_plane_analysis.md` argues that it would be inaccurate to say the system simply bypassed the workflow layer, especially for the arXiv path.

`workflow_execution_architecture_analysis.md` presents a harsher reading in which the custom-workflow path turns authored workflows into opaque Python pipelines.

Live registry and Vontology inspection support the milder reading more strongly:

- `#V#arxiv_paper_representation_workflow` exists as a live Vontology-backed workflow definition
- it currently has a real multi-step state graph
- recent durable instances and execution traces exist for it

So the more accurate description is:

- the workflow structure is real
- the steps are often executed through deterministic Python-backed actions
- the top-level control and user explanation remain too Python-led

#### 6.3.2 Whether the tool-calling path is itself a real workflow

The two papers diverge sharply here.

The control-plane paper says `#V#tool_calling_workflow` is already represented as a real workflow.

The execution-architecture paper describes the tool-calling path more like pure LLM improvisation that does not really leverage authored VWL workflows.

The current runtime supports the control-plane paper more strongly:

- the tool-pipeline path calls `execute_workflow(tool_dispatch_workflow_id, ...)`
- `#V#tool_calling_workflow` exists in the workflow registry
- its `respond` step is authored with `workflow_step_execution_mode=llm`

So the tool path is not best described as workflow-free improvisation. It is better described as an authored workflow whose central reasoning step is LLM-mode.

#### 6.3.3 Whether narration and buttonify are only conceptual labels

The execution-architecture paper treats `#V#chat_buttonify_workflow` in particular as if it were mainly a concept or telemetry artefact whose real execution still lives in Python.

The current route and workflow surfaces show a more mixed reality:

- `von_routes.py` invokes both narration and buttonify through `orchestrator.execute_workflow(...)`
- Vontology-backed runtime definitions exist for both `#V#chat_narration_workflow` and `#V#chat_buttonify_workflow`
- `#V#chat_narration_workflow` has a genuine `llm` execution-mode step for narration rendering
- recent durable instances exist for `#V#chat_buttonify_workflow`

So these workflows are not merely conceptual. They are real runtime workflows, even if their placement after main response assembly still contributes to the communication gap.

#### 6.3.4 What is most wrong beneath the workflow surface

The control-plane paper puts stronger weight on semantically heavy Python domain mutation, especially in the scholarly-paper/arXiv materialisation path, including direct repository mutation below the service layer.

The execution-architecture paper concentrates more on:

- missing LLM supervision during workflow execution
- lack of progress/event emission
- the black-box nature of `execute_workflow()`

#### 6.3.5 Whether certain judgement steps should be `llm` steps

The execution-architecture paper pushes harder for an audit of steps such as `arxiv.decide_acquisition_mode`, suggesting some judgement steps may be misclassified as deterministic.

The control-plane paper is more willing to treat source normalisation, acquisition-mode selection, cache inspection, and verification as legitimate deterministic support primitives beneath the workflow.

Current authored state shows `#V#workflow_step_arxiv_paper_representation_workflow_decide_acquisition_mode` is explicitly marked `workflow_step_execution_mode=deterministic`.

The VWL manual requires prompt-bearing reasoning steps to compile as `llm`, but it does not say that every substantive decision must be LLM-authored. So this is best understood as a design question, not a clear present conformance violation.

### 6.4 Fact-check corrections and clarifications

#### 6.4.1 Workflow authority and transitional repo-side artefacts

The VWL manual supports both papers' core authority claims:

- Vontology-native workflow state is the intended authority surface
- repo-side seed bundles are transitional and non-authoritative by default
- backend code should mainly provide reusable support surfaces

#### 6.4.2 Conversation-turn workflow: real, but still thin

Live workflow inspection confirms that `#V#conversation_turn_execution_workflow` exists as a Vontology-backed durable workflow with a current runtime definition.

Its present runtime shape is thin:

- `turn_execution.critic`
- `turn_execution.completion_gate`
- terminal completion

That supports the control-plane paper's claim that the workflow is currently thin/transitional.

It does not support the stronger suggestion that the current live definition already governs context building, workflow discovery, dispatch, narration, and full response composition.

#### 6.4.3 The exact "not runnable" warning should not be over-generalised

The runtime evidence complicates the broader warning interpretation in the harsher paper.

Current fact-checking showed:

- `#V#chat_buttonify_workflow` has live runtime definitions and recorded instances
- `#V#arxiv_paper_representation_workflow` has live runtime definitions and recorded instances
- `#V#conversation_turn_execution_workflow` has bound actions and recorded workflow-use aggregates in Vontology

So a warning such as "workflow is not runnable; instance was not created" should be interpreted as a path-specific submission or probe result, not as proof that the named workflow is generally unreal or non-executable in the current system.

For `#V#conversation_turn_execution_workflow` specifically, the strongest supported claim is:

- it is not yet the true top-level controller of the request path

rather than:

- it simply lacks executable reality altogether

#### 6.4.4 The arXiv workflow and launch-input claim are supported

Live Vontology inspection confirms that `#V#arxiv_paper_representation_workflow` currently publishes a launch-input contract that requires `prompt`.

So the claim that unresolved `prompt` could truthfully trigger a launchability warning is well supported.

#### 6.4.5 Tool-calling workflow is a real authored workflow

The current system contains a live `#V#tool_calling_workflow` definition with:

- `tool_calling.preflight_requirements`
- `tool_calling.respond`
- `turn_execution.critic`
- `turn_execution.completion_gate`

Its `respond` step is currently authored as `llm` execution mode. This is an important correction to any reading that treats the tool path as merely improvisational and non-workflow-governed.

#### 6.4.6 Renderer critique is strongly supported

The criticism of `_render_custom_workflow_response_text(...)` is strongly supported by code inspection.

It prioritises a small family of text-like fields and a few experiment-oriented fields. It does not generically surface outcomes such as:

- `file_copy_concept_id`
- `paper_concept_id`
- `author_concept_ids`
- `topic_concept_ids`

That directly explains why a workflow can succeed in durable state while still producing a user-visible answer that sounds like "just a summary".

### 6.5 Bottom line of the added analysis

Neither paper should be adopted wholesale over the other.

The control-plane paper is more accurate about how much genuine workflow structure already exists in the runtime.

The execution-architecture paper is more forceful about the experiential and communicative cost of the current execution model.

Taken together, and corrected by live workflow and Vontology inspection, they support this balanced view:

- Von is not merely faking workflows
- Von is also not yet living under workflow-first top-level control
- the arXiv paper path is genuinely workflow-executed
- but the current user experience still makes those workflows feel like silent backend pipelines

---

## 7. Independent Fact-Check Results

The following fact-checks were performed against the live codebase and Vontology on 2026-04-07, using direct code inspection and MCP tool queries against the production ontology.

### 7.1 Claims Verified as Accurate (Both Documents)

| Claim | Evidence |
|---|---|
| `selector_requests_custom_workflow` flag exists at line 22306 of `orchestrator.py` with the described conditions | Confirmed — exact code matches both documents |
| `_render_custom_workflow_response_text()` scrapes fields (`response_text`, `summary`, `final_response`, `verdict`, etc.) and never surfaces `file_copy_concept_id` or `paper_concept_id` | Confirmed — function checks ~11 field names, none artefact-related |
| `#V#conversation_turn_execution_workflow` has 3 steps: critic → completion_gate → completed | Confirmed in Vontology (hasStep relation shows exactly these 3 step concepts) |
| The system collects durable workflow side-effects but only for telemetry | Confirmed — `_collect_workflow_durable_side_effects()` exists, detects `*_created_ids`/`*_updated_ids` patterns, feeds them to telemetry summaries only |
| `execute_workflow()` is a synchronous black-box call | Confirmed — blocking call, no async patterns |
| `WorkflowExecutor.run()` produces only internal telemetry, no user-facing events | Confirmed — traces, metadata validation events, and runtime events are internal only |

### 7.2 Claims Verified as Accurate (Control Plane Document Only)

| Claim | Evidence |
|---|---|
| `_register_python_defined_workflows()` is effectively a no-op for production | Confirmed — function body is `return None` with explicit comment that "Vontology is now authoritative" |
| The arXiv workflow steps include `normalise_arxiv_source`, `fetch_arxiv_metadata`, `decide_acquisition_mode`, `finalise_cached_pdf`, `recover_partial_cache_state`, `download_or_finalise`, `delegate_to_general_paper_workflow`, `verify_arxiv_path`, `completed`, `failed` | Confirmed in Vontology — the `hasStep` relation lists exactly 10 step concepts matching this inventory |
| Seed bundles are transitional infrastructure, not a subversion | Consistent with VWL manual §2.2, which explicitly permits seed bundles as "migration seed data" and notes `workflow_repo_seed_bootstrap.py` treats them as "seed fixtures only" |

### 7.3 Claims Found Inaccurate (Execution Architecture Document)

| Claim | Finding |
|---|---|
| `_register_durable_action_modules()` registers handlers from **20** workflow-family modules | **Actually 25 modules.** The extra 5 are: `skill_interop`, `context_bundle_actions`, `control_flow_actions`, `subworkflow_actions`, `testing_workflow_actions` |
| `#V#chat_buttonify_workflow` is "defined as a Vontology concept but its execution is embedded in the orchestrator's Python code rather than expressed as a runnable VWL program" | **Wrong.** Vontology shows `#V#chat_buttonify_workflow` as a fully runnable durable workflow with **506 attempts and 377 completions** (74.5% completion rate), 4 real steps (`assess_input`, `select_prompt`, `extract_options`, `completed`), and registered action handlers |
| The arXiv case study step sequence: `arxiv.normalise_source` → `arxiv.decide_acquisition_mode` → download → `scholarly_paper.materialise_from_file_copy` → `scholarly_paper.enrich_from_metadata` → `scholarly_paper.resolve_authors` → `scholarly_paper.verify_representation` | **Conflates the arXiv workflow with its sub-workflow.** The `materialise_from_file_copy`, `enrich_from_metadata`, and `resolve_authors` actions belong to `#V#scholarly_paper_representation_workflow`, which is invoked as a subworkflow via the `delegate_to_general_paper_workflow` step |
| `arxiv.decide_acquisition_mode` should potentially be an `llm`-mode step, citing VWL manual §4 | **Weakly argued; own qualifier undermines it.** The doc concedes "these specific steps do not currently carry prompt contracts." The VWL manual rule applies to steps that *have* prompt contracts, not to mechanically deterministic cache-existence checks |
| Startup bootstrap services are a "subversion" that ensures "perpetual availability of workflows whose entire step inventory is bound to monolithic Python handlers" | **Overstated.** VWL manual §2.2 explicitly anticipates this pattern as transitional, and `workflow_repo_seed_bootstrap.py` now preserves a valid Vontology workflow rather than overwriting it back to seed parity |

### 7.4 Vontology State Summary (as of 2026-04-07)

| Workflow Concept | Steps | Instances/Attempts | Status |
|---|---|---|---|
| `#V#conversation_turn_execution_workflow` | 3 (critic, completion_gate, completed) | 3 attempts, 100% completion | Exists but not the real turn controller — route/orchestrator Python owns the turn lifecycle |
| `#V#arxiv_paper_representation_workflow` | 10 steps (see §7.2) | 28 attempts, 82.1% completion | Fully operational; published lifecycle phase |
| `#V#chat_buttonify_workflow` | 4 steps (assess_input, select_prompt, extract_options, completed) | 506 attempts, 74.5% completion | Fully operational — contradicts execution architecture doc's claim |

### 7.5 Summary Assessment

The **control plane document** is more factually careful, more nuanced in distinguishing legitimate Python support from control-plane overreach, and more architecturally precise about where the top-level fix belongs. The **execution architecture document** provides a more thorough catalogue of the subversion patterns and a stronger articulation of the user-experience consequences, but contains several factual errors (workflow family count, buttonify runnability, arXiv step conflation) and tends to overstate the severity of patterns the VWL manual already anticipates as transitional.
