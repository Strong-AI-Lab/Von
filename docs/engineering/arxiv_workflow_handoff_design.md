# Phase 0 Design Note: JVNAUTOSCI-1763
## Ceding Turn Control to Durable Conversation Workflows

**Status**: Draft
**Related Issue**: JVNAUTOSCI-1763
**Author**: Gemini CLI

### 1. Objective
Transition the `/von/generate` route from a "Python-orchestrated" model to a "Workflow-owned" model. The route should serve as a lightweight entry point that instantiates `#V#conversation_turn_execution_workflow` and streams its lifecycle events directly to the client.

### 2. Current Reality Analysis
Currently, `/von/generate` in `von_routes.py` acts as the "brain" of the turn. It manually sequences:
1.  User context resolution and early history persistence.
2.  `orchestrator.run()` call (which itself may bypass the LLM for "custom workflows").
3.  Manual backfilling of narration (`CHAT_NARRATION_WORKFLOW_ID`).
4.  Manual execution of quick-reply generation (`CHAT_BUTTONIFY_WORKFLOW_ID`).

While a durable instance of `#V#conversation_turn_execution_workflow` is registered today, it is used primarily for telemetry. The Python route still blocks on the orchestrator's completion before returning a single JSON response.

### 3. Proposed Changes

#### 3.1. Route Transformation
The `generate()` function in `von_routes.py` will be refactored to:
*   Perform only the essential pre-flight checks (Auth, Namespace, Rate Limiting).
*   Call `submit_verified_workflow_instance` for `#V#conversation_turn_execution_workflow`.
*   Immediately return a streaming response using Server-Sent Events (SSE).

#### 3.2. Workflow Ownership
The `#V#conversation_turn_execution_workflow` definition must be updated to encompass the entire turn lifecycle:
*   **Step 1: Routing**: Execute the `WorkflowSelector` logic.
*   **Step 2: Execution**: Dispatch to the selected sub-workflow (Tool Calling or Specialized).
*   **Step 3: Refinement**: Parallel or sequential execution of Narration and Buttonify.
*   **Step 4: Finalisation**: Assemble the final `OrchestratorResult`.

#### 3.3. Streaming Event Protocol
The `WorkflowExecutor` and `DurableWorkflowExecutor` will be augmented to emit lifecycle events into a durable event log (e.g., a capped collection in MongoDB or a Redis stream). The SSE generator in the route will yield:
*   `event: progress`: For phase transitions (e.g., `workflow_discovery`, `tool_execute`).
*   `event: text_chunk`: For streaming LLM responses from within workflow steps.
*   `event: result`: The final structured outcome of the turn.

#### 3.4. Handling the "Custom Workflow" Bypass
The bypass logic currently in `orchestrator.py` will move into the `#V#conversation_turn_execution_workflow` VWL transitions. The decision to "skip the LLM and run deterministic actions" will be an authored policy in Vontology, making it transparent and editable without Python deployments.

### 4. Implementation Phasing
*   **Phase 0 (This Note)**: Architecture alignment.
*   **Phase 1**: Implement SSE event bridge in `von_routes.py`.
*   **Phase 2**: Author the "Master Turn" VWL definition.
*   **Phase 3**: Migrate Narration/Buttonify logic into the master workflow.
