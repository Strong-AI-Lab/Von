# Arxiv VWL Purity Analysis

## Current Status of JVNAUTOSCI-1763
Task 1763 attempted to transition the arXiv ingestion pipeline to a supervised Vontology-hosted Workflow (VWL). While structural steps were added to the engine, the implementation falls fundamentally short of actual "workflow purity." As detailed in JVNAUTOSCI-1768, the control plane is still effectively owned by Python, treating VWL definitions as mere data rather than the authoritative execution graph.

## Why 1763 Failed the Purity Requirement
The core requirement "no more Python code pretending to be a proper VWL Vontology hosted workflow" means that all execution logic, branching, prompts, and evaluation must be represented in the Vontology and interpreted by a generic engine. 

Instead, the implementation exhibited 7 major authority gaps:
1. **Control Flow in Python (Gap 1):** The orchestrator (`von_routes.py`) has an explicit `if is_arxiv_workflow:` fork, overriding the generic pipeline and hardcoding route knowledge about specific workflows.
2. **Prompts Hardcoded in Code (Gap 2):** The narration step prompt is hardcoded in `turn_execution_actions.py` instead of being a Vontology `#V#prompt...` concept invoked via an `execution_mode: "llm"` step.
3. **Missing Declarative Validation (Gap 3):** The `critic` and `completion_gate` steps were defined in the JSON but not implemented or registered properly, leaving validation enforcement incomplete.
4. **Fallback Scraping (Gap 4):** Python code (`orchestrator.py`) manually scrapes summary fields from the result object as a fallback if the narration fails, violating the principle that the workflow itself should define its output contract.
5. **Orphaned Schema Fields (Gap 5):** Telemetry records were extended but not actually populated during execution.
6. **Hardcoded State Inspection (Gap 6):** `paper_representation_workflow.py` relies on fragile substring matching (`"arxiv" in source_url`) rather than querying the ontology for explicit source identity relations.
7. **Inconsistent Contracts (Gap 7):** Dual-named fields in the completion report.

## How to Truly Satisfy the Purity Requirement
To truly achieve VWL purity and fix these anti-patterns, developers must internalize that **Python is the Engine, not the Application**. 

We must execute the following corrective strategy:
1. **Convert Narration to a VWL LLM Step:** Delete the Python narration handler. Create a `#V#prompt_turn_execution_narrate_completion_report` concept. Use standard VWL `execution_mode: "llm"` to invoke it.
2. **Implement Vontology-based Critics:** Ensure the critic and completion gate steps are fully implemented using the Vontology prompt concepts.
3. **Remove Route-Level Forks:** Delete the arXiv specific branch in `von_routes.py`. The route must indiscriminately execute `execute_conversation_turn_supervised()`.
4. **Enforce Output Contracts:** Remove Python fallbacks. If a workflow fails to produce `response_text`, it should be a workflow failure, not something the Python wrapper "fixes" by scraping IDs.
5. **Generalize Completion Reports:** Rename `arxiv_completion_report` to `completion_report` so any supervised workflow can return structural completion data to the orchestrator.

**Conclusion:** JVNAUTOSCI-1763 should be considered incomplete because it violated the fundamental architectural mandate. We must implement JVNAUTOSCI-1768 to close these gaps and enforce true VWL purity.